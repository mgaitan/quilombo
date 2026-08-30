import asyncio
import base64
import hashlib
import io
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import httpx2
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.signing import SignatureExpired
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Implementation
from rest_framework.test import APIClient

from .models import (
    AccessEvent,
    ApiToken,
    Holding,
    InventoryEvent,
    Item,
    Location,
    LocationRelation,
    Membership,
    OAuthAuthorizationGrant,
    OAuthAuthorizationRequest,
    OAuthClient,
    OAuthCredential,
    VerificationStatus,
    Workspace,
)
from .services import (
    BulkUpsertError,
    InventoryUndoError,
    audit_inventory,
    create_item_with_holding,
    delete_inventory_item,
    hash_request,
    move_inventory,
    preview_inventory_undo,
    undo_inventory_event,
    update_inventory_item,
)

SOCIAL_PROVIDER_SETTINGS = {
    "github": {
        "APPS": [{"client_id": "github-client", "secret": "github-secret", "key": ""}],
        "SCOPE": ["user:email"],
        "EMAIL_AUTHENTICATION": True,
        "EMAIL_AUTHENTICATION_AUTO_CONNECT": True,
    },
    "google": {
        "APPS": [{"client_id": "google-client", "secret": "google-secret", "key": ""}],
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "OAUTH_PKCE_ENABLED": True,
        "EMAIL_AUTHENTICATION": True,
        "EMAIL_AUTHENTICATION_AUTO_CONNECT": True,
    },
}


def test_database_disables_server_side_cursors_for_transaction_poolers():
    assert settings.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True


@override_settings(
    PUBLIC_BASE_URL="https://quilombo.life",
    LEGACY_PUBLIC_HOSTS=("quilombo-v1-mgaitan.onrender.com",),
)
def test_legacy_render_hostname_redirects_to_canonical_domain():
    from quilombo.asgi import create_application

    async def request_legacy_mcp():
        transport = httpx2.ASGITransport(app=create_application())
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="https://quilombo-v1-mgaitan.onrender.com",
        ) as http_client:
            return await http_client.post("/mcp?source=legacy")

    response = asyncio.run(request_legacy_mcp())

    assert response.status_code == 308
    assert response.headers["Location"] == "https://quilombo.life/mcp?source=legacy"


@pytest.fixture
def users(db):
    user_model = get_user_model()
    return user_model.objects.create_user("one"), user_model.objects.create_user("two")


@pytest.fixture
def workspaces(users):
    first = Workspace.objects.create(name="Workshop", slug="workshop")
    second = Workspace.objects.create(name="Library", slug="library")
    Membership.objects.create(workspace=first, user=users[0], role=Membership.Role.OWNER)
    Membership.objects.create(workspace=second, user=users[1], role=Membership.Role.OWNER)
    return first, second


@pytest.mark.django_db
def test_location_parent_must_share_workspace(workspaces):
    first, second = workspaces
    parent = Location.objects.create(workspace=first, key="drawer", name="Drawer")
    child = Location(workspace=second, key="a1", name="A1", parent=parent)

    with pytest.raises(ValidationError, match="another workspace"):
        child.full_clean()


@pytest.mark.django_db
def test_location_hierarchy_rejects_cycles(workspaces):
    workspace, _ = workspaces
    parent = Location.objects.create(workspace=workspace, key="drawer", name="Drawer")
    child = Location.objects.create(
        workspace=workspace, key="drawer-a", name="Drawer A", parent=parent
    )
    parent.parent = child

    with pytest.raises(ValidationError, match="cycles"):
        parent.full_clean()


@pytest.mark.django_db
def test_discrete_holding_requires_whole_quantity(workspaces):
    workspace, _ = workspaces
    shelf = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    book = Item.objects.create(
        workspace=workspace,
        key="gelman-1",
        name="Interrupciones I",
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
    )
    holding = Holding(workspace=workspace, item=book, location=shelf, quantity=Decimal("1.5"))

    with pytest.raises(ValidationError, match="whole quantity"):
        holding.full_clean()


@pytest.mark.django_db
def test_holding_quantity_cannot_be_negative(workspaces):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="a1", name="A1")
    item = Item.objects.create(workspace=workspace, key="screw", name="Screw")

    with pytest.raises(IntegrityError), transaction.atomic():
        Holding.objects.create(
            workspace=workspace, item=item, location=location, quantity=Decimal("-1")
        )


@pytest.mark.django_db
def test_api_isolates_workspaces(users, workspaces):
    first, second = workspaces
    Location.objects.create(workspace=first, key="a1", name="A1")
    Location.objects.create(workspace=second, key="shelf", name="Shelf")
    client = APIClient()
    client.force_authenticate(users[0])

    own_response = client.get("/api/workspaces/workshop/locations/")
    other_response = client.get("/api/workspaces/library/locations/")

    assert own_response.status_code == 200
    assert own_response.json()["results"][0]["key"] == "a1"
    assert other_response.status_code == 404


@pytest.mark.django_db
def test_api_rejects_cross_workspace_holding(users, workspaces):
    first, second = workspaces
    own_location = Location.objects.create(workspace=first, key="a1", name="A1")
    other_item = Item.objects.create(workspace=second, key="book", name="Book")
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/holdings/",
        {
            "item": str(other_item.id),
            "location": str(own_location.id),
            "quantity": "1",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "another workspace" in str(response.json())


@pytest.mark.django_db
def test_api_collections_paginate_stably_and_handle_last_and_empty_pages(users, workspaces):
    workshop, library = workspaces
    for name in ["Echo", "Alpha", "Delta", "Bravo", "Charlie"]:
        Item.objects.create(workspace=workshop, key=name.lower(), name=name)
    client = APIClient()
    client.force_authenticate(users[0])

    first_page = client.get("/api/workspaces/workshop/items/", {"page": 1, "page_size": 2})
    last_page = client.get("/api/workspaces/workshop/items/", {"page": 3, "page_size": 2})

    assert first_page.status_code == 200
    assert [row["name"] for row in first_page.json()["results"]] == ["Alpha", "Bravo"]
    assert first_page.json()["pagination"] == {
        "count": 5,
        "page": 1,
        "page_size": 2,
        "total_pages": 3,
        "next": "http://testserver/api/workspaces/workshop/items/?page=2&page_size=2",
        "previous": None,
    }
    assert [row["name"] for row in last_page.json()["results"]] == ["Echo"]
    assert last_page.json()["pagination"]["next"] is None

    client.force_authenticate(users[1])
    empty_page = client.get("/api/workspaces/library/items/", {"page_size": 2})
    assert empty_page.status_code == 200
    assert empty_page.json()["results"] == []
    assert empty_page.json()["pagination"]["count"] == 0


@pytest.mark.django_db
def test_workspace_api_pagination_uses_id_to_break_name_ties(users, workspaces):
    workshop, _ = workspaces
    same_named = [
        Workspace.objects.create(name="Shared", slug=f"shared-{index}") for index in range(3)
    ]
    Membership.objects.bulk_create(
        [
            Membership(workspace=workspace, user=users[0], role=Membership.Role.MEMBER)
            for workspace in same_named
        ]
    )
    client = APIClient()
    client.force_authenticate(users[0])

    first = client.get("/api/workspaces/", {"page": 1, "page_size": 2}).json()
    second = client.get("/api/workspaces/", {"page": 2, "page_size": 2}).json()

    returned_ids = [row["id"] for row in [*first["results"], *second["results"]]]
    expected_ids = [
        str(workspace.id)
        for workspace in sorted([workshop, *same_named], key=lambda row: (row.name, row.id))
    ]
    assert returned_ids == expected_ids
    assert len(returned_ids) == len(set(returned_ids))


@pytest.mark.django_db
def test_search_pagination_preserves_filters_and_tenant_scope(users, workspaces):
    workshop, library = workspaces
    drawer = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    other_location = Location.objects.create(workspace=library, key="drawer", name="Other drawer")
    for index in range(3):
        item = Item.objects.create(
            workspace=workshop,
            key=f"bolt-{index}",
            name=f"Bolt {index}",
            category="fasteners",
        )
        Holding.objects.create(
            workspace=workshop, item=item, location=drawer, quantity=Decimal("1")
        )
    other_item = Item.objects.create(workspace=library, key="bolt-secret", name="Bolt secret")
    Holding.objects.create(
        workspace=library, item=other_item, location=other_location, quantity=Decimal("1")
    )
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.get(
        "/api/workspaces/workshop/search/",
        {
            "q": "bolt",
            "category": "fasteners",
            "location": "drawer",
            "page": 2,
            "page_size": 1,
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 3
    assert body["truncated"] is False
    assert body["pagination"]["page"] == 2
    assert len(body["results"]) == 1
    assert "category=fasteners" in body["pagination"]["next"]
    assert "location=drawer" in body["pagination"]["previous"]
    assert all(row["item_key"] != "bolt-secret" for row in body["results"])


@pytest.mark.django_db
def test_bulk_upsert_creates_workshop_inventory_with_provenance(users, workspaces):
    workspace, _ = workspaces
    client = APIClient()
    client.force_authenticate(users[0])
    payload = {
        "idempotency_key": "photo-2026-08-14-001",
        "provenance": {
            "client_actor": "workshop-agent/1.0",
            "source_kind": "photo",
            "source_reference": "Processed a workshop photo on 2026-08-14",
        },
        "locations": [
            {"key": "drawer", "name": "Drawer cabinet"},
            {"key": "drawer-1", "name": "Drawer 1", "parent_key": "drawer"},
            {"key": "drawer-2", "name": "Drawer 2", "parent_key": "drawer"},
        ],
        "items": [
            {
                "key": "fix-35mm",
                "name": "FIX 35 mm screws",
                "category": "wood screws",
                "aliases": ["tornillos para madera"],
                "unit": "piece",
                "minimum_quantity": "25",
                "target_quantity": "100",
            }
        ],
        "holdings": [
            {
                "item_key": "fix-35mm",
                "location_key": "drawer-1",
                "quantity": "100",
                "approximate": True,
            }
        ],
        "location_relations": [
            {"subject_key": "drawer-1", "relation": "above", "object_key": "drawer-2"}
        ],
    }

    response = client.post("/api/workspaces/workshop/bulk-upsert/", payload, format="json")

    assert response.status_code == 201
    assert response.json()["processed"] == {
        "locations": 3,
        "items": 1,
        "holdings": 1,
        "location_relations": 1,
    }
    assert workspace.locations.get(key="drawer-1").parent.key == "drawer"
    assert workspace.holdings.get().quantity == Decimal("100")
    assert workspace.items.get().minimum_quantity == Decimal("25")
    assert workspace.items.get().target_quantity == Decimal("100")
    event = workspace.inventory_events.get()
    assert event.source_kind == InventoryEvent.SourceKind.PHOTO
    assert event.source_reference == "Processed a workshop photo on 2026-08-14"

    replay = client.post("/api/workspaces/workshop/bulk-upsert/", payload, format="json")

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert workspace.inventory_events.count() == 1


@pytest.mark.django_db
def test_web_history_previews_and_undoes_latest_bulk_upsert(client, users, workspaces):
    workspace, _ = workspaces
    Location.objects.create(workspace=workspace, key="bench", name="Workbench")
    api = APIClient()
    api.force_authenticate(users[0])
    payload = {
        "idempotency_key": "mistaken-bulk-001",
        "provenance": {
            "metadata": {"server_mcp_client": {"name": "spoofed-rest-client", "version": "9.9"}}
        },
        "locations": [{"key": "wrong-box", "name": "Wrong box"}],
        "items": [{"key": "wrong-item", "name": "Wrong item"}],
        "holdings": [{"item_key": "wrong-item", "location_key": "wrong-box", "quantity": "2"}],
    }
    assert (
        api.post("/api/workspaces/workshop/bulk-upsert/", payload, format="json").status_code == 201
    )
    original = workspace.inventory_events.get(kind=InventoryEvent.Kind.BULK_UPSERT)
    assert "server_mcp_client" not in original.metadata
    original.client_actor = "inventory-agent/2.0"
    original.source_kind = InventoryEvent.SourceKind.AGENT
    original.source_reference = "conversation://inventory-42"
    original.metadata = {"server_mcp_client": {"name": "quilombo-test-client", "version": "1.2.3"}}
    original.save(update_fields=["client_actor", "source_kind", "source_reference", "metadata"])
    original_summary = original.summary.copy()
    client.force_login(users[0])

    history = client.get("/app/workshop/history/")
    preview = client.get(f"/app/workshop/history/{original.id}/undo/")
    response = client.post(
        f"/app/workshop/history/{original.id}/undo/",
        {"preview_token": preview.context["preview"]["token"]},
    )

    assert history.status_code == 200
    history_html = history.content.decode()
    assert "mistaken-bulk-001" not in history_html
    assert f"/app/workshop/history/{original.id}/undo/" in history_html
    assert "quilombo-test-client 1.2.3" in history_html
    assert "inventory-agent/2.0" in history_html
    assert "conversation://inventory-42" in history_html
    assert "1 ubicación" in history_html
    assert "1 objeto" in history_html
    assert "1 existencia" in history_html
    assert str(original.summary) not in history_html
    assert preview.status_code == 200
    assert preview.context["preview"]["allowed"] is True
    assert response.status_code == 302
    assert list(workspace.locations.values_list("key", flat=True)) == ["bench"]
    assert not workspace.items.exists()
    original.refresh_from_db()
    assert original.summary == original_summary
    undo = workspace.inventory_events.get(kind=InventoryEvent.Kind.UNDO)
    assert undo.summary["undoes_event_id"] == str(original.id)
    undo_history = client.get("/app/workshop/history/").content.decode()
    assert "Se deshizo: Actualización en lote" in undo_history


@pytest.mark.django_db
def test_undo_requires_preview_and_rejects_inventory_changed_after_it(users, workspaces):
    workspace, _ = workspaces
    source = Location.objects.create(workspace=workspace, key="source", name="Source")
    destination = Location.objects.create(
        workspace=workspace, key="destination", name="Destination"
    )
    item = Item.objects.create(workspace=workspace, key="book", name="A book")
    Holding.objects.create(workspace=workspace, item=item, location=source, quantity=Decimal("3"))
    request = {
        "item_key": item.key,
        "from_location_key": source.key,
        "to_location_key": destination.key,
        "quantity": "1",
        "idempotency_key": "move-before-undo",
        "provenance": {},
    }
    event, _ = move_inventory(
        workspace=workspace,
        actor=users[0],
        request_hash=hash_request(request),
        **request,
    )
    preview = preview_inventory_undo(workspace=workspace, event=event)
    moved = workspace.holdings.get(location=destination)
    moved.notes = "Edited after preview"
    moved.save(update_fields=["notes", "updated_at"])

    with pytest.raises(InventoryUndoError):
        undo_inventory_event(
            workspace=workspace,
            actor=users[0],
            event_id=event.id,
            preview_token=preview["token"],
        )
    with pytest.raises(InventoryUndoError):
        undo_inventory_event(
            workspace=workspace,
            actor=users[0],
            event_id=event.id,
            preview_token="",
        )

    assert not workspace.inventory_events.filter(kind=InventoryEvent.Kind.UNDO).exists()
    assert workspace.holdings.get(location=destination).notes == "Edited after preview"


@pytest.mark.django_db
def test_previewed_move_undo_restores_quantities_and_records_compensation(users, workspaces):
    workspace, _ = workspaces
    source = Location.objects.create(workspace=workspace, key="shelf-a", name="Shelf A")
    destination = Location.objects.create(workspace=workspace, key="shelf-b", name="Shelf B")
    item = Item.objects.create(workspace=workspace, key="gelman", name="Interrupciones I")
    original_holding = Holding.objects.create(
        workspace=workspace,
        item=item,
        location=source,
        quantity=Decimal("2"),
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=timezone.now(),
        last_observed_by=users[0],
    )
    original_timestamps = {
        "location_created": source.created_at,
        "location_updated": source.updated_at,
        "item_created": item.created_at,
        "item_updated": item.updated_at,
        "holding_updated": original_holding.updated_at,
    }
    request = {
        "item_key": item.key,
        "from_location_key": source.key,
        "to_location_key": destination.key,
        "quantity": "1",
        "idempotency_key": "move-book-undo",
        "provenance": {},
    }
    event, _ = move_inventory(
        workspace=workspace,
        actor=users[0],
        request_hash=hash_request(request),
        **request,
    )

    preview = preview_inventory_undo(workspace=workspace, event=event)
    undo = undo_inventory_event(
        workspace=workspace,
        actor=users[0],
        event_id=event.id,
        preview_token=preview["token"],
    )

    restored = workspace.holdings.get(location=source)
    source.refresh_from_db()
    item.refresh_from_db()
    assert restored.id == original_holding.id
    assert restored.quantity == Decimal("2")
    assert source.created_at == original_timestamps["location_created"]
    assert source.updated_at == original_timestamps["location_updated"]
    assert item.created_at == original_timestamps["item_created"]
    assert item.updated_at == original_timestamps["item_updated"]
    assert restored.updated_at == original_timestamps["holding_updated"]
    assert restored.verification_status == VerificationStatus.CONFIRMED
    assert restored.last_observed_at == original_holding.last_observed_at
    assert restored.last_observed_by == users[0]
    assert not workspace.holdings.filter(location=destination).exists()
    assert undo.kind == InventoryEvent.Kind.UNDO
    assert undo.summary["original_kind"] == InventoryEvent.Kind.MOVE


@pytest.mark.django_db
def test_read_only_member_can_view_history_but_cannot_open_undo(client, users, workspaces):
    workspace, _ = workspaces
    Membership.objects.create(workspace=workspace, user=users[1], can_write=False)
    event = InventoryEvent.objects.create(
        workspace=workspace,
        kind=InventoryEvent.Kind.MOVE,
        undo_data={
            "before": {"locations": [], "items": [], "holdings": [], "location_relations": []},
            "after_hash": "unused",
        },
    )
    client.force_login(users[1])

    history = client.get("/app/workshop/history/")
    undo = client.get(f"/app/workshop/history/{event.id}/undo/")

    assert history.status_code == 200
    assert f"/app/workshop/history/{event.id}/undo/" not in history.content.decode()
    assert undo.status_code == 403


@pytest.mark.django_db
def test_bulk_upsert_rejects_changed_idempotent_request(users, workspaces):
    client = APIClient()
    client.force_authenticate(users[0])
    url = "/api/workspaces/workshop/bulk-upsert/"
    first = {
        "idempotency_key": "same-key",
        "locations": [{"key": "a1", "name": "A1"}],
    }
    changed = {
        "idempotency_key": "same-key",
        "locations": [{"key": "a1", "name": "A1 changed"}],
    }

    assert client.post(url, first, format="json").status_code == 201
    response = client.post(url, changed, format="json")

    assert response.status_code == 409
    assert "different payload" in response.json()["detail"]


@pytest.mark.django_db
def test_bulk_upsert_rolls_back_invalid_references(users, workspaces):
    workspace, _ = workspaces
    client = APIClient()
    client.force_authenticate(users[0])
    payload = {
        "idempotency_key": "invalid-reference",
        "items": [{"key": "book", "name": "A book", "tracking_mode": "discrete"}],
        "holdings": [{"item_key": "book", "location_key": "missing", "quantity": "1"}],
    }

    response = client.post("/api/workspaces/workshop/bulk-upsert/", payload, format="json")

    assert response.status_code == 400
    assert workspace.items.count() == 0
    assert workspace.inventory_events.count() == 0


@pytest.mark.django_db
def test_bulk_upsert_query_count_is_constant_for_large_batch(users, workspaces):
    client = APIClient()
    client.force_authenticate(users[0])
    payload = {
        "idempotency_key": "large-batch",
        "locations": [{"key": f"box-{index}", "name": f"Box {index}"} for index in range(100)],
        "items": [{"key": f"book-{index}", "name": f"Book {index}"} for index in range(100)],
        "holdings": [
            {"item_key": f"book-{index}", "location_key": f"box-{index}", "quantity": "1"}
            for index in range(100)
        ],
    }

    with CaptureQueriesContext(connection) as queries:
        response = client.post("/api/workspaces/workshop/bulk-upsert/", payload, format="json")

    assert response.status_code == 201
    assert len(queries) < 25


@pytest.mark.django_db
def test_json_inventory_export_dry_run_import_and_idempotent_round_trip(users, workspaces):
    workshop, _ = workspaces
    root = Location.objects.create(workspace=workshop, key="workshop", name="Workshop")
    drawer = Location.objects.create(
        workspace=workshop,
        key="drawer",
        name="Drawer",
        parent=root,
        aliases=["cajón"],
        metadata={"zone": 1},
    )
    item = Item.objects.create(
        workspace=workshop,
        key="screws",
        name="Screws",
        attributes={"material": "steel"},
        unit="piece",
        minimum_quantity=Decimal("5"),
        target_quantity=Decimal("10"),
    )
    holding = Holding.objects.create(
        workspace=workshop,
        item=item,
        location=drawer,
        quantity=Decimal("8"),
        approximate=True,
        notes="Counted by hand",
    )
    relation = LocationRelation.objects.create(
        workspace=workshop,
        subject=drawer,
        relation=LocationRelation.Relation.NEAR,
        object=root,
    )
    client = APIClient()
    client.force_authenticate(users[0])

    exported = client.get("/api/workspaces/workshop/export/", {"format": "json"})
    document = exported.json()

    assert exported.status_code == 200
    assert "workshop-inventory.json" in exported.headers["Content-Disposition"]
    assert document["format_version"] == "1.0"
    assert document["holdings"][0]["id"] == str(holding.id)

    drawer_id = drawer.id
    item_id = item.id
    holding_id = holding.id
    relation.delete()
    holding.delete()
    item.delete()
    drawer.delete()
    root.delete()
    payload = {
        "format": "json",
        "document": document,
        "dry_run": True,
        "idempotency_key": "json-import-001",
        "provenance": {
            "client_actor": "test-importer",
            "source_reference": "Round-trip fixture",
            "metadata": {"server_mcp_client": {"name": "spoofed-import-client", "version": "9.9"}},
        },
    }
    preview = client.post("/api/workspaces/workshop/import/", payload, format="json")

    assert preview.status_code == 200
    assert preview.json()["event_id"] is None
    assert preview.json()["summary"]["locations"] == {"created": 2, "updated": 0}
    assert workshop.locations.count() == 0
    assert not workshop.inventory_events.filter(idempotency_key="json-import-001").exists()

    payload["dry_run"] = False
    imported = client.post("/api/workspaces/workshop/import/", payload, format="json")
    replayed = client.post("/api/workspaces/workshop/import/", payload, format="json")

    assert imported.status_code == 200
    assert imported.json()["replayed"] is False
    assert replayed.json()["replayed"] is True
    assert workshop.locations.get(id=drawer_id).aliases == ["cajón"]
    assert workshop.items.get(id=item_id).attributes == {"material": "steel"}
    assert workshop.holdings.get(id=holding_id).quantity == Decimal("8")
    event = workshop.inventory_events.get(idempotency_key="json-import-001")
    assert event.source_kind == InventoryEvent.SourceKind.IMPORT
    assert event.client_actor == "test-importer"
    assert event.metadata["transfer_format_version"] == "1.0"
    assert "server_mcp_client" not in event.metadata


@pytest.mark.django_db
def test_csv_inventory_round_trip_covers_library_records(users, workspaces):
    _, library = workspaces
    shelf = Location.objects.create(
        workspace=library,
        key="poetry",
        name="Poesía",
        metadata={"floor": 2},
    )
    book = Item.objects.create(
        workspace=library,
        key="gelman",
        name="Interrupciones I",
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
        attributes={"author": "Juan Gelman"},
    )
    holding = Holding.objects.create(
        workspace=library,
        item=book,
        location=shelf,
        quantity=Decimal("1"),
        notes="Firmado",
    )
    client = APIClient()
    client.force_authenticate(users[1])

    exported = client.get("/api/workspaces/library/export/", {"format": "csv"})
    content = exported.content.decode()

    assert exported.status_code == 200
    assert exported.headers["Content-Type"].startswith("text/csv")
    assert content.splitlines()[0].startswith("record_type,id,key,name")
    assert "Poesía" in content

    shelf_id = shelf.id
    book_id = book.id
    holding_id = holding.id
    holding.delete()
    book.delete()
    shelf.delete()
    imported = client.post(
        "/api/workspaces/library/import/",
        {
            "format": "csv",
            "content": content,
            "idempotency_key": "csv-import-001",
        },
        format="json",
    )

    assert imported.status_code == 200
    assert library.locations.get(id=shelf_id).metadata == {"floor": 2}
    assert library.items.get(id=book_id).attributes == {"author": "Juan Gelman"}
    assert library.holdings.get(id=holding_id).notes == "Firmado"


@pytest.mark.django_db
def test_inventory_import_rejects_foreign_ids_without_partial_writes(users, workspaces):
    workshop, library = workspaces
    foreign_location = Location.objects.create(
        workspace=library, key="private", name="Private shelf"
    )
    new_item_id = uuid.uuid4()
    document = {
        "format_version": "1.0",
        "locations": [
            {
                "id": str(foreign_location.id),
                "key": "intrusion",
                "name": "Intrusion",
                "parent_id": None,
            }
        ],
        "items": [
            {
                "id": str(new_item_id),
                "key": "new-item",
                "name": "New item",
                "tracking_mode": "bulk",
                "unit": "unit",
                "minimum_quantity": None,
                "target_quantity": None,
            }
        ],
        "holdings": [],
        "location_relations": [],
    }
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/import/",
        {
            "format": "json",
            "content": json.dumps(document),
            "idempotency_key": "foreign-import",
        },
        format="json",
    )
    inaccessible_export = client.get("/api/workspaces/library/export/")
    inaccessible_import = client.post(
        "/api/workspaces/library/import/",
        {
            "format": "json",
            "document": document,
            "idempotency_key": "inaccessible",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "another workspace" in response.json()["detail"]
    assert not workshop.locations.exists()
    assert not workshop.items.filter(id=new_item_id).exists()
    assert not workshop.inventory_events.filter(idempotency_key="foreign-import").exists()
    assert foreign_location.workspace == library
    assert inaccessible_export.status_code == 404
    assert inaccessible_import.status_code == 404


@pytest.mark.django_db
def test_inventory_dry_run_rejects_duplicate_holding_identity(users, workspaces):
    workshop, _ = workspaces
    location_id = uuid.uuid4()
    item_id = uuid.uuid4()
    document = {
        "format_version": "1.0",
        "locations": [{"id": str(location_id), "key": "box", "name": "Box", "parent_id": None}],
        "items": [
            {
                "id": str(item_id),
                "key": "part",
                "name": "Part",
                "tracking_mode": "bulk",
                "unit": "unit",
                "minimum_quantity": None,
                "target_quantity": None,
            }
        ],
        "holdings": [
            {
                "id": str(uuid.uuid4()),
                "item_id": str(item_id),
                "location_id": str(location_id),
                "quantity": "1",
            },
            {
                "id": str(uuid.uuid4()),
                "item_id": str(item_id),
                "location_id": str(location_id),
                "quantity": "2",
            },
        ],
        "location_relations": [],
    }
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/import/",
        {
            "format": "json",
            "document": document,
            "dry_run": True,
            "idempotency_key": "duplicate-holding",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "duplicate item and location" in response.json()["detail"]
    assert not workshop.locations.exists()
    assert not workshop.items.exists()


@pytest.mark.django_db
def test_inventory_import_does_not_reassign_uuid_created_by_another_workspace(users, workspaces):
    workshop, library = workspaces
    location_id = uuid.uuid4()
    document = {
        "format_version": "1.0",
        "locations": [
            {
                "id": str(location_id),
                "key": "imported",
                "name": "Imported",
                "parent_id": None,
            }
        ],
        "items": [],
        "holdings": [],
        "location_relations": [],
    }
    from .transfers import _validate_target

    def create_competing_location(workspace, candidate):
        _validate_target(workspace, candidate)
        Location.objects.create(
            id=location_id,
            workspace=library,
            key="competing",
            name="Competing",
        )

    client = APIClient()
    client.force_authenticate(users[0])
    with patch(
        "inventory.transfers._validate_target",
        side_effect=create_competing_location,
    ):
        response = client.post(
            "/api/workspaces/workshop/import/",
            {
                "format": "json",
                "document": document,
                "idempotency_key": "competing-uuid",
            },
            format="json",
        )

    assert response.status_code == 400
    assert "constraint" in response.json()["detail"]
    assert not workshop.locations.filter(id=location_id).exists()


@pytest.mark.django_db
def test_inventory_import_rejects_non_object_provenance_metadata(users, workspaces):
    workshop, _ = workspaces
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/import/",
        {
            "format": "json",
            "document": {
                "format_version": "1.0",
                "locations": [],
                "items": [],
                "holdings": [],
                "location_relations": [],
            },
            "idempotency_key": "invalid-provenance",
            "provenance": {"metadata": ["camera"]},
        },
        format="json",
    )

    assert response.status_code == 400
    assert "Metadata must be a JSON object" in str(response.json())
    assert not workshop.inventory_events.exists()


@pytest.mark.django_db
def test_user_can_create_workspace_and_becomes_owner(users):
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post("/api/workspaces/", {"name": "Garage", "slug": "garage"}, format="json")

    assert response.status_code == 201
    workspace = Workspace.objects.get(slug="garage")
    assert workspace.memberships.get(user=users[0]).role == Membership.Role.OWNER


@pytest.mark.django_db
def test_api_token_is_returned_once_and_scoped_to_workspace(users, workspaces):
    first, second = workspaces
    Membership.objects.create(workspace=second, user=users[0], role=Membership.Role.MEMBER)
    client = APIClient()
    client.force_authenticate(users[0])

    issued = client.post("/api/workspaces/workshop/tokens/", {"name": "My agent"}, format="json")

    assert issued.status_code == 201
    raw_token = issued.json()["token"]
    stored = ApiToken.objects.get(workspace=first)
    assert raw_token.startswith(f"qlo_{stored.prefix}_")
    assert stored.token_hash != raw_token

    token_client = APIClient()
    token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
    assert token_client.get("/api/workspaces/workshop/locations/").status_code == 200
    assert token_client.get("/api/workspaces/library/locations/").status_code == 404
    assert token_client.get("/api/workspaces/").json()["results"][0]["slug"] == "workshop"


@pytest.mark.django_db
def test_inventory_search_uses_aliases_attributes_and_locations(users, workspaces):
    workshop, library = workspaces
    drawer = Location.objects.create(
        workspace=workshop,
        key="drawer-1-a",
        name="Drawer 1 compartment A",
        aliases=["cajón superior"],
    )
    screws = Item.objects.create(
        workspace=workshop,
        key="fix-35mm",
        name="FIX 35 mm",
        category="fasteners",
        aliases=["tornillos para madera"],
        attributes={"length_mm": 35, "material": "steel"},
        unit="piece",
    )
    Holding.objects.create(workspace=workshop, item=screws, location=drawer, quantity=Decimal("80"))
    shelf = Location.objects.create(workspace=library, key="middle-left", name="Middle left")
    book = Item.objects.create(
        workspace=library,
        key="gelman-interrupciones-1",
        name="Interrupciones I",
        category="poetry",
        attributes={"author": "Juan Gelman"},
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
    )
    Holding.objects.create(workspace=library, item=book, location=shelf, quantity=Decimal("1"))
    workshop_client = APIClient()
    workshop_client.force_authenticate(users[0])
    library_client = APIClient()
    library_client.force_authenticate(users[1])

    screws_response = workshop_client.get(
        "/api/workspaces/workshop/search/", {"q": "tornillos madera"}
    )
    book_response = library_client.get("/api/workspaces/library/search/", {"q": "Gelman"})

    assert screws_response.status_code == 200
    assert screws_response.json()["results"][0]["location_key"] == "drawer-1-a"
    assert book_response.status_code == 200
    assert book_response.json()["results"][0]["item_key"] == "gelman-interrupciones-1"


@pytest.mark.django_db
def test_inventory_search_returns_stable_ids_for_item_repairs(users, workspaces):
    _, library = workspaces
    suite = Location.objects.create(workspace=library, key="suite", name="Biblioteca de la suite")
    shelf = Location.objects.create(
        workspace=library,
        key="shelf-3-left",
        name="Estante 3 izquierda",
        parent=suite,
    )
    book = Item.objects.create(
        workspace=library,
        key="golden-boys",
        name="Golden Boys",
        category="book",
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
    )
    holding = Holding.objects.create(
        workspace=library,
        item=book,
        location=shelf,
        quantity=Decimal("1"),
    )
    client = APIClient()
    client.force_authenticate(users[1])

    response = client.get(
        "/api/workspaces/workshop/search/",
        {"q": "Golden Boys", "location": "shelf-3-left"},
    )

    assert response.status_code == 404

    response = client.get(
        "/api/workspaces/library/search/",
        {"q": "Golden Boys", "location": "shelf-3-left"},
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["item"] == str(book.id)
    assert result["id"] == str(holding.id)
    assert result["location"] == str(shelf.id)


@pytest.mark.django_db
def test_inventory_search_normalizes_ranks_partial_matches_and_explains_them(users, workspaces):
    workshop, _ = workspaces
    location = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    batteries = Item.objects.create(
        workspace=workshop,
        key="aaa-batteries",
        name="Pilas AAA",
        category="pilas",
        aliases=["pilas triple A", "baterías AAA", "AAA batteries"],
        attributes={"size": "AAA"},
    )
    Holding.objects.create(workspace=workshop, item=batteries, location=location, quantity=1)
    client = APIClient()
    client.force_authenticate(users[0])

    partial = client.get(
        "/api/workspaces/workshop/search/",
        {"q": "¿Hay PILAS baterías AAA AA?"},
    )
    exact_code = client.get("/api/workspaces/workshop/search/", {"q": "AA"})
    english = client.get("/api/workspaces/workshop/search/", {"q": "battery"})

    assert partial.status_code == 200
    result = partial.json()["results"][0]
    assert result["item_key"] == "aaa-batteries"
    assert result["search"]["match_type"] == "partial"
    assert "PILAS" in result["search"]["matched_terms"]
    assert "baterías" in result["search"]["matched_terms"]
    assert "AA?" in result["search"]["unmatched_terms"]
    assert exact_code.json()["results"] == []
    assert english.json()["results"][0]["item_key"] == "aaa-batteries"


@pytest.mark.django_db
def test_inventory_search_excludes_partial_matches_when_complete_matches_exist(users, workspaces):
    workshop, _ = workspaces
    location = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    complete_item = Item.objects.create(
        workspace=workshop,
        key="red-batteries",
        name="Red batteries",
    )
    partial_item = Item.objects.create(
        workspace=workshop,
        key="red",
        name="Red",
    )
    Holding.objects.create(workspace=workshop, item=complete_item, location=location, quantity=1)
    Holding.objects.create(workspace=workshop, item=partial_item, location=location, quantity=1)
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.get("/api/workspaces/workshop/search/", {"q": "red batteries"})

    assert response.status_code == 200
    assert [result["item_key"] for result in response.json()["results"]] == ["red-batteries"]


@pytest.mark.django_db
def test_inventory_search_tokenizes_hyphenated_keys(users, workspaces):
    workshop, _ = workspaces
    location = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    item = Item.objects.create(
        workspace=workshop,
        key="fix-screw-35mm",
        name="Fix screws 35mm",
        category="fastener",
    )
    Holding.objects.create(workspace=workshop, item=item, location=location, quantity=10)
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.get("/api/workspaces/workshop/search/", {"q": "fix-screw-35mm"})

    assert response.status_code == 200
    assert response.json()["results"][0]["item_key"] == "fix-screw-35mm"


@pytest.mark.django_db
def test_bulk_upsert_normalizes_duplicate_aliases(users, workspaces):
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/bulk-upsert/",
        {
            "idempotency_key": "normalize-aliases",
            "items": [
                {
                    "key": "aaa-batteries",
                    "name": "Pilas AAA",
                    "aliases": [" Baterías ", "baterías", "AAA batteries"],
                }
            ],
        },
        format="json",
    )

    assert response.status_code == 201
    assert Workspace.objects.get(slug="workshop").items.get(key="aaa-batteries").aliases == [
        "Baterías",
        "AAA batteries",
    ]


@pytest.mark.django_db
def test_inventory_search_scopes_to_location_descendants(users, workspaces):
    workshop, _ = workspaces
    workshop_location = Location.objects.create(workspace=workshop, key="taller", name="Taller")
    drawer = Location.objects.create(
        workspace=workshop, key="cajonera", name="Cajonera", parent=workshop_location
    )
    compartment = Location.objects.create(
        workspace=workshop, key="cajonera-a1", name="Compartimiento A1", parent=drawer
    )
    patio = Location.objects.create(workspace=workshop, key="patio", name="Patio")
    screws = Item.objects.create(workspace=workshop, key="screws", name="Tornillos")
    Holding.objects.create(
        workspace=workshop, item=screws, location=compartment, quantity=Decimal("20")
    )
    Holding.objects.create(workspace=workshop, item=screws, location=patio, quantity=Decimal("5"))
    client = APIClient()
    client.force_authenticate(users[0])

    subtree = client.get(
        "/api/workspaces/workshop/search/", {"q": "tornillos", "location": "taller"}
    )
    exact = client.get(
        "/api/workspaces/workshop/search/",
        {"q": "tornillos", "location": "taller", "include_descendants": "false"},
    )

    assert subtree.status_code == 200
    assert [row["location_key"] for row in subtree.json()["results"]] == ["cajonera-a1"]
    assert exact.status_code == 200
    assert exact.json()["results"] == []


@pytest.mark.django_db
def test_inventory_search_returns_physical_and_neighboring_clues(users, workspaces):
    _, library = workspaces
    bookcase = Location.objects.create(workspace=library, key="biblioteca", name="Biblioteca")
    shelf = Location.objects.create(
        workspace=library,
        key="estante-2-izquierda",
        name="Segundo estante a la izquierda",
        parent=bookcase,
    )
    quilombo = Item.objects.create(
        workspace=library,
        key="quilombo",
        name="Quilombo",
        description="Edición ancha con lomo rojo y letras blancas",
        aliases=["Quilombo de Samantha Schweblin"],
        attributes={
            "schema": "book",
            "appearance": {"spine_color": "red", "lettering_color": "white"},
        },
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
    )
    dolina = Item.objects.create(
        workspace=library,
        key="cronicas-angel-gris",
        name="Crónicas del Ángel Gris",
        description="Edición con lomo azul",
        attributes={"schema": "book", "appearance": {"spine_color": "blue"}},
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
    )
    Holding.objects.create(
        workspace=library,
        item=quilombo,
        location=shelf,
        quantity=1,
        notes="La copia tiene una marca en la esquina inferior",
    )
    dolina_holding = Holding.objects.create(
        workspace=library,
        item=dolina,
        location=shelf,
        quantity=1,
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=timezone.now() - timedelta(days=100),
        last_observed_by=users[1],
    )
    client = APIClient()
    client.force_authenticate(users[1])

    response = client.get("/api/workspaces/library/search/", {"q": "Quilombo"})

    result = response.json()["results"][0]
    assert response.status_code == 200
    assert result["item_description"] == "Edición ancha con lomo rojo y letras blancas"
    assert result["item_attributes"]["appearance"]["spine_color"] == "red"
    assert [location["key"] for location in result["location_path"]] == [
        "biblioteca",
        "estante-2-izquierda",
    ]
    assert len(result["nearby_items"]) == 1
    nearby = result["nearby_items"][0]
    assert nearby["holding_id"] == str(dolina_holding.id)
    assert nearby["item_key"] == "cronicas-angel-gris"
    assert nearby["description"] == "Edición con lomo azul"
    assert nearby["freshness"] == "stale"
    assert nearby["verification_status"] == VerificationStatus.CONFIRMED


@pytest.mark.django_db
def test_stock_status_reports_missing_and_low_items_within_location(users, workspaces):
    workshop, library = workspaces
    root = Location.objects.create(workspace=workshop, key="taller", name="Taller")
    drawer = Location.objects.create(workspace=workshop, parent=root, key="cajon", name="Cajón")
    shelf = Location.objects.create(workspace=workshop, parent=root, key="estante", name="Estante")
    screws = Item.objects.create(
        workspace=workshop,
        key="tornillos",
        name="Tornillos",
        unit="units",
        minimum_quantity=Decimal("10"),
        target_quantity=Decimal("25"),
    )
    Item.objects.create(
        workspace=workshop,
        key="guantes",
        name="Guantes",
        unit="pairs",
        minimum_quantity=Decimal("2"),
    )
    batteries = Item.objects.create(
        workspace=workshop,
        key="pilas",
        name="Pilas",
        minimum_quantity=Decimal("1"),
    )
    Item.objects.create(
        workspace=library,
        key="papel",
        name="Papel",
        minimum_quantity=Decimal("100"),
    )
    Holding.objects.create(workspace=workshop, item=screws, location=drawer, quantity=Decimal("3"))
    Holding.objects.create(workspace=workshop, item=screws, location=shelf, quantity=Decimal("1"))
    Holding.objects.create(
        workspace=workshop, item=batteries, location=drawer, quantity=Decimal("5")
    )
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.get("/api/workspaces/workshop/stock-status/")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "item_key": "guantes",
            "item_name": "Guantes",
            "status": "missing",
            "current_quantity": "0.000000",
            "minimum_quantity": "2.000000",
            "target_quantity": "2.000000",
            "recommended_add_quantity": "2.000000",
            "unit": "pairs",
            "locations": [],
        },
        {
            "item_key": "tornillos",
            "item_name": "Tornillos",
            "status": "low",
            "current_quantity": "4.000000",
            "minimum_quantity": "10.000000",
            "target_quantity": "25.000000",
            "recommended_add_quantity": "21.000000",
            "unit": "units",
            "locations": [
                {
                    "location_key": "cajon",
                    "location_name": "Cajón",
                    "quantity": "3.000000",
                },
                {
                    "location_key": "estante",
                    "location_name": "Estante",
                    "quantity": "1.000000",
                },
            ],
        },
    ]
    assert client.get("/api/workspaces/library/stock-status/").status_code == 404


@pytest.mark.django_db
def test_item_rejects_target_below_minimum(users, workspaces):
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/items/",
        {
            "key": "guantes",
            "name": "Guantes",
            "minimum_quantity": "5",
            "target_quantity": "2",
        },
    )

    assert response.status_code == 400
    assert "target_quantity" in response.json()


@pytest.mark.django_db
def test_item_api_book_schema_sets_item_defaults(users, workspaces):
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.post(
        "/api/workspaces/workshop/items/",
        {
            "key": "matilda",
            "name": "Matilda",
            "category": "libros",
            "attributes": {},
            "tracking_mode": Item.TrackingMode.BULK,
            "unit": "unit",
        },
        format="json",
    )

    item = workspaces[0].items.get(key="matilda")
    assert response.status_code == 201
    assert item.attributes == {"schema": "book"}
    assert item.tracking_mode == Item.TrackingMode.DISCRETE
    assert item.unit == "copy"


@pytest.mark.django_db
def test_book_lookup_normalizes_open_library_metadata_and_is_tenant_scoped(users, workspaces):
    cache.clear()
    payload = {
        "ISBN:9780140328721": {
            "url": "https://openlibrary.org/books/OL7353617M/Matilda",
            "title": "Matilda",
            "description": {"value": "A clever girl outwits a cruel headmistress."},
            "authors": [{"name": "Roald Dahl"}],
            "publishers": [{"name": "Puffin"}],
            "publish_date": "1988",
            "number_of_pages": 240,
            "identifiers": {"isbn_13": ["9780140328721"]},
            "cover": {"medium": "https://covers.openlibrary.org/example.jpg"},
        }
    }
    client = APIClient()
    client.force_authenticate(users[0])

    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(payload).encode()),
    ) as urlopen_mock:
        response = client.get("/api/workspaces/workshop/catalog/books/978-0-14-032872-1/")
        cached_response = client.get("/api/workspaces/workshop/catalog/books/9780140328721/")

    assert response.status_code == 200
    assert cached_response.status_code == 200
    assert urlopen_mock.call_count == 1
    result = response.json()
    assert result["provider"] == "open_library"
    assert result["suggested_item"]["name"] == "Matilda"
    assert result["suggested_item"]["description"] == (
        "A clever girl outwits a cruel headmistress."
    )
    assert result["suggested_item"]["attributes"]["book"]["synopsis"] == (
        "A clever girl outwits a cruel headmistress."
    )
    assert result["retrieved_at"]
    assert result["suggested_item"]["attributes"]["book"]["authors"] == ["Roald Dahl"]

    inaccessible = client.get("/api/workspaces/library/catalog/books/9780140328721/")
    invalid = client.get("/api/workspaces/workshop/catalog/books/9780140328722/")
    assert inaccessible.status_code == 404
    assert invalid.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [None, {"ISBN:9780140328721": []}],
)
def test_book_lookup_rejects_structurally_invalid_catalog_payload(payload):
    from inventory.catalogs import CatalogLookupError, lookup_book_by_isbn

    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(payload).encode()),
    ):
        with pytest.raises(CatalogLookupError, match="invalid response"):
            lookup_book_by_isbn("9780140328721")


@pytest.mark.django_db
def test_book_catalog_expands_work_editions_into_specific_candidates():
    from inventory.catalogs import search_books

    search_payload = {
        "docs": [
            {
                "title": "Matilda",
                "author_name": ["Roald Dahl"],
                "publisher": ["Puffin", "Ace"],
                "edition_key": ["OL111M", "OL222M"],
                "isbn": ["9780140328721", "9780439023481"],
            }
        ]
    }
    edition_payloads = [
        {
            "title": "Matilda",
            "publishers": ["Puffin"],
            "publish_date": "1988",
            "number_of_pages": 240,
            "identifiers": {"isbn_13": ["9780140328721"]},
            "covers": [111],
        },
        {
            "title": "Matilda",
            "publishers": ["Ace"],
            "publish_date": "2000",
            "number_of_pages": 256,
            "identifiers": {"isbn_13": ["9780439023481"]},
            "covers": [222],
        },
    ]

    cache.clear()
    responses = [
        io.BytesIO(json.dumps(search_payload).encode()),
        *(io.BytesIO(json.dumps(payload).encode()) for payload in edition_payloads),
    ]
    with patch("inventory.catalogs.urlopen", side_effect=responses) as urlopen_mock:
        result = search_books(title="Matilda", authors=["Roald Dahl"])

    assert [candidate["openlibrary_edition"] for candidate in result["candidates"]] == [
        "OL111M",
        "OL222M",
    ]
    assert [candidate["isbn"] for candidate in result["candidates"]] == [
        ["9780140328721"],
        ["9780439023481"],
    ]
    assert [candidate["publishers"] for candidate in result["candidates"]] == [
        ["Puffin"],
        ["Ace"],
    ]
    assert result["candidates"][0]["cover_url"].endswith("/111-M.jpg")
    assert urlopen_mock.call_count == 3
    assert urlopen_mock.call_args_list[1].args[0].full_url.endswith("/books/OL111M.json")
    assert urlopen_mock.call_args_list[2].args[0].full_url.endswith("/books/OL222M.json")


@pytest.mark.django_db
def test_book_catalog_looks_up_multiple_isbns_in_batches_and_reports_missing_records():
    from inventory.catalogs import lookup_books_by_isbn

    payload = {
        "ISBN:9780140328721": {
            "title": "Matilda",
            "authors": [{"name": "Roald Dahl"}],
            "publishers": [{"name": "Puffin"}],
            "identifiers": {"isbn_13": ["9780140328721"]},
        }
    }

    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(payload).encode()),
    ) as urlopen_mock:
        result = lookup_books_by_isbn(["9780140328721", "9780439023481", "9780140328721"])

    assert result["requested"] == ["9780140328721", "9780439023481"]
    assert result["duplicates"] == ["9780140328721"]
    assert result["results"][0]["status"] == "found"
    assert result["results"][0]["details"]["title"] == "Matilda"
    assert result["results"][1] == {
        "isbn": "9780439023481",
        "status": "not_found",
        "message": "No Open Library record was found for that ISBN.",
    }
    assert urlopen_mock.call_count == 1
    requested_bibkeys = parse_qs(urlopen_mock.call_args.args[0].full_url.split("?", 1)[1])[
        "bibkeys"
    ][0]
    assert requested_bibkeys == "ISBN:9780140328721,ISBN:9780439023481"


@pytest.mark.django_db
def test_public_signup_requires_email_verification_before_login(client):
    response = client.post(
        "/accounts/signup/",
        {
            "username": "new-user",
            "email": "new-user@example.com",
            "password1": "correct-horse-battery-staple-917",
            "password2": "correct-horse-battery-staple-917",
        },
    )

    assert response.status_code == 302
    assert response.url == "/accounts/confirm-email/"
    assert client.session.get("_auth_user_id") is None
    user = get_user_model().objects.get(username="new-user")
    assert user.email == "new-user@example.com"
    email_address = user.emailaddress_set.get()
    assert email_address.email == "new-user@example.com"
    assert email_address.verified is False
    workspace = user.workspaces.get()
    assert workspace.name == "Home"
    assert workspace.slug == f"home-{str(user.id)[:8]}"
    assert workspace.memberships.get(user=user).role == Membership.Role.OWNER
    assert len(mail.outbox) == 1
    verification_page = client.get(response.url)
    verification_content = verification_page.content.decode()
    assert verification_page.status_code == 200
    assert 'href="/static/inventory/styles.css"' in verification_content
    assert "Mensajes:" not in verification_content
    assert "Has iniciado sesión" not in verification_content
    assert mail.outbox[0].subject.endswith("Confirmá tu dirección de correo electrónico | Quilombo")
    assert "¡Hola de parte de Quilombo!" in mail.outbox[0].body
    assert "registrar una cuenta en Quilombo" in mail.outbox[0].body
    assert "Hello from quilombo.life" not in mail.outbox[0].body

    confirmation_line = next(
        line.strip()
        for line in mail.outbox[0].body.splitlines()
        if "/accounts/confirm-email/" in line
    )
    confirmation_url = confirmation_line[confirmation_line.index("http") :]
    confirmation_response = client.post(urlsplit(confirmation_url).path)

    email_address.refresh_from_db()
    assert confirmation_response.status_code == 302
    assert confirmation_response.url == "/accounts/login/"
    assert email_address.verified is True
    assert client.session.get("_auth_user_id") is None

    login_response = client.post(
        "/accounts/login/",
        {"login": user.email, "password": "correct-horse-battery-staple-917"},
    )

    assert login_response.status_code == 302
    assert login_response.url == "/app/"
    assert client.session.get("_auth_user_id") == str(user.id)


@pytest.mark.django_db
def test_public_signup_requires_email_and_matching_passwords(client):
    missing_email = client.post(
        "/accounts/signup/",
        {
            "username": "missing-email",
            "password1": "correct-horse-battery-staple-917",
            "password2": "correct-horse-battery-staple-917",
        },
    )
    mismatched_passwords = client.post(
        "/accounts/signup/",
        {
            "username": "mismatched-passwords",
            "email": "mismatched@example.com",
            "password1": "correct-horse-battery-staple-917",
            "password2": "different-horse-battery-staple-917",
        },
    )

    assert missing_email.status_code == 200
    assert "email" in missing_email.context["form"].errors
    assert mismatched_passwords.status_code == 200
    assert "password2" in mismatched_passwords.context["form"].errors
    assert (
        not get_user_model()
        .objects.filter(username__in=["missing-email", "mismatched-passwords"])
        .exists()
    )


@pytest.mark.django_db
@pytest.mark.parametrize("identifier", ["password-user", "password-user@example.com"])
def test_password_user_can_log_in_with_username_or_email(client, identifier):
    user = get_user_model().objects.create_user(
        username="password-user",
        email="password-user@example.com",
        password="correct-horse-battery-staple-917",
    )
    user.emailaddress_set.create(email=user.email, verified=True, primary=True)

    response = client.post(
        "/accounts/login/",
        {"login": identifier, "password": "correct-horse-battery-staple-917"},
    )

    assert response.status_code == 302
    assert response.url == "/app/"
    assert client.session.get("_auth_user_id") is not None


@pytest.mark.django_db
def test_password_reset_flow_sends_email_and_changes_password(client):
    user = get_user_model().objects.create_user(
        username="reset-user",
        email="reset-user@example.com",
        password="old-password-917",
    )
    user.emailaddress_set.create(email=user.email, verified=True, primary=True)

    login_page = client.get("/accounts/login/")
    assert 'href="/accounts/password/reset/"' in login_page.content.decode()

    requested = client.post(
        "/accounts/password/reset/",
        {"email": user.email},
    )

    assert requested.status_code == 302
    assert requested.url == "/accounts/password/reset/done/"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject.endswith("Restablecé tu contraseña | Quilombo")
    reset_url = next(
        line.strip()
        for line in mail.outbox[0].body.splitlines()
        if "/accounts/password/reset/key/" in line
    )

    reset_page = client.get(urlsplit(reset_url).path, follow=True)
    assert reset_page.status_code == 200
    assert "Elegí una contraseña nueva" in reset_page.content.decode()
    reset_path = reset_page.redirect_chain[-1][0]

    completed = client.post(
        reset_path,
        {
            "password1": "new-password-918",
            "password2": "new-password-918",
        },
    )

    assert completed.status_code == 302
    assert completed.url == "/accounts/password/reset/key/done/"
    login = client.post(
        "/accounts/login/",
        {"login": user.email, "password": "new-password-918"},
    )
    assert login.status_code == 302
    assert login.url == "/app/"


@pytest.mark.django_db
def test_admin_dashboard_shows_recent_users_items_and_locations(client):
    admin_user = get_user_model().objects.create_superuser(
        username="admin", email="admin@example.com", password="password"
    )
    recent_user = get_user_model().objects.create_user(username="recent-user")
    old_user = get_user_model().objects.create_user(username="old-user")
    get_user_model().objects.filter(pk=old_user.pk).update(
        date_joined=timezone.now() - timedelta(days=8)
    )
    workspace = Workspace.objects.create(name="Admin test", slug="admin-test")
    recent_item = Item.objects.create(workspace=workspace, key="recent-item", name="Recent item")
    old_item = Item.objects.create(workspace=workspace, key="old-item", name="Old item")
    recent_location = Location.objects.create(
        workspace=workspace, key="recent-location", name="Recent location"
    )
    old_location = Location.objects.create(
        workspace=workspace, key="old-location", name="Old location"
    )
    old_date = timezone.now() - timedelta(days=8)
    Item.objects.filter(pk=old_item.pk).update(created_at=old_date)
    Location.objects.filter(pk=old_location.pk).update(created_at=old_date)
    AccessEvent.objects.create(
        user=recent_user, channel=AccessEvent.Channel.MCP, client_name="Claude"
    )

    client.force_login(admin_user)
    response = client.get("/admin/")
    content = response.content.decode()

    assert response.status_code == 200
    assert AccessEvent.objects.filter(user=admin_user, channel=AccessEvent.Channel.WEB).exists()
    assert "Last 7 days" in content
    assert recent_user.username in content
    assert recent_item.name in content
    assert recent_location.name in content
    assert "Web logins" in content
    assert "MCP logins" in content
    assert "Claude" in content
    assert old_user.username not in content
    assert old_item.name not in content
    assert old_location.name not in content


@pytest.mark.django_db
def test_admin_list_views_support_operational_search_without_exposing_tokens(client):
    from allauth.socialaccount.models import SocialAccount, SocialToken
    from django.contrib import admin

    admin_user = get_user_model().objects.create_superuser(
        username="admin-lists", email="admin-lists@example.com", password="password"
    )
    workspace = Workspace.objects.create(name="Workshop", slug="admin-lists-workshop")
    Membership.objects.create(workspace=workspace, user=admin_user, role=Membership.Role.OWNER)
    location = Location.objects.create(workspace=workspace, key="drawer", name="Tool drawer")
    item = Item.objects.create(
        workspace=workspace,
        key="fix-35",
        name="FIX screws",
        category="fasteners",
    )
    Holding.objects.create(workspace=workspace, item=item, location=location, quantity=12)
    InventoryEvent.objects.create(
        workspace=workspace,
        actor=admin_user,
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        source_kind=InventoryEvent.SourceKind.MANUAL,
        client_actor="web",
        summary={"item_id": str(item.id), "item_key": item.key, "item_fields": ["name"]},
    )
    token, raw_token = ApiToken.issue(workspace=workspace, user=admin_user, name="Agent token")
    SocialAccount.objects.create(user=admin_user, provider="github", uid="github-123")
    client.force_login(admin_user)

    item_list = client.get("/admin/inventory/item/", {"q": "fasteners"})
    event_list = client.get("/admin/inventory/inventoryevent/", {"q": "web"})
    workspace_list = client.get("/admin/inventory/workspace/")
    user_list = client.get("/admin/auth/user/", {"q": "admin-lists@example.com"})
    social_list = client.get("/admin/socialaccount/socialaccount/", {"q": "github-123"})
    token_list = client.get("/admin/inventory/apitoken/", {"q": token.prefix})

    assert item_list.status_code == 200
    assert item.name in item_list.content.decode()
    assert "fasteners" in item_list.content.decode()
    assert event_list.status_code == 200
    assert "web" in event_list.content.decode()
    assert workspace_list.status_code == 200
    workspace_row = next(
        row for row in workspace_list.context["cl"].result_list if row.pk == workspace.pk
    )
    assert workspace_row.member_count == 1
    assert workspace_row.item_count == 1
    assert workspace_row.event_count == 1
    assert user_list.status_code == 200
    assert admin_user.email in user_list.content.decode()
    assert social_list.status_code == 200
    assert "github-123" in social_list.content.decode()
    assert token_list.status_code == 200
    assert token.prefix in token_list.content.decode()
    assert raw_token not in token_list.content.decode()
    assert token.token_hash not in token_list.content.decode()
    assert SocialToken not in admin.site._registry


@pytest.mark.django_db
def test_admin_login_uses_quilombo_authentication_and_preserves_next(client):
    from allauth.account.models import EmailAddress

    admin_user = get_user_model().objects.create_superuser(
        username="admin-login",
        email="admin-login@example.com",
        password="correct-horse-battery-staple-917",
    )
    EmailAddress.objects.create(
        user=admin_user,
        email=admin_user.email,
        verified=True,
        primary=True,
    )

    response = client.get("/admin/")

    assert response.status_code == 302
    assert response.url == "/accounts/login/?next=/admin/"
    assert client.get("/admin/login/?next=/admin/").url == "/accounts/login/?next=/admin/"

    login_page = client.get(response.url)
    content = login_page.content.decode()
    assert login_page.status_code == 200
    assert 'href="/static/inventory/styles.css"' in content
    assert '<input type="hidden" name="next" value="/admin/">' in content

    login_response = client.post(
        "/accounts/login/",
        {
            "login": admin_user.username,
            "password": "correct-horse-battery-staple-917",
            "next": "/admin/",
        },
    )

    assert login_response.status_code == 302
    assert login_response.url == "/admin/"


@pytest.mark.django_db
def test_authenticated_home_redirects_to_dashboard(client, users):
    client.force_login(users[0])

    response = client.get("/")

    assert response.status_code == 302
    assert response.url == "/app/"


@pytest.mark.django_db
def test_item_update_history_links_item_in_same_workspace(client, users, workspaces):
    workspace, other_workspace = workspaces
    item = Item.objects.create(workspace=workspace, key="drill", name="6 mm drill bit")
    other_item = Item.objects.create(workspace=other_workspace, key="book", name="The Aleph")
    InventoryEvent.objects.create(
        workspace=workspace,
        actor=users[0],
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        summary={"item_id": str(item.id), "item_key": item.key, "item_fields": ["name"]},
    )
    InventoryEvent.objects.create(
        workspace=workspace,
        actor=users[0],
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        summary={"item_id": str(other_item.id), "item_key": other_item.key, "item_fields": []},
    )
    client.force_login(users[0])

    content = client.get("/app/workshop/history/").content.decode()

    assert f'href="/app/workshop/items/{item.id}/">{item.name}</a>' in content
    assert f'Objeto: <a href="/app/workshop/items/{item.id}/">' in content
    assert f'href="/app/library/items/{other_item.id}/"' not in content
    assert other_item.name not in content


def test_search_cursor_keeps_sqlite_candidate_window_stable(users, workspaces):
    from inventory import services

    with (
        patch.object(services.connection, "vendor", "sqlite"),
        patch.object(services, "_candidate_holdings", return_value=[]) as candidates,
    ):
        services.search_holdings(workspace=workspaces[0], query="screw", limit=2, offset=0)
        services.search_holdings(workspace=workspaces[0], query="screw", limit=2, offset=2)

    assert [call.args[2] for call in candidates.call_args_list] == [5000, 5000]


@pytest.mark.django_db
def test_admin_dashboard_hides_models_without_view_permission(client):
    staff_user = get_user_model().objects.create_user(username="limited-staff", is_staff=True)
    workspace = Workspace.objects.create(name="Private workspace", slug="private-workspace")
    Item.objects.create(workspace=workspace, key="private-item", name="Private item")
    Location.objects.create(workspace=workspace, key="private-location", name="Private location")
    staff_user.user_permissions.add(
        Permission.objects.get(codename="view_item", content_type__app_label="inventory")
    )

    client.force_login(staff_user)
    content = client.get("/admin/").content.decode()

    assert "Objects" in content
    assert "Private item" in content
    assert "Users" not in content
    assert "Locations" not in content
    assert "Private location" not in content
    assert "Web logins" not in content
    assert "MCP logins" not in content


@pytest.mark.django_db
@override_settings(SOCIALACCOUNT_PROVIDERS=SOCIAL_PROVIDER_SETTINGS)
def test_google_and_github_login_are_post_actions_when_configured(client):
    login_content = client.get("/accounts/login/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
    signup_content = client.get("/accounts/signup/", HTTP_ACCEPT_LANGUAGE="en").content.decode()

    assert 'method="post" action="/accounts/google/login/?process=login"' in login_content
    assert 'method="post" action="/accounts/github/login/?process=login"' in login_content
    assert "Continue with Google" in login_content
    assert "Continue with GitHub" in signup_content
    assert 'class="social-icon google-icon"' in login_content
    assert 'class="social-icon github-icon"' in signup_content

    google = client.post("/accounts/google/login/")
    github = client.post("/accounts/github/login/")

    assert google.status_code == 302
    assert google.url.startswith("https://accounts.google.com/")
    assert github.status_code == 302
    assert github.url.startswith("https://github.com/")


@pytest.mark.django_db
@override_settings(SOCIALACCOUNT_PROVIDERS=SOCIAL_PROVIDER_SETTINGS)
def test_auth_pages_preserve_pending_oauth_consent_return(client):
    consent_url = "/oauth/consent/?request=12345678-1234-1234-1234-123456789abc"

    login = client.get("/accounts/login/", {"next": consent_url})
    login_content = login.content.decode()

    assert (
        'action="/accounts/google/login/?process=login&amp;next=%2Foauth%2Fconsent%2F%3Frequest%3D'
        '12345678-1234-1234-1234-123456789abc"' in login_content
    )
    assert f'<input type="hidden" name="next" value="{consent_url}">' in login_content
    assert (
        'href="/accounts/signup/?next=/oauth/consent/%3Frequest%3D'
        '12345678-1234-1234-1234-123456789abc"' in login_content
    )

    signup = client.get("/accounts/signup/", {"next": consent_url})
    signup_content = signup.content.decode()

    assert (
        'action="/accounts/google/login/?process=login&amp;next=%2Foauth%2Fconsent%2F%3Frequest%3D'
        '12345678-1234-1234-1234-123456789abc"' in signup_content
    )
    assert f'<input type="hidden" name="next" value="{consent_url}">' in signup_content
    assert (
        'href="/accounts/login/?next=/oauth/consent/%3Frequest%3D'
        '12345678-1234-1234-1234-123456789abc"' in signup_content
    )


@pytest.mark.django_db
def test_social_login_buttons_are_hidden_without_provider_credentials(client):
    content = client.get("/accounts/login/").content.decode()

    assert "/accounts/google/login/" not in content
    assert "/accounts/github/login/" not in content


@pytest.mark.django_db
def test_social_signup_creates_the_private_home_workspace():
    from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

    from .accounts import QuilomboSocialAccountAdapter

    user = get_user_model().objects.create_user(username="social-user")
    adapter = QuilomboSocialAccountAdapter()
    with patch.object(DefaultSocialAccountAdapter, "save_user", return_value=user):
        saved_user = adapter.save_user(None, object())

    assert saved_user == user
    workspace = user.workspaces.get()
    assert workspace.name == "Home"
    assert workspace.memberships.get(user=user).role == Membership.Role.OWNER


@pytest.mark.django_db
@override_settings(SOCIALACCOUNT_PROVIDERS=SOCIAL_PROVIDER_SETTINGS)
def test_new_google_signup_returns_to_pending_oauth_consent():
    from allauth.account.models import EmailAddress
    from allauth.core import context
    from allauth.socialaccount.adapter import get_adapter
    from allauth.socialaccount.internal.flows.login import complete_login
    from allauth.socialaccount.models import SocialAccount, SocialLogin
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.messages.middleware import MessageMiddleware
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    consent_url = "/oauth/consent/?request=12345678-1234-1234-1234-123456789abc"
    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda request: None).process_request(request)
    MessageMiddleware(lambda request: None).process_request(request)
    request.session.save()
    request.user = AnonymousUser()
    provider = get_adapter(request).get_provider(request, "google")
    sociallogin = SocialLogin(
        user=get_user_model()(username="new-google-user", email="new-google@example.com"),
        account=SocialAccount(provider="google", uid="new-google-uid"),
        email_addresses=[EmailAddress(email="new-google@example.com", verified=True, primary=True)],
        provider=provider,
    )
    sociallogin.state = {"process": "login", "next": consent_url}

    with context.request_context(request):
        response = complete_login(request, sociallogin)

    assert response.status_code == 302
    assert response.url == consent_url
    assert sociallogin.user.pk is not None
    assert sociallogin.user.workspaces.get().name == "Home"


@pytest.mark.django_db
@override_settings(SOCIALACCOUNT_PROVIDERS=SOCIAL_PROVIDER_SETTINGS)
def test_verified_google_email_reuses_and_connects_password_user():
    from allauth.account.models import EmailAddress
    from allauth.core import context
    from allauth.socialaccount.adapter import get_adapter
    from allauth.socialaccount.internal.flows.login import complete_login
    from allauth.socialaccount.models import SocialAccount, SocialLogin
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.messages.middleware import MessageMiddleware
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    from .accounts import ensure_home_workspace

    user = get_user_model().objects.create_user(
        username="existing-user",
        email="existing@example.com",
        password="correct-horse-battery-staple-917",
    )
    EmailAddress.objects.create(
        user=user,
        email=user.email,
        verified=True,
        primary=True,
    )
    ensure_home_workspace(user)
    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda request: None).process_request(request)
    MessageMiddleware(lambda request: None).process_request(request)
    request.session.save()
    request.user = AnonymousUser()
    provider = get_adapter(request).get_provider(request, "google")
    sociallogin = SocialLogin(
        user=get_user_model()(username="google-profile", email=user.email),
        account=SocialAccount(provider="google", uid="google-uid"),
        email_addresses=[EmailAddress(email=user.email, verified=True, primary=True)],
        provider=provider,
    )
    sociallogin.state = {"process": "login"}

    with context.request_context(request):
        response = complete_login(request, sociallogin)

    user.refresh_from_db()
    assert response.status_code == 302
    assert response.url == "/app/"
    assert sociallogin.user == user
    assert user.has_usable_password()
    assert user.workspaces.count() == 1
    assert SocialAccount.objects.get(provider="google", uid="google-uid").user == user


@pytest.mark.django_db
def test_public_home_and_connector_guide(client):
    home_response = client.get("/")
    connector_response = client.get("/connect/")
    privacy_response = client.get("/privacy/")
    terms_response = client.get("/terms/")
    login_response = client.get("/accounts/login/")
    signup_response = client.get("/accounts/signup/")

    assert home_response.status_code == 200
    assert "Una memoria para las cosas que te rodean." in home_response.content.decode()
    assert "La vida real es un quilombo." in home_response.content.decode()
    assert "bisagras" not in home_response.content.decode()
    assert "inventory/home/workshop-es.webp" in home_response.content.decode()
    assert "inventory/home/workshop-es-mobile.webp" in home_response.content.decode()
    assert "inventory/home/library-es.webp" in home_response.content.decode()
    assert "inventory/home/library-es-mobile.webp" in home_response.content.decode()
    assert "inventory/home/moving-es.webp" in home_response.content.decode()
    assert "inventory/home/moving-es-mobile.webp" in home_response.content.decode()
    assert home_response.content.decode().count('data-carousel-index="') == 3
    assert "data-carousel-prev" not in home_response.content.decode()
    assert "data-carousel-next" not in home_response.content.decode()
    assert "data-carousel-toggle" in home_response.content.decode()
    assert '<meta name="twitter:card" content="summary_large_image">' in (
        home_response.content.decode()
    )
    assert (
        '<meta property="og:image" '
        'content="http://localhost:8000/static/inventory/home/workshop-es-social.jpg">'
        in home_response.content.decode()
    )
    assert (
        "Quilombo es un sistema de gestión de inventario agéntico."
        in home_response.content.decode()
    )
    assert "Quilombo guarda hechos" not in home_response.content.decode()
    assert connector_response.status_code == 200
    assert privacy_response.status_code == 200
    assert "Política de privacidad" in privacy_response.content.decode()
    assert "no sube ni procesa fotos o videos" in privacy_response.content.decode()
    assert terms_response.status_code == 200
    assert "Términos de servicio" in terms_response.content.decode()
    assert "http://localhost:8000/mcp" in connector_response.content.decode()
    assert "ChatGPT" in connector_response.content.decode()
    assert "codex mcp add quilombo --url http://localhost:8000/mcp" in (
        connector_response.content.decode()
    )
    assert "codex mcp login quilombo" in connector_response.content.decode()
    assert "opencode mcp add" in connector_response.content.decode()
    assert "opencode mcp auth quilombo" in connector_response.content.decode()
    assert "opencode mcp list" in connector_response.content.decode()
    assert (
        "npx skills add mgaitan/quilombo --skill manage-quilombo-inventory -g"
        in connector_response.content.decode()
    )
    assert "Claude" in connector_response.content.decode()
    assert login_response.status_code == 200
    assert signup_response.status_code == 200


@pytest.mark.django_db
def test_web_detects_english_and_spanish_from_accept_language(client):
    english = client.get("/", HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
    spanish = client.get("/", HTTP_ACCEPT_LANGUAGE="es-AR,es;q=0.9")

    english_content = english.content.decode()
    spanish_content = spanish.content.decode()
    assert '<html lang="en">' in english_content
    assert "A memory for the things around you." in english_content
    assert "Real life is messy." in english_content
    assert "Let AI help a little." in english_content
    assert "inventory/home/workshop-en.webp" in english_content
    assert "inventory/home/workshop-en-mobile.webp" in english_content
    assert "inventory/home/library-en.webp" in english_content
    assert "inventory/home/moving-en.webp" in english_content
    assert "inventory/home/moving-en-mobile.webp" in english_content
    assert "http://localhost:8000/static/inventory/home/workshop-en-social.jpg" in english_content
    assert "Quilombo is an agentic inventory management system." in english_content
    assert "Tell or show your AI agent" in english_content
    assert "Later, ask where that 6 mm drill bit, a book, or the forks ended up." in english_content
    assert "Use Quilombo as a classic web app" in english_content
    assert "Describe or show" in english_content
    assert "send it a photo" in english_content
    assert "suggest better places for things" in english_content
    assert "hinges" not in english_content
    assert "Create account" in english_content
    assert '<html lang="es">' in spanish_content
    assert "Una memoria para las cosas que te rodean." in spanish_content
    assert "Crear cuenta" in spanish_content


@pytest.mark.django_db
def test_about_link_and_dictionary_entry_follow_language(client):
    english = client.get("/", HTTP_ACCEPT_LANGUAGE="en-US")
    english_content = english.content.decode()

    assert (
        "https://mgaitan.github.io/en/posts/quilombo-agents-to-organize-real-life/"
        in english_content
    )
    assert "noun · lunfardo" in english_content
    assert "a mess, a chaotic tangle" in english_content

    spanish = client.get("/", HTTP_ACCEPT_LANGUAGE="es-AR")
    spanish_content = spanish.content.decode()

    assert (
        "https://mgaitan.github.io/posts/quilombo-agentes-para-organizar-la-vida-real/"
        in spanish_content
    )
    assert "sustantivo · lunfardo" in spanish_content
    assert "un lío, un enredo caótico" in spanish_content


@pytest.mark.django_db
def test_manual_language_switch_persists_choice(client):
    switched = client.post(
        "/i18n/setlang/",
        {"language": "en", "next": "/connect/"},
    )

    assert switched.status_code == 302
    assert switched.url == "/connect/"
    assert switched.cookies["django_language"].value == "en"

    english = client.get("/connect/", HTTP_ACCEPT_LANGUAGE="es")
    content = english.content.decode()
    assert '<html lang="en">' in content
    assert "Connect an agent" in content
    assert 'value="en" aria-current="true"' in content

    client.post("/i18n/setlang/", {"language": "es", "next": "/"})
    spanish = client.get("/", HTTP_ACCEPT_LANGUAGE="en")
    assert "Una memoria para las cosas que te rodean." in spanish.content.decode()


@pytest.mark.django_db
def test_oauth_consent_explains_missing_workspace(client, users):
    oauth_client = OAuthClient.objects.create(
        client_id="consent-test-client",
        metadata={"client_name": "Consent test", "redirect_uris": ["https://example.com/cb"]},
    )
    authorization_request = OAuthAuthorizationRequest.objects.create(
        client=oauth_client,
        code_challenge="challenge",
        redirect_uri="https://example.com/cb",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    client.force_login(users[0])

    response = client.get("/oauth/consent/", {"request": authorization_request.id})

    assert response.status_code == 400
    assert "no tiene un inventario" in response.content.decode()


def test_skill_zip_is_downloadable(client):
    response = client.get("/skills/manage-quilombo-inventory.zip")
    archive = ZipFile(io.BytesIO(b"".join(response.streaming_content)))

    assert response.status_code == 200
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "manage-quilombo-inventory/SKILL.md" in archive.namelist()
    assert "manage-quilombo-inventory/agents/openai.yaml" in archive.namelist()


@pytest.mark.django_db
def test_dashboard_requires_login_and_only_lists_member_workspaces(client, users, workspaces):
    workshop, library = workspaces

    anonymous = client.get("/app/")
    client.force_login(users[0])
    authenticated = client.get("/app/")

    assert anonymous.status_code == 302
    assert anonymous.url.startswith("/accounts/login/")
    assert authenticated.status_code == 200
    assert workshop.name in authenticated.content.decode()
    assert library.name not in authenticated.content.decode()
    assert f"/app/{workshop.slug}/first-inventory/" in authenticated.content.decode()
    dashboard_workspace = authenticated.context["workspaces"][0]
    assert dashboard_workspace.location_count == workshop.locations.count()
    assert dashboard_workspace.item_count == workshop.items.count()
    assert dashboard_workspace.holding_count == workshop.holdings.count()

    guide = client.get(f"/app/{workshop.slug}/first-inventory/")
    other_workspace = client.get(f"/app/{library.slug}/first-inventory/")

    assert guide.status_code == 200
    assert "una zona a la vez" in guide.content.decode()
    assert other_workspace.status_code == 404


@pytest.mark.django_db
def test_owner_creates_renames_and_shares_workspace(client, users, workspaces):
    client.force_login(users[0])

    created = client.post("/app/new/", {"name": "Casa nueva"})
    workspace = Workspace.objects.get(name="Casa nueva")
    original_slug = workspace.slug
    assert created.status_code == 302
    assert created.url == f"/app/{workspace.slug}/"
    assert workspace.memberships.get(user=users[0]).role == Membership.Role.OWNER

    renamed = client.post(
        f"/app/{workspace.slug}/settings/",
        {"name": "Casa ordenada"},
    )
    shared = client.post(
        f"/app/{workspace.slug}/members/",
        {"username": users[1].username, "can_write": "on"},
    )
    workspace.refresh_from_db()
    membership = workspace.memberships.get(user=users[1])

    assert renamed.status_code == 302
    assert shared.status_code == 302
    assert workspace.name == "Casa ordenada"
    assert workspace.slug == original_slug
    assert membership.can_write is True

    updated = client.post(
        f"/app/{workspace.slug}/members/{users[1].id}/",
        {},
    )
    membership.refresh_from_db()
    assert updated.status_code == 302
    assert membership.can_write is False


@pytest.mark.django_db
def test_read_only_access_can_read_but_cannot_mutate(users, workspaces):
    workspace, _ = workspaces
    Membership.objects.create(workspace=workspace, user=users[1], can_write=False)
    Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    client = APIClient()
    client.force_authenticate(users[1])

    assert client.get("/api/workspaces/workshop/locations/").status_code == 200
    denied = client.post(
        "/api/workspaces/workshop/items/",
        {"key": "book", "name": "Book", "unit": "item"},
        format="json",
    )
    assert denied.status_code == 403

    _, raw_token = ApiToken.issue(
        workspace=workspace,
        user=users[0],
        name="Read-only agent",
        can_write=False,
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
    assert client.get("/api/workspaces/workshop/locations/").status_code == 200
    denied = client.post(
        "/api/workspaces/workshop/items/",
        {"key": "agent-book", "name": "Agent book", "unit": "item"},
        format="json",
    )
    assert denied.status_code == 403


@pytest.mark.django_db
def test_oauth_consent_records_read_only_choice(client, users, workspaces):
    workspace, _ = workspaces
    oauth_client = OAuthClient.objects.create(
        client_id="read-only-client",
        metadata={"client_name": "Read-only client"},
    )
    authorization_request = OAuthAuthorizationRequest.objects.create(
        client=oauth_client,
        code_challenge="challenge",
        redirect_uri="https://example.com/callback",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    client.force_login(users[0])

    response = client.post(
        "/oauth/consent/",
        {
            "request": authorization_request.id,
            "workspace": workspace.id,
            "action": "allow",
        },
    )

    assert response.status_code == 302
    assert OAuthAuthorizationGrant.objects.get(client=oauth_client).can_write is False


@pytest.mark.django_db
def test_oauth_access_tokens_enforce_expiry_revocation_and_workspace_scope(users, workspaces):
    from inventory.oauth import resolve_inventory_token

    workspace, other_workspace = workspaces
    Membership.objects.create(
        workspace=other_workspace,
        user=users[0],
        role=Membership.Role.MEMBER,
    )
    oauth_client = OAuthClient.objects.create(
        client_id="credential-audit-client",
        metadata={"client_name": "Credential audit client"},
    )
    family_id = uuid.uuid4()
    valid, valid_raw = OAuthCredential.issue(
        kind=OAuthCredential.Kind.ACCESS,
        client=oauth_client,
        user=users[0],
        workspace=workspace,
        can_write=False,
        family_id=family_id,
        scopes=["inventory"],
        resource="https://quilombo.life/mcp",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    _expired, expired_raw = OAuthCredential.issue(
        kind=OAuthCredential.Kind.ACCESS,
        client=oauth_client,
        user=users[0],
        workspace=workspace,
        family_id=uuid.uuid4(),
        scopes=["inventory"],
        resource="https://quilombo.life/mcp",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    revoked, revoked_raw = OAuthCredential.issue(
        kind=OAuthCredential.Kind.ACCESS,
        client=oauth_client,
        user=users[0],
        workspace=other_workspace,
        family_id=uuid.uuid4(),
        scopes=["inventory"],
        resource="https://quilombo.life/mcp",
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["revoked_at"])

    assert resolve_inventory_token(valid_raw) == valid
    assert resolve_inventory_token(valid_raw).can_write is False
    assert resolve_inventory_token(expired_raw) is None
    assert resolve_inventory_token(revoked_raw) is None

    api_client = APIClient()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {valid_raw}")
    assert api_client.get(f"/api/workspaces/{other_workspace.slug}/items/").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_oauth_registration_and_authorization_reject_unsafe_redirects_without_echoing_secrets():
    async def exercise_oauth():
        from quilombo.asgi import create_application

        application = create_application()
        transport = httpx2.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as http_client:
                unsafe_http = await http_client.post(
                    "/register",
                    json={"redirect_uris": ["http://example.com/callback"]},
                )
                fragment = await http_client.post(
                    "/register",
                    json={"redirect_uris": ["https://example.com/callback#fragment"]},
                )
                invalid_scope = await http_client.post(
                    "/register",
                    json={
                        "redirect_uris": ["https://example.com/callback"],
                        "scope": "inventory unknown",
                    },
                )
                registration = await http_client.post(
                    "/register",
                    json={"redirect_uris": ["https://example.com/callback"]},
                )
                registered = registration.json()
                mismatched_redirect = await http_client.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": registered["client_id"],
                        "redirect_uri": "https://attacker.example/callback",
                        "code_challenge": "challenge",
                        "code_challenge_method": "S256",
                    },
                )
                secret = registered["client_secret"]
                invalid_secret = await http_client.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": registered["client_id"],
                        "client_secret": "wrong-secret",
                        "code": "not-a-code",
                        "code_verifier": "verifier",
                    },
                )
                return (
                    unsafe_http,
                    fragment,
                    invalid_scope,
                    registration,
                    mismatched_redirect,
                    secret,
                    invalid_secret,
                )

    (
        unsafe_http,
        fragment,
        invalid_scope,
        registration,
        mismatched_redirect,
        secret,
        invalid_secret,
    ) = asyncio.run(exercise_oauth())

    assert unsafe_http.status_code == 400
    assert fragment.status_code == 400
    assert invalid_scope.status_code == 400
    assert registration.status_code == 201
    assert mismatched_redirect.status_code == 400
    assert mismatched_redirect.json()["error"] == "invalid_request"
    assert invalid_secret.status_code == 401
    assert secret not in invalid_secret.text
    assert "wrong-secret" not in invalid_secret.text


@pytest.mark.django_db
def test_dashboard_workspace_pagination_is_stable(client, users, workspaces):
    extra_workspaces = [
        Workspace(name=f"Workspace {index:02}", slug=f"workspace-{index:02}") for index in range(26)
    ]
    Workspace.objects.bulk_create(extra_workspaces)
    Membership.objects.bulk_create(
        [
            Membership(workspace=workspace, user=users[0], role=Membership.Role.MEMBER)
            for workspace in extra_workspaces
        ]
    )
    client.force_login(users[0])

    response = client.get("/app/", {"page": 2})

    assert response.status_code == 200
    assert response.context["page_obj"].number == 2
    assert response.context["page_obj"].paginator.count == 27
    assert [workspace.name for workspace in response.context["workspaces"]] == [
        "Workspace 24",
        "Workspace 25",
    ]


@pytest.mark.django_db
def test_human_inventory_search_scopes_to_location_subtree(client, users, workspaces):
    workshop, _ = workspaces
    root = Location.objects.create(workspace=workshop, key="taller", name="Taller")
    drawer = Location.objects.create(
        workspace=workshop,
        key="cajon-1",
        name="Cajón 1",
        parent=root,
    )
    outside = Location.objects.create(workspace=workshop, key="biblioteca", name="Biblioteca")
    screws = Item.objects.create(workspace=workshop, key="fix-35", name="Tornillos FIX 35 mm")
    Holding.objects.create(workspace=workshop, item=screws, location=drawer, quantity=20)
    Holding.objects.create(workspace=workshop, item=screws, location=outside, quantity=5)
    client.force_login(users[0])

    response = client.get(
        "/app/workshop/",
        {"q": "tornillos", "location": "taller"},
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert "Cajón 1" in content
    assert [holding.location.key for holding in response.context["holdings"]] == ["cajon-1"]


@pytest.mark.django_db
def test_human_category_filter_preserves_pagination_and_workspace_scope(client, users, workspaces):
    workshop, library = workspaces
    location = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    for index in range(26):
        item = Item.objects.create(
            workspace=workshop,
            key=f"fastener-{index}",
            name=f"Fastener {index}",
            category="fasteners",
        )
        Holding.objects.create(workspace=workshop, item=item, location=location, quantity=1)
    other_item = Item.objects.create(
        workspace=library,
        key="secret-book",
        name="Secret book",
        category="books",
    )
    other_location = Location.objects.create(workspace=library, key="shelf", name="Shelf")
    Holding.objects.create(workspace=library, item=other_item, location=other_location, quantity=1)
    client.force_login(users[0])

    response = client.get("/app/workshop/", {"category": "FASTENERS", "page": 2})
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["category"] == "FASTENERS"
    assert response.context["page_obj"].paginator.count == 26
    assert len(response.context["holdings"]) == 1
    assert response.context["holdings"][0].item.category == "fasteners"
    assert 'value="fasteners" selected' in content
    assert "category=FASTENERS" in content
    assert other_item.name not in content


@pytest.mark.django_db
def test_human_category_options_are_case_insensitive_unique(client, users, workspaces):
    workshop, _ = workspaces
    location = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    for index, category in enumerate(("Books", "books", "TOOLS")):
        item = Item.objects.create(
            workspace=workshop,
            key=f"category-{index}",
            name=f"Category item {index}",
            category=category,
        )
        Holding.objects.create(workspace=workshop, item=item, location=location, quantity=1)
    client.force_login(users[0])

    response = client.get("/app/workshop/")

    assert response.status_code == 200
    assert response.context["category_options"] == ["books", "tools"]


@pytest.mark.django_db
def test_human_location_filter_renders_depth_first_tree(client, users, workspaces):
    workshop, library = workspaces
    bookcase = Location.objects.create(
        workspace=workshop,
        key="bookcase",
        name="Biblioteca de 4 estantes",
    )
    shelf = Location.objects.create(
        workspace=workshop,
        key="shelf-1",
        name="Estante 1",
        parent=bookcase,
    )
    Location.objects.create(
        workspace=workshop,
        key="drawer-1",
        name="Cajón 1",
        parent=shelf,
    )
    Location.objects.create(
        workspace=workshop,
        key="hallway-library",
        name="Biblioteca del pasillo",
    )
    client.force_login(users[0])

    response = client.get("/app/workshop/", {"location": "drawer-1"})

    assert response.status_code == 200
    assert response.context["location_options"] == [
        {"key": "bookcase", "label": "Biblioteca de 4 estantes"},
        {"key": "shelf-1", "label": "\u00a0\u00a0⤷ Estante 1"},
        {"key": "drawer-1", "label": "\u00a0\u00a0\u00a0\u00a0⤷ Cajón 1"},
        {"key": "hallway-library", "label": "Biblioteca del pasillo"},
    ]
    assert 'value="drawer-1" selected' in response.content.decode()

    fiction = Location.objects.create(
        workspace=library,
        key="fiction",
        name="Ficción",
    )
    Location.objects.create(
        workspace=library,
        key="latin-america",
        name="Latinoamérica",
        parent=fiction,
    )
    client.force_login(users[1])

    library_response = client.get("/app/library/")

    assert library_response.status_code == 200
    assert library_response.context["location_options"] == [
        {"key": "fiction", "label": "Ficción"},
        {"key": "latin-america", "label": "\u00a0\u00a0⤷ Latinoamérica"},
    ]


@pytest.mark.django_db
def test_web_crud_manages_workshop_item_holdings_and_locations(client, users, workspaces):
    workshop, _ = workspaces
    client.force_login(users[0])

    parent_response = client.post(
        "/app/workshop/locations/new/",
        {
            "key": "cabinet",
            "name": "Tool cabinet",
            "description": "Against the north wall",
            "kind": "cabinet",
            "aliases": "storage, tools",
        },
    )
    assert parent_response.status_code == 302, parent_response.context["location_form"].errors
    parent = workshop.locations.get(key="cabinet")
    child_response = client.post(
        "/app/workshop/locations/new/",
        {
            "key": "drawer-1",
            "name": "Drawer 1",
            "kind": "drawer",
            "parent": parent.id,
            "aliases": "",
        },
    )
    assert child_response.status_code == 302, child_response.context["location_form"].errors
    drawer = workshop.locations.get(key="drawer-1")

    cycle = client.post(
        f"/app/workshop/locations/{parent.id}/edit/",
        {
            "key": "cabinet",
            "name": "Tool cabinet",
            "kind": "cabinet",
            "parent": drawer.id,
            "aliases": "storage, tools",
        },
    )
    parent.refresh_from_db()
    assert cycle.status_code == 200
    assert parent.parent is None

    new_item_page = client.get("/app/workshop/items/new/")
    assert "Tool cabinet → Drawer 1" in new_item_page.content.decode()

    created = client.post(
        "/app/workshop/items/new/",
        {
            "key": "fix-35",
            "name": "FIX screws",
            "description": "Red box",
            "category": "fasteners",
            "aliases": "wood screws, screws",
            "tracking_mode": Item.TrackingMode.BULK,
            "unit": "piece",
            "minimum_quantity": "10",
            "target_quantity": "20",
            "holding-location": drawer.id,
            "holding-quantity": "12.5",
            "holding-approximate": "on",
            "holding-notes": "Opened box",
        },
    )
    item = workshop.items.get(key="fix-35")
    holding = item.holdings.get()
    detail = client.get(f"/app/workshop/items/{item.id}/")

    assert parent_response.status_code == 302
    assert child_response.status_code == 302
    assert created.status_code == 302
    assert created.url == f"/app/workshop/items/{item.id}/"
    assert item.aliases == ["wood screws", "screws"]
    assert holding.quantity == Decimal("12.5")
    assert "Tool cabinet → Drawer 1" in detail.content.decode()

    invalid_tracking = client.post(
        f"/app/workshop/items/{item.id}/edit/",
        {
            "key": "fix-35",
            "name": "FIX screws",
            "description": "Red box",
            "category": "fasteners",
            "aliases": "wood screws, screws",
            "tracking_mode": Item.TrackingMode.DISCRETE,
            "unit": "piece",
            "minimum_quantity": "10",
            "target_quantity": "20",
        },
    )
    item.refresh_from_db()
    assert invalid_tracking.status_code == 200
    assert item.tracking_mode == Item.TrackingMode.BULK

    edited = client.post(
        f"/app/workshop/items/{item.id}/edit/",
        {
            "key": "fix-35",
            "name": "FIX 35 mm screws",
            "description": "Red and white box",
            "category": "fasteners",
            "aliases": "wood screws",
            "tracking_mode": Item.TrackingMode.BULK,
            "unit": "piece",
            "minimum_quantity": "10",
            "target_quantity": "25",
        },
    )
    holding_edited = client.post(
        f"/app/workshop/items/{item.id}/holdings/{holding.id}/edit/",
        {
            "location": drawer.id,
            "quantity": "15",
            "notes": "Counted",
        },
    )
    location_edited = client.post(
        f"/app/workshop/locations/{drawer.id}/edit/",
        {
            "key": "drawer-1",
            "name": "Top drawer",
            "kind": "drawer",
            "parent": parent.id,
            "aliases": "",
        },
    )
    item.refresh_from_db()
    holding.refresh_from_db()
    drawer.refresh_from_db()

    assert edited.status_code == 302
    assert holding_edited.status_code == 302
    assert location_edited.status_code == 302
    assert item.name == "FIX 35 mm screws"
    assert holding.quantity == Decimal("15")
    assert drawer.name == "Top drawer"

    assert (
        client.get(f"/app/workshop/items/{item.id}/holdings/{holding.id}/delete/").status_code
        == 200
    )
    assert (
        client.post(f"/app/workshop/items/{item.id}/holdings/{holding.id}/delete/").status_code
        == 302
    )
    assert not Holding.objects.filter(id=holding.id).exists()
    item_list_response = client.get("/app/workshop/items/")
    assert item_list_response.status_code == 200
    assert "FIX 35 mm screws" in item_list_response.content.decode()
    assert client.post(f"/app/workshop/items/{item.id}/delete/").status_code == 302
    assert not Item.objects.filter(id=item.id).exists()


@pytest.mark.django_db
def test_web_crud_renders_library_paths(client, users, workspaces):
    _, library = workspaces
    bookcase = Location.objects.create(workspace=library, key="bookcase", name="Bookcase")
    shelf = Location.objects.create(
        workspace=library,
        key="shelf-2",
        name="Shelf 2",
        parent=bookcase,
    )
    book = Item.objects.create(
        workspace=library,
        key="gelman",
        name="Interrupciones I",
        tracking_mode=Item.TrackingMode.DISCRETE,
        unit="copy",
    )
    Holding.objects.create(workspace=library, item=book, location=shelf, quantity=1)
    client.force_login(users[1])

    locations = client.get("/app/library/locations/")
    detail = client.get(f"/app/library/items/{book.id}/")

    assert locations.status_code == 200
    assert detail.status_code == 200
    assert "Bookcase → Shelf 2" in locations.content.decode()
    assert "Bookcase → Shelf 2" in detail.content.decode()
    assert "Ubicaciones" in locations.content.decode()

    client.post("/i18n/setlang/", {"language": "en", "next": "/app/library/locations/"})
    english_locations = client.get("/app/library/locations/")
    assert "Locations" in english_locations.content.decode()


@pytest.mark.django_db
def test_web_book_type_sets_schema_and_item_defaults(client, users, workspaces):
    workspace, _ = workspaces
    shelf = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    client.force_login(users[0])

    new_item_page = client.get("/app/workshop/items/new/")
    created = client.post(
        "/app/workshop/items/new/",
        {
            "key": "matilda",
            "name": "Matilda",
            "schema": "book",
            "description": "",
            "category": "libros",
            "aliases": "",
            "minimum_quantity": "",
            "target_quantity": "",
            "holding-location": shelf.id,
            "holding-quantity": "1",
            "holding-notes": "",
        },
    )

    item = workspace.items.get(key="matilda")
    assert new_item_page.status_code == 200
    assert 'name="schema"' in new_item_page.content.decode()
    assert created.status_code == 302
    assert item.attributes == {"schema": "book"}
    assert item.tracking_mode == Item.TrackingMode.DISCRETE
    assert item.unit == "copy"
    assert item.holdings.get().quantity == Decimal("1")


@pytest.mark.django_db
def test_legacy_book_schema_migration_preserves_attributes(users, workspaces):
    from importlib import import_module

    workspace, _ = workspaces
    item = Item.objects.create(
        workspace=workspace,
        key="legacy-book",
        name="Legacy book",
        category="libros",
        attributes={"author": "An author", "publisher": "A publisher"},
        tracking_mode=Item.TrackingMode.BULK,
        unit="unit",
    )
    schema_only_item = Item.objects.create(
        workspace=workspace,
        key="schema-only-book",
        name="Schema-only book",
        attributes={"schema": "book", "source": "legacy"},
        tracking_mode=Item.TrackingMode.BULK,
        unit="unit",
    )

    migration = import_module("inventory.migrations.0013_normalize_book_schema")
    migration.normalize_book_schema(
        SimpleNamespace(
            get_model=lambda app_label, model_name: {"Item": Item, "Holding": Holding}[model_name]
        ),
        None,
    )

    item.refresh_from_db()
    assert item.attributes == {"author": "An author", "publisher": "A publisher", "schema": "book"}
    assert "title" not in item.attributes.get("book", {})
    assert item.tracking_mode == Item.TrackingMode.DISCRETE
    assert item.unit == "copy"
    schema_only_item.refresh_from_db()
    assert schema_only_item.attributes == {"schema": "book", "source": "legacy"}
    assert schema_only_item.tracking_mode == Item.TrackingMode.DISCRETE
    assert schema_only_item.unit == "copy"


@pytest.mark.django_db
def test_legacy_book_schema_migration_rejects_fractional_holdings(users, workspaces):
    from importlib import import_module

    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    item = Item.objects.create(
        workspace=workspace,
        key="fractional-book",
        name="Fractional book",
        category="libros",
        tracking_mode=Item.TrackingMode.BULK,
        unit="unit",
    )
    Holding.objects.create(
        workspace=workspace,
        item=item,
        location=location,
        quantity=Decimal("1.5"),
    )

    migration = import_module("inventory.migrations.0013_normalize_book_schema")
    with pytest.raises(RuntimeError, match="fractional holdings"):
        migration.normalize_book_schema(
            SimpleNamespace(
                get_model=lambda app_label, model_name: {"Item": Item, "Holding": Holding}[
                    model_name
                ]
            ),
            None,
        )

    item.refresh_from_db()
    assert item.attributes == {}
    assert item.tracking_mode == Item.TrackingMode.BULK
    assert item.unit == "unit"


@pytest.mark.django_db
def test_item_api_rejects_book_conversion_with_fractional_holdings(users, workspaces):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    item = Item.objects.create(
        workspace=workspace,
        key="fractional-book",
        name="Fractional book",
        tracking_mode=Item.TrackingMode.BULK,
        unit="unit",
    )
    Holding.objects.create(
        workspace=workspace,
        item=item,
        location=location,
        quantity=Decimal("1.5"),
    )
    client = APIClient()
    client.force_authenticate(users[0])

    response = client.patch(
        f"/api/workspaces/{workspace.slug}/items/{item.id}/",
        {"attributes": {"schema": "book"}},
        format="json",
    )

    assert response.status_code == 400
    assert "whole quantities" in response.content.decode()
    item.refresh_from_db()
    assert item.attributes == {}
    assert item.tracking_mode == Item.TrackingMode.BULK
    assert item.unit == "unit"


@pytest.mark.django_db
def test_web_book_detail_shows_editions_and_confirms_identifier(client, users, workspaces):
    workspace, _ = workspaces
    book = Item.objects.create(
        workspace=workspace,
        key="matilda",
        name="Matilda",
        category="books",
        attributes={
            "schema": "book",
            "book": {"title": "Matilda", "authors": ["Roald Dahl"]},
        },
    )
    client.force_login(users[0])
    search_payload = {
        "docs": [
            {
                "title": "Matilda",
                "author_name": ["Roald Dahl"],
                "publisher": ["Puffin", "Ace"],
                "edition_key": ["OL111M", "OL222M"],
                "isbn": ["9780140328721", "9780439023481"],
            }
        ]
    }
    edition_payloads = [
        {
            "title": "Matilda",
            "publishers": ["Puffin"],
            "publish_date": "1988",
            "number_of_pages": 240,
            "identifiers": {"isbn_13": ["9780140328721"]},
            "covers": [111],
        },
        {
            "title": "Matilda",
            "publishers": ["Ace"],
            "publish_date": "2000",
            "number_of_pages": 256,
            "identifiers": {"isbn_13": ["9780439023481"]},
            "covers": [222],
        },
    ]
    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        side_effect=[
            io.BytesIO(json.dumps(search_payload).encode()),
            *(io.BytesIO(json.dumps(payload).encode()) for payload in edition_payloads),
        ],
    ) as urlopen_mock:
        detail = client.get(f"/app/workshop/items/{book.id}/")

    content = detail.content.decode()
    assert detail.status_code == 200
    assert "Open Library" in content
    assert "9780140328721" in content
    assert "9780439023481" in content
    assert "Confirm this edition" in content
    assert urlopen_mock.call_count == 3

    confirmation_payload = {
        "title": "Matilda",
        "authors": [{"name": "Roald Dahl"}],
        "publishers": [{"name": "Puffin"}],
        "identifiers": {"isbn_13": ["9780140328721"]},
    }
    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(confirmation_payload).encode()),
    ):
        confirmed = client.post(
            f"/app/workshop/items/{book.id}/book/confirm/",
            {"isbn": "9780140328721", "edition": "OL111M"},
        )

    assert confirmed.status_code == 302
    book.refresh_from_db()
    assert book.attributes["identifiers"] == {
        "isbn": ["9780140328721"],
        "openlibrary_edition": ["OL111M"],
    }
    assert "Puffin" not in book.attributes


@pytest.mark.django_db
def test_web_book_confirmation_rejects_mismatched_isbn_and_edition(client, users, workspaces):
    workspace, _ = workspaces
    book = Item.objects.create(
        workspace=workspace,
        key="matilda",
        name="Matilda",
        category="books",
        attributes={"schema": "book", "book": {"title": "Matilda"}},
    )
    client.force_login(users[0])
    edition_payload = {
        "title": "Matilda",
        "publishers": [{"name": "Ace"}],
        "identifiers": {"isbn_13": ["9780439023481"]},
    }

    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(edition_payload).encode()),
    ):
        response = client.post(
            f"/app/workshop/items/{book.id}/book/confirm/",
            {"isbn": "9780140328721", "edition": "OL222M"},
        )

    assert response.status_code == 302
    book.refresh_from_db()
    assert "identifiers" not in book.attributes


@pytest.mark.django_db
def test_web_crud_rejects_read_only_and_cross_workspace_writes(client, users, workspaces):
    workshop, library = workspaces
    own_location = Location.objects.create(workspace=workshop, key="bench", name="Bench")
    other_location = Location.objects.create(workspace=library, key="shelf", name="Shelf")
    item = Item.objects.create(workspace=workshop, key="hammer", name="Hammer")
    Membership.objects.create(workspace=workshop, user=users[1], can_write=False)

    client.force_login(users[1])
    assert client.get(f"/app/workshop/items/{item.id}/").status_code == 200
    onboarding = client.get("/app/workshop/first-inventory/")
    assert onboarding.status_code == 200
    assert "Crear primera ubicación" not in onboarding.content.decode()
    denied = client.post(
        "/app/workshop/items/new/",
        {
            "key": "intrusion",
            "name": "Intrusion",
            "tracking_mode": Item.TrackingMode.BULK,
            "unit": "unit",
            "holding-location": own_location.id,
            "holding-quantity": "1",
        },
    )
    assert denied.status_code == 403
    assert not workshop.items.filter(key="intrusion").exists()

    client.force_login(users[0])
    cross_parent = client.post(
        "/app/workshop/locations/new/",
        {
            "key": "cross-parent",
            "name": "Cross parent",
            "parent": other_location.id,
            "aliases": "",
        },
    )
    cross_holding = client.post(
        f"/app/workshop/items/{item.id}/holdings/new/",
        {"location": other_location.id, "quantity": "1"},
    )

    assert cross_parent.status_code == 200
    assert cross_holding.status_code == 200
    assert not workshop.locations.filter(key="cross-parent").exists()
    assert not item.holdings.exists()

    with pytest.raises(ValidationError, match="another workspace"):
        create_item_with_holding(
            workspace=workshop,
            item_data={
                "key": "rolled-back",
                "name": "Rolled back",
                "tracking_mode": Item.TrackingMode.BULK,
                "unit": "unit",
            },
            holding_data={"location": other_location, "quantity": Decimal("1")},
        )
    assert not workshop.items.filter(key="rolled-back").exists()


@pytest.mark.django_db
def test_human_inventory_pagination_preserves_search_and_location(client, users, workspaces):
    workshop, _ = workspaces
    drawer = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    for index in range(26):
        item = Item.objects.create(
            workspace=workshop,
            key=f"screw-{index:02}",
            name=f"Screw {index:02}",
        )
        Holding.objects.create(
            workspace=workshop, item=item, location=drawer, quantity=Decimal("1")
        )
    client.force_login(users[0])

    response = client.get(
        "/app/workshop/",
        {"q": "screw", "location": "drawer", "page": 2},
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 26
    assert [holding.item.key for holding in response.context["holdings"]] == ["screw-25"]
    assert "q=screw&amp;location=drawer&amp;page=1" in content


@pytest.mark.django_db
def test_human_inventory_initial_page_paginates_holdings_in_database(client, users, workspaces):
    workshop, _ = workspaces
    location = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    for index in range(26):
        item = Item.objects.create(
            workspace=workshop,
            key=f"item-{index:02}",
            name=f"Item {index:02}",
        )
        Holding.objects.create(
            workspace=workshop, item=item, location=location, quantity=Decimal("1")
        )
    client.force_login(users[0])

    with patch("inventory.views.search_holdings") as search_mock:
        response = client.get("/app/workshop/")

    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 26
    assert len(response.context["holdings"]) == 25
    assert response.context["truncated"] is False
    assert all(
        "last_observed_by" in holding._state.fields_cache
        for holding in response.context["holdings"]
    )
    search_mock.assert_not_called()


@pytest.mark.django_db
def test_health_check_includes_database(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": settings.APP_VERSION}


@pytest.mark.django_db
def test_inventory_audit_corrects_and_verifies_facts_idempotently(users, workspaces):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="drawer", name="Drawer")
    item = Item.objects.create(
        workspace=workspace,
        key="screws",
        name="Screws",
        tracking_mode=Item.TrackingMode.DISCRETE,
    )
    holding = Holding.objects.create(
        workspace=workspace, item=item, location=location, quantity=Decimal("8")
    )
    observed_at = timezone.now() - timedelta(days=2)
    data = {
        "location_key": location.key,
        "location_status": VerificationStatus.CONFIRMED,
        "holdings": [
            {
                "holding_id": holding.id,
                "status": VerificationStatus.CONFIRMED,
                "quantity": Decimal("10"),
                "approximate": True,
            }
        ],
        "idempotency_key": "audit-drawer-001",
        "provenance": {
            "client_actor": "workshop-audit-agent",
            "source_kind": InventoryEvent.SourceKind.AGENT,
            "source_reference": "session://audit-42",
            "observed_at": observed_at,
        },
    }

    event, replayed = audit_inventory(
        workspace=workspace,
        actor=users[0],
        data=data,
        request_hash=hash_request(data),
    )
    replay_event, was_replayed = audit_inventory(
        workspace=workspace,
        actor=users[0],
        data=data,
        request_hash=hash_request(data),
    )

    location.refresh_from_db()
    holding.refresh_from_db()
    assert replayed is False
    assert was_replayed is True
    assert replay_event == event
    assert event.kind == InventoryEvent.Kind.AUDIT
    assert event.client_actor == "workshop-audit-agent"
    assert event.source_reference == "session://audit-42"
    assert location.verification_status == VerificationStatus.CONFIRMED
    assert location.last_observed_at == observed_at
    assert location.last_observed_by == users[0]
    assert holding.quantity == Decimal("10")
    assert holding.approximate is True
    assert holding.freshness_status == "current"
    assert event.summary["holdings"][0]["corrected_fields"] == ["quantity", "approximate"]
    assert workspace.inventory_events.filter(kind=InventoryEvent.Kind.AUDIT).count() == 1


@pytest.mark.django_db
def test_inventory_audit_rejects_cross_workspace_holding_and_rolls_back(users, workspaces):
    workshop, library = workspaces
    drawer = Location.objects.create(workspace=workshop, key="drawer", name="Drawer")
    shelf = Location.objects.create(workspace=library, key="shelf", name="Shelf")
    book = Item.objects.create(workspace=library, key="gelman", name="Interrupciones I")
    library_holding = Holding.objects.create(
        workspace=library, item=book, location=shelf, quantity=Decimal("1")
    )
    data = {
        "location_key": drawer.key,
        "location_status": VerificationStatus.CONFIRMED,
        "holdings": [{"holding_id": library_holding.id, "status": VerificationStatus.CONFIRMED}],
        "idempotency_key": "cross-workspace-audit",
        "provenance": {},
    }

    with pytest.raises(BulkUpsertError, match="not found"):
        audit_inventory(
            workspace=workshop,
            actor=users[0],
            data=data,
            request_hash=hash_request(data),
        )

    drawer.refresh_from_db()
    assert drawer.verification_status == VerificationStatus.UNKNOWN
    assert drawer.last_observed_at is None
    assert not workshop.inventory_events.exists()


@pytest.mark.django_db
def test_confirmed_fact_becomes_stale_after_configured_interval(settings, workspaces):
    settings.INVENTORY_FRESHNESS_DAYS = 30
    workspace, _ = workspaces
    location = Location.objects.create(
        workspace=workspace,
        key="cabinet",
        name="Cabinet",
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=timezone.now() - timedelta(days=31),
    )

    assert location.freshness_status == "stale"


@pytest.mark.django_db
def test_holding_mutation_invalidates_previous_verification(users, workspaces):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="drawer", name="Drawer")
    item = Item.objects.create(workspace=workspace, key="screws", name="Screws")
    holding = Holding.objects.create(
        workspace=workspace,
        item=item,
        location=location,
        quantity=Decimal("5"),
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=timezone.now(),
        last_observed_by=users[0],
    )

    holding.quantity = Decimal("6")
    holding.save(update_fields=["quantity", "updated_at"])

    holding.refresh_from_db()
    assert holding.verification_status == VerificationStatus.UNKNOWN
    assert holding.last_observed_at is None
    assert holding.last_observed_by is None


@pytest.mark.django_db
def test_inventory_audit_rejects_observation_older_than_current_fact(users, workspaces):
    workspace, _ = workspaces
    newer = timezone.now()
    location = Location.objects.create(
        workspace=workspace,
        key="shelf",
        name="Shelf",
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=newer,
        last_observed_by=users[0],
    )
    item = Item.objects.create(workspace=workspace, key="book", name="A book")
    holding = Holding.objects.create(
        workspace=workspace,
        item=item,
        location=location,
        quantity=Decimal("1"),
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=newer,
        last_observed_by=users[0],
    )
    data = {
        "location_key": location.key,
        "location_status": VerificationStatus.CONFIRMED,
        "holdings": [
            {
                "holding_id": holding.id,
                "status": VerificationStatus.CONFIRMED,
                "quantity": Decimal("2"),
            }
        ],
        "idempotency_key": "delayed-audit",
        "provenance": {"observed_at": newer - timedelta(days=1)},
    }

    with pytest.raises(BulkUpsertError, match="newer observation"):
        audit_inventory(
            workspace=workspace,
            actor=users[0],
            data=data,
            request_hash=hash_request(data),
        )

    holding.refresh_from_db()
    assert holding.quantity == Decimal("1")
    assert holding.last_observed_at == newer
    assert not workspace.inventory_events.exists()


def test_public_web_footer_shows_runtime_version(client):
    response = client.get("/")

    assert response.status_code == 200
    assert f"v{settings.APP_VERSION}" in response.content.decode()


@pytest.mark.django_db
def test_web_item_edit_records_event_and_item_detail_shows_latest_edit(client, users, workspaces):
    workspace, other_workspace = workspaces
    item = Item.objects.create(workspace=workspace, key="drill", name="Old drill")
    client.force_login(users[0])

    response = client.post(
        f"/app/workshop/items/{item.id}/edit/",
        {
            "key": item.key,
            "name": "Updated drill",
            "description": "",
            "category": "",
            "aliases": "",
            "tracking_mode": Item.TrackingMode.BULK,
            "unit": item.unit,
            "minimum_quantity": "",
            "target_quantity": "",
        },
    )
    InventoryEvent.objects.create(
        workspace=other_workspace,
        actor=users[1],
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        source_kind=InventoryEvent.SourceKind.AGENT,
        summary={"item_id": str(item.id), "item_key": item.key, "item_fields": ["name"]},
    )

    event = workspace.inventory_events.get(kind=InventoryEvent.Kind.ITEM_UPDATE)
    detail = client.get(f"/app/workshop/items/{item.id}/")
    content = detail.content.decode()

    assert response.status_code == 302
    assert response.url == f"/app/workshop/items/{item.id}/"
    assert event.actor == users[0]
    assert event.source_kind == InventoryEvent.SourceKind.MANUAL
    assert event.summary == {
        "item_id": str(item.id),
        "item_key": item.key,
        "item_fields": ["name"],
    }
    assert "Actualización de objeto" in content
    assert "Responsable: one" in content
    assert "Origen: Manual" in content
    assert "Campos: name" in content
    assert "two" not in content


@pytest.mark.django_db
def test_item_detail_without_edits_hides_latest_edit(client, users, workspaces):
    workspace, _ = workspaces
    item = Item.objects.create(workspace=workspace, key="drill", name="A drill")
    client.force_login(users[0])

    content = client.get(f"/app/workshop/items/{item.id}/").content.decode()

    assert "Actualización de objeto" not in content


@pytest.mark.django_db
def test_item_detail_shows_latest_agent_edit(client, users, workspaces):
    workspace, _ = workspaces
    item = Item.objects.create(workspace=workspace, key="drill", name="A drill")
    InventoryEvent.objects.create(
        workspace=workspace,
        actor=users[0],
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        source_kind=InventoryEvent.SourceKind.AGENT,
        summary={"item_id": str(item.id), "item_key": item.key, "item_fields": ["category"]},
    )
    client.force_login(users[0])

    content = client.get(f"/app/workshop/items/{item.id}/").content.decode()

    assert "Actualización de objeto" in content
    assert "Origen: Agente" in content
    assert "Campos: category" in content


@pytest.mark.django_db
def test_move_inventory_is_atomic_and_idempotent(users, workspaces):
    workspace, _ = workspaces
    source = Location.objects.create(workspace=workspace, key="drawer-1", name="Drawer 1")
    destination = Location.objects.create(workspace=workspace, key="drawer-2", name="Drawer 2")
    item = Item.objects.create(workspace=workspace, key="fix-35mm", name="FIX 35 mm")
    Holding.objects.create(
        workspace=workspace,
        item=item,
        location=source,
        quantity=Decimal("10"),
    )
    request = {
        "item_key": item.key,
        "from_location_key": source.key,
        "to_location_key": destination.key,
        "quantity": "3",
        "idempotency_key": "move-fix-001",
        "provenance": {"client_actor": "test-agent"},
    }

    event, replayed = move_inventory(
        workspace=workspace,
        actor=users[0],
        request_hash=hash_request(request),
        **request,
    )
    replay_event, was_replayed = move_inventory(
        workspace=workspace,
        actor=users[0],
        request_hash=hash_request(request),
        **request,
    )

    assert replayed is False
    assert was_replayed is True
    assert replay_event == event
    assert workspace.holdings.get(location=source).quantity == Decimal("7")
    assert workspace.holdings.get(location=destination).quantity == Decimal("3")
    assert workspace.inventory_events.filter(kind=InventoryEvent.Kind.MOVE).count() == 1


@pytest.mark.django_db
def test_update_inventory_item_repairs_fields_and_holding_idempotently(users, workspaces):
    workspace, _ = workspaces
    source = Location.objects.create(workspace=workspace, key="wrong", name="Wrong shelf")
    destination = Location.objects.create(workspace=workspace, key="right", name="Right shelf")
    item = Item.objects.create(
        workspace=workspace,
        key="wrong-key",
        name="Wrong name",
        description="Incorrect description",
    )
    holding = Holding.objects.create(
        workspace=workspace,
        item=item,
        location=source,
        quantity=Decimal("2"),
    )
    data = {
        "item_id": item.id,
        "idempotency_key": "repair-item-001",
        "provenance": {"client_actor": "test-agent"},
        "item": {
            "key": "correct-key",
            "name": "Correct name",
            "description": "Correct description",
            "attributes": {"color": "green"},
        },
        "holdings": [
            {
                "id": holding.id,
                "location_id": destination.id,
                "quantity": Decimal("3"),
                "notes": "Confirmed correction",
            }
        ],
    }

    event, replayed = update_inventory_item(
        workspace=workspace,
        actor=users[0],
        data=data,
        request_hash=hash_request(data),
    )
    replay_event, was_replayed = update_inventory_item(
        workspace=workspace,
        actor=users[0],
        data=data,
        request_hash=hash_request(data),
    )

    item.refresh_from_db()
    holding.refresh_from_db()
    assert replayed is False
    assert was_replayed is True
    assert replay_event == event
    assert item.key == "correct-key"
    assert item.attributes == {"color": "green"}
    assert holding.location == destination
    assert holding.quantity == Decimal("3")
    assert workspace.inventory_events.filter(kind=InventoryEvent.Kind.ITEM_UPDATE).count() == 1


@pytest.mark.django_db
def test_update_inventory_item_is_tenant_scoped_and_rolls_back(users, workspaces):
    workspace, other_workspace = workspaces
    location = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    other_location = Location.objects.create(
        workspace=other_workspace, key="other-shelf", name="Other shelf"
    )
    item = Item.objects.create(workspace=workspace, key="item", name="Original")
    other_item = Item.objects.create(workspace=other_workspace, key="other", name="Other")
    holding = Holding.objects.create(
        workspace=workspace, item=item, location=location, quantity=Decimal("1")
    )
    data = {
        "item_id": item.id,
        "idempotency_key": "repair-item-rollback",
        "item": {"name": "Changed"},
        "holdings": [{"id": holding.id, "location_id": other_location.id}],
    }

    with pytest.raises(BulkUpsertError, match="destination location"):
        update_inventory_item(
            workspace=workspace,
            actor=users[0],
            data=data,
            request_hash=hash_request(data),
        )
    with pytest.raises(BulkUpsertError, match="not found"):
        update_inventory_item(
            workspace=workspace,
            actor=users[0],
            data={
                "item_id": other_item.id,
                "idempotency_key": "repair-other-item",
                "item": {"name": "Intrusion"},
            },
            request_hash="other",
        )

    item.refresh_from_db()
    other_item.refresh_from_db()
    assert item.name == "Original"
    assert other_item.name == "Other"
    assert not workspace.inventory_events.filter(idempotency_key="repair-item-rollback").exists()


@pytest.mark.django_db
def test_update_inventory_item_requires_whole_holdings_when_switching_to_discrete(
    users, workspaces
):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="bin", name="Bin")
    item = Item.objects.create(workspace=workspace, key="parts", name="Parts")
    holding = Holding.objects.create(
        workspace=workspace,
        item=item,
        location=location,
        quantity=Decimal("1.5"),
    )
    invalid = {
        "item_id": item.id,
        "idempotency_key": "switch-discrete-invalid",
        "item": {"tracking_mode": Item.TrackingMode.DISCRETE},
    }

    with pytest.raises(BulkUpsertError, match="whole quantities"):
        update_inventory_item(
            workspace=workspace,
            actor=users[0],
            data=invalid,
            request_hash=hash_request(invalid),
        )

    item.refresh_from_db()
    assert item.tracking_mode == Item.TrackingMode.BULK

    corrected = {
        "item_id": item.id,
        "idempotency_key": "switch-discrete-corrected",
        "item": {"tracking_mode": Item.TrackingMode.DISCRETE},
        "holdings": [{"id": holding.id, "quantity": Decimal("2")}],
    }
    update_inventory_item(
        workspace=workspace,
        actor=users[0],
        data=corrected,
        request_hash=hash_request(corrected),
    )

    item.refresh_from_db()
    holding.refresh_from_db()
    assert item.tracking_mode == Item.TrackingMode.DISCRETE
    assert holding.quantity == Decimal("2")


@pytest.mark.django_db
def test_delete_inventory_item_is_tenant_scoped_and_idempotent(users, workspaces):
    workspace, other_workspace = workspaces
    item = Item.objects.create(workspace=workspace, key="duplicate", name="Duplicate")
    other_item = Item.objects.create(workspace=other_workspace, key="kept", name="Kept")
    data = {
        "item_id": item.id,
        "idempotency_key": "delete-duplicate-001",
        "provenance": {"source_reference": "Confirmed duplicate"},
    }

    event, replayed = delete_inventory_item(
        workspace=workspace,
        actor=users[0],
        data=data,
        request_hash=hash_request(data),
    )
    replay_event, was_replayed = delete_inventory_item(
        workspace=workspace,
        actor=users[0],
        data=data,
        request_hash=hash_request(data),
    )
    with pytest.raises(BulkUpsertError, match="not found"):
        delete_inventory_item(
            workspace=workspace,
            actor=users[0],
            data={
                "item_id": other_item.id,
                "idempotency_key": "delete-other-item",
            },
            request_hash="other",
        )

    assert replayed is False
    assert was_replayed is True
    assert replay_event == event
    assert not workspace.items.filter(id=item.id).exists()
    assert other_workspace.items.filter(id=other_item.id).exists()
    assert event.summary["item_id"] == str(item.id)


@pytest.mark.django_db(transaction=True)
def test_mcp_collection_cursors_preserve_filters_and_boundaries(users, workspaces):
    from inventory.mcp import (
        MCPErrorCode,
        StructuredToolError,
        find_inventory,
        get_inventory_snapshot,
    )

    workspace, _ = workspaces
    root = Location.objects.create(workspace=workspace, key="drawer", name="Drawer")
    for index in range(3):
        location = Location.objects.create(
            workspace=workspace,
            parent=root,
            key=f"drawer-{index}",
            name=f"Drawer {index}",
        )
        item = Item.objects.create(
            workspace=workspace,
            key=f"screw-{index}",
            name=f"Screw {index}",
            category="hardware",
        )
        Holding.objects.create(workspace=workspace, item=item, location=location, quantity=1)
    _, raw_token = ApiToken.issue(workspace=workspace, user=users[0], name="Cursor test")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )

    first_search = find_inventory(
        "screw",
        ctx,
        category="hardware",
        location_key="drawer",
        include_descendants=True,
        limit=2,
    )
    second_search = find_inventory(
        "screw",
        ctx,
        category="hardware",
        location_key="drawer",
        include_descendants=True,
        limit=2,
        cursor=first_search["next_cursor"],
    )

    assert first_search["truncated"] is True
    assert len(first_search["results"]) == 2
    assert second_search["truncated"] is False
    assert len(second_search["results"]) == 1
    assert {
        result["item_key"] for result in first_search["results"] + second_search["results"]
    } == {"screw-0", "screw-1", "screw-2"}
    assert second_search["next_cursor"] is None

    first_snapshot = get_inventory_snapshot(
        ctx,
        location_key="drawer",
        category="hardware",
        include_descendants=True,
        limit=2,
    )
    second_snapshot = get_inventory_snapshot(
        ctx,
        location_key="drawer",
        category="hardware",
        include_descendants=True,
        limit=2,
        cursor=first_snapshot["next_cursor"],
    )

    assert first_snapshot["truncated"] is True
    assert len(first_snapshot["locations"]) == 2
    assert len(first_snapshot["items"]) == 2
    assert len(first_snapshot["holdings"]) == 2
    assert second_snapshot["truncated"] is False
    assert len(second_snapshot["locations"]) == 2
    assert len(second_snapshot["items"]) == 1
    assert len(second_snapshot["holdings"]) == 1
    assert second_snapshot["next_cursor"] is None

    with pytest.raises(ToolError, match="Invalid or expired cursor"):
        find_inventory("screw", ctx, cursor="invalid")
    with patch("inventory.mcp._CURSOR_SIGNER.unsign_object", side_effect=SignatureExpired):
        with pytest.raises(ToolError, match="Invalid or expired cursor"):
            find_inventory("screw", ctx, cursor=first_search["next_cursor"])

    with pytest.raises(StructuredToolError) as invalid_cursor:
        find_inventory("screw", ctx, cursor="invalid")
    assert invalid_cursor.value.code == MCPErrorCode.INVALID_INPUT.value


@pytest.mark.django_db
def test_mcp_errors_have_stable_codes_and_do_not_expose_workspace_details(users, workspaces):
    from inventory.mcp import (
        MCPErrorCode,
        StructuredToolError,
        _token_from_context,
        bulk_upsert_inventory,
        find_inventory,
    )

    with pytest.raises(StructuredToolError) as missing_token:
        _token_from_context(SimpleNamespace(headers={}))
    assert missing_token.value.payload == {
        "code": MCPErrorCode.AUTHENTICATION.value,
        "message": "A Quilombo bearer token is required.",
    }

    _, raw_token = ApiToken.issue(workspace=workspaces[0], user=users[0], name="MCP errors")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )
    foreign_item = Item.objects.create(
        workspace=workspaces[1], key="foreign-secret", name="Foreign secret"
    )
    Location.objects.create(workspace=workspaces[0], key="drawer", name="Drawer")
    with pytest.raises(StructuredToolError) as isolated_reference:
        bulk_upsert_inventory(
            "cross-workspace-mcp",
            ctx,
            holdings=[
                {
                    "item_key": foreign_item.key,
                    "location_key": "drawer",
                    "quantity": "1",
                }
            ],
        )
    assert isolated_reference.value.code == MCPErrorCode.NOT_FOUND.value
    assert "foreign-secret" not in isolated_reference.value.user_message

    with pytest.raises(StructuredToolError) as invalid_query:
        find_inventory("", ctx)
    assert invalid_query.value.code == MCPErrorCode.INVALID_INPUT.value


@pytest.mark.django_db
def test_mcp_attribute_profile_handles_alias_and_invalid_categories(users, workspaces):
    from inventory.mcp import MCPErrorCode, StructuredToolError, get_attribute_profile

    _, raw_token = ApiToken.issue(workspace=workspaces[0], user=users[0], name="MCP profiles")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )

    profile = get_attribute_profile(" books ", ctx)

    assert profile["category"] == "book"
    assert profile["version"] == "1.1"
    assert profile["tracking_mode"] == "discrete"
    assert profile["unit"] == "copy"
    assert profile["minimum_for_catalog_lookup"] == []
    with pytest.raises(StructuredToolError) as empty_category:
        get_attribute_profile("   ", ctx)
    with pytest.raises(StructuredToolError) as unknown_category:
        get_attribute_profile("vinyl", ctx)
    assert empty_category.value.code == MCPErrorCode.INVALID_INPUT.value
    assert unknown_category.value.code == MCPErrorCode.NOT_FOUND.value


@pytest.mark.django_db
def test_mcp_book_details_uses_stored_isbn_without_mutating_inventory(users, workspaces):
    from inventory.mcp import get_book_details

    item = Item.objects.create(
        workspace=workspaces[0],
        key="matilda",
        name="Matilda",
        category="books",
        attributes={
            "schema": "book",
            "book": {"title": "Matilda", "authors": ["Roald Dahl"]},
            "identifiers": {"isbn_13": ["9780140328721"]},
        },
    )
    original_attributes = item.attributes.copy()
    _, raw_token = ApiToken.issue(workspace=workspaces[0], user=users[0], name="MCP books")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )
    payload = {
        "ISBN:9780140328721": {
            "url": "https://openlibrary.org/books/OL7353617M/Matilda",
            "title": "Matilda",
            "authors": [{"name": "Roald Dahl"}],
            "publishers": [{"name": "Puffin"}],
            "identifiers": {"isbn_13": ["9780140328721"]},
            "cover": {"medium": "https://covers.openlibrary.org/example.jpg"},
        }
    }

    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(payload).encode()),
    ) as urlopen_mock:
        result = get_book_details(str(item.id), ctx)

    assert result["match_method"] == "isbn"
    assert result["isbn"] == "9780140328721"
    assert result["details"]["title"] == "Matilda"
    assert result["details"]["authors"] == ["Roald Dahl"]
    assert "suggested_item" not in result
    assert urlopen_mock.call_count == 1
    item.refresh_from_db()
    assert item.attributes == original_attributes
    assert not InventoryEvent.objects.exists()


@pytest.mark.django_db
def test_mcp_book_details_searches_profile_and_isolates_workspace(users, workspaces):
    from inventory.mcp import MCPErrorCode, StructuredToolError, get_book_details

    item = Item.objects.create(
        workspace=workspaces[0],
        key="the-left-hand-of-darkness",
        name="The Left Hand of Darkness",
        attributes={
            "schema": "book",
            "book": {
                "title": "The Left Hand of Darkness",
                "authors": ["Ursula K. Le Guin"],
                "publishers": ["Ace"],
            },
        },
    )
    _, foreign_token = ApiToken.issue(workspace=workspaces[1], user=users[1], name="MCP other")
    foreign_ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {foreign_token}"},
        session=SimpleNamespace(client_params=None),
    )
    with pytest.raises(StructuredToolError) as inaccessible:
        get_book_details(str(item.id), foreign_ctx)
    assert inaccessible.value.code == MCPErrorCode.NOT_FOUND.value

    _, raw_token = ApiToken.issue(workspace=workspaces[0], user=users[0], name="MCP books")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )
    payload = {
        "numFound": 2,
        "docs": [
            {
                "title": "The Left Hand of Darkness",
                "author_name": ["Ursula K. Le Guin"],
                "publisher": ["Ace"],
                "first_publish_year": 1969,
                "edition_key": ["OL123M"],
                "isbn": ["9780441007318"],
                "cover_i": 123,
                "number_of_pages_median": 304,
            },
            {
                "title": "The Left Hand of Darkness",
                "author_name": ["Ursula K. Le Guin"],
                "publisher": ["Gollancz"],
                "edition_key": ["OL456M"],
            },
        ],
    }

    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        return_value=io.BytesIO(json.dumps(payload).encode()),
    ) as urlopen_mock:
        result = get_book_details(str(item.id), ctx)

    assert result["match_method"] == "metadata"
    assert result["query"] == {
        "title": "The Left Hand of Darkness",
        "authors": ["Ursula K. Le Guin"],
        "publishers": ["Ace"],
    }
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["cover_url"].endswith("/123-M.jpg")
    assert result["candidates"][0]["openlibrary_edition"] == "OL123M"
    assert urlopen_mock.call_count == 1
    item.refresh_from_db()
    assert item.attributes["schema"] == "book"


@pytest.mark.django_db
@override_settings(MCP_MAX_MUTATION_COLLECTION_ITEMS=2)
def test_mcp_mutation_collection_limits_reject_before_writing(users, workspaces):
    from inventory.mcp import StructuredToolError, bulk_upsert_inventory

    _, raw_token = ApiToken.issue(workspace=workspaces[0], user=users[0], name="MCP limits")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )
    with pytest.raises(StructuredToolError, match="cannot contain more than 2") as error:
        bulk_upsert_inventory(
            "too-many-items",
            ctx,
            items=[{"key": f"item-{index}", "name": f"Item {index}"} for index in range(3)],
        )

    assert error.value.code == "invalid_input"
    assert not workspaces[0].items.exists()


@pytest.mark.django_db
@override_settings(MCP_MAX_MUTATION_PAYLOAD_BYTES=128)
def test_mcp_mutation_payload_size_limit_rejects_large_serialized_input(users, workspaces):
    from inventory.mcp import StructuredToolError, bulk_upsert_inventory

    _, raw_token = ApiToken.issue(workspace=workspaces[0], user=users[0], name="MCP payload")
    ctx = SimpleNamespace(
        headers={"authorization": f"Bearer {raw_token}"},
        session=SimpleNamespace(client_params=None),
    )
    with pytest.raises(StructuredToolError, match="payload is too large") as error:
        bulk_upsert_inventory(
            "large-payload",
            ctx,
            provenance={"metadata": {"client_note": "x" * 200}},
        )

    assert error.value.code == "invalid_input"


@pytest.mark.django_db
@override_settings(BOOK_CATALOG_TIMEOUT_SECONDS=0.25, BOOK_CATALOG_MAX_RETRIES=2)
def test_book_catalog_retries_transient_failures_with_bounded_timeout():
    from unittest.mock import MagicMock

    from inventory.catalogs import lookup_book_by_isbn

    response = MagicMock()
    response.__enter__.return_value = response
    payload = {
        "ISBN:9780140328721": {
            "title": "Fantastic Mr Fox",
            "identifiers": {"isbn_13": ["9780140328721"]},
        }
    }
    cache.clear()
    with (
        patch(
            "inventory.catalogs.urlopen",
            side_effect=[URLError("temporary"), URLError("temporary"), response],
        ) as urlopen,
        patch("inventory.catalogs.json.load", return_value=payload),
    ):
        result = lookup_book_by_isbn("9780140328721")

    assert result["suggested_item"]["name"] == "Fantastic Mr Fox"
    assert urlopen.call_count == 3
    assert all(call.kwargs["timeout"] == 0.25 for call in urlopen.call_args_list)


@pytest.mark.django_db
@override_settings(BOOK_CATALOG_TIMEOUT_SECONDS=0.25, BOOK_CATALOG_MAX_RETRIES=2)
def test_book_catalog_exhausted_retries_raise_clean_upstream_error():
    from inventory.catalogs import CatalogLookupError, lookup_book_by_isbn

    cache.clear()
    with patch(
        "inventory.catalogs.urlopen",
        side_effect=[URLError("temporary"), URLError("temporary"), URLError("temporary")],
    ) as urlopen:
        with pytest.raises(CatalogLookupError, match="temporarily unavailable"):
            lookup_book_by_isbn("9780140328721")

    assert urlopen.call_count == 3


@pytest.mark.django_db(transaction=True)
def test_mcp_snapshot_has_bounded_collections_and_constant_query_work(users, workspaces):
    from inventory.mcp import get_inventory_snapshot

    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    items = [
        Item(
            workspace=workspace,
            key=f"tool-{index}",
            name=f"Tool {index}",
            category="tools",
        )
        for index in range(101)
    ]
    Item.objects.bulk_create(items)
    Holding.objects.bulk_create(
        [Holding(workspace=workspace, item=item, location=location, quantity=1) for item in items]
    )
    _, raw_token = ApiToken.issue(workspace=workspace, user=users[0], name="Snapshot bound test")
    ctx = SimpleNamespace(headers={"authorization": f"Bearer {raw_token}"})

    with CaptureQueriesContext(connection) as queries:
        snapshot = get_inventory_snapshot(ctx, category="tools")

    assert snapshot["limit"] == 100
    assert len(snapshot["items"]) == 100
    assert len(snapshot["holdings"]) == 100
    assert snapshot["truncated"] is True
    assert snapshot["next_cursor"]
    assert len(queries) <= 10


@pytest.mark.django_db(transaction=True)
def test_streamable_http_mcp_authenticates_and_searches(users, workspaces):
    workspace, _ = workspaces
    workshop = Location.objects.create(workspace=workspace, key="workshop", name="Workshop")
    location = Location.objects.create(
        workspace=workspace,
        parent=workshop,
        key="drawer-1-a",
        name="Drawer 1 A",
    )
    item = Item.objects.create(
        workspace=workspace,
        key="fix-35mm",
        name="FIX 35 mm screws",
        description="Red box with white lettering",
        aliases=["tornillos para madera"],
        attributes={"appearance": {"color": "red"}},
        minimum_quantity=Decimal("20"),
        target_quantity=Decimal("30"),
    )
    Holding.objects.create(
        workspace=workspace, item=item, location=location, quantity=Decimal("12")
    )
    outside_location = Location.objects.create(
        workspace=workspace, key="outside-room", name="Outside room"
    )
    outside_item = Item.objects.create(
        workspace=workspace,
        key="outside-match",
        name="Outside tornillos madera",
        aliases=["tornillos madera"],
    )
    Holding.objects.create(
        workspace=workspace, item=outside_item, location=outside_location, quantity=Decimal("4")
    )
    neighbor = Item.objects.create(
        workspace=workspace,
        key="wall-plugs",
        name="Wall plugs",
    )
    neighbor_holding = Holding.objects.create(
        workspace=workspace,
        item=neighbor,
        location=location,
        quantity=Decimal("8"),
        verification_status=VerificationStatus.CONFIRMED,
        last_observed_at=timezone.now(),
        last_observed_by=users[0],
    )
    empty_location = Location.objects.create(
        workspace=workspace,
        key="empty-shelf",
        name="Empty shelf",
    )
    empty_item = Item.objects.create(
        workspace=workspace,
        key="unplaced-tool",
        name="Unplaced tool",
    )
    _, raw_token = ApiToken.issue(workspace=workspace, user=users[0], name="MCP test")
    _, read_only_token = ApiToken.issue(
        workspace=workspace,
        user=users[0],
        name="Read-only MCP test",
        can_write=False,
    )

    async def exercise_mcp():
        from quilombo.asgi import create_application

        application = create_application()
        transport = httpx2.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {raw_token}"},
            ) as http_client:
                mcp_transport = streamable_http_client(
                    "http://testserver/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                )
                async with Client(
                    mcp_transport,
                    client_info=Implementation(name="quilombo-test-client", version="1.2.3"),
                ) as mcp_client:
                    tools = await mcp_client.list_tools()
                    resources = await mcp_client.list_resources()
                    policy = await mcp_client.read_resource("quilombo://guides/inventory-policy")
                    profile = await mcp_client.call_tool(
                        "get_attribute_profile", {"category": "book"}
                    )
                    assert profile.is_error is False
                    assert profile.structured_content["category"] == "book"
                    assert profile.structured_content["minimum_for_catalog_lookup"] == []
                    assert profile.structured_content["recommended_for_disambiguation"] == [
                        "authors",
                        "publishers",
                    ]
                    result = await mcp_client.call_tool(
                        "find_inventory",
                        {
                            "query": "tornillos madera",
                            "location_key": "workshop",
                            "include_descendants": True,
                            "limit": 10,
                        },
                    )
                    status_result = await mcp_client.call_tool("get_inventory_status", {})
                    snapshot_result = await mcp_client.call_tool("get_inventory_snapshot", {})
                    mutation_result = await mcp_client.call_tool(
                        "bulk_upsert_inventory",
                        {
                            "idempotency_key": "mcp-client-provenance",
                            "items": [{"key": "mcp-marker", "name": "MCP marker"}],
                        },
                    )
                    server_info = mcp_client.server_info
                    server_instructions = mcp_client.instructions
                retry_transport = streamable_http_client(
                    "http://testserver/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                )
                async with Client(
                    retry_transport,
                    client_info=Implementation(name="quilombo-test-client", version="9.0.0"),
                ) as retry_client:
                    replay_result = await retry_client.call_tool(
                        "bulk_upsert_inventory",
                        {
                            "idempotency_key": "mcp-client-provenance",
                            "items": [{"key": "mcp-marker", "name": "MCP marker"}],
                        },
                    )
                http_client.headers["Authorization"] = f"Bearer {read_only_token}"
                read_only_transport = streamable_http_client(
                    "http://testserver/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                )
                async with Client(read_only_transport) as read_only_client:
                    read_result = await read_only_client.call_tool(
                        "find_inventory", {"query": "screws"}
                    )
                    write_result = await read_only_client.call_tool(
                        "bulk_upsert_inventory",
                        {"idempotency_key": "read-only-write"},
                    )
                return (
                    tools,
                    resources,
                    policy,
                    result,
                    status_result,
                    snapshot_result,
                    mutation_result,
                    replay_result,
                    server_info,
                    server_instructions,
                    read_result,
                    write_result,
                )

    (
        tools,
        resources,
        policy,
        result,
        status_result,
        snapshot_result,
        mutation_result,
        replay_result,
        server_info,
        server_instructions,
        read_result,
        write_result,
    ) = asyncio.run(exercise_mcp())

    assert {tool.name for tool in tools.tools} == {
        "audit_inventory",
        "bulk_upsert_inventory",
        "delete_inventory_item",
        "find_inventory",
        "get_attribute_profile",
        "get_book_details",
        "get_inventory_status",
        "get_inventory_snapshot",
        "lookup_book_by_isbn",
        "lookup_books_by_isbn",
        "move_inventory",
        "update_inventory_item",
    }
    assert [str(resource.uri) for resource in resources.resources] == [
        "quilombo://guides/inventory-policy"
    ]
    tools_by_name = {tool.name: tool for tool in tools.tools}
    assert all(tool.input_schema["type"] == "object" for tool in tools_by_name.values())
    assert tools_by_name["find_inventory"].input_schema["required"] == ["query"]
    assert tools_by_name["find_inventory"].input_schema["properties"]["cursor"] == {
        "default": "",
        "title": "Cursor",
        "type": "string",
    }
    assert tools_by_name["get_attribute_profile"].input_schema["required"] == ["category"]
    assert tools_by_name["get_book_details"].input_schema["required"] == ["item_id"]
    assert tools_by_name["lookup_books_by_isbn"].input_schema["required"] == ["isbns"]
    assert tools_by_name["get_inventory_snapshot"].input_schema["properties"]["limit"] == {
        "default": 100,
        "title": "Limit",
        "type": "integer",
    }
    assert tools_by_name["find_inventory"].annotations.read_only_hint is True
    assert tools_by_name["get_attribute_profile"].annotations.read_only_hint is True
    assert tools_by_name["get_book_details"].annotations.read_only_hint is True
    assert tools_by_name["get_inventory_snapshot"].annotations.read_only_hint is True
    assert tools_by_name["bulk_upsert_inventory"].annotations.read_only_hint is False
    assert tools_by_name["lookup_book_by_isbn"].annotations.open_world_hint is True
    assert tools_by_name["get_book_details"].annotations.open_world_hint is True
    assert tools_by_name["lookup_books_by_isbn"].annotations.open_world_hint is True
    for tool_name in {
        "find_inventory",
        "get_attribute_profile",
        "get_inventory_snapshot",
        "audit_inventory",
        "bulk_upsert_inventory",
        "move_inventory",
        "update_inventory_item",
        "delete_inventory_item",
    }:
        assert tools_by_name[tool_name].annotations.open_world_hint is False
    for tool_name in {"audit_inventory", "move_inventory", "update_inventory_item"}:
        annotations = tools_by_name[tool_name].annotations
        assert annotations.destructive_hint is True
        assert annotations.idempotent_hint is True
    for tool_name in {"bulk_upsert_inventory", "delete_inventory_item"}:
        annotations = tools_by_name[tool_name].annotations
        assert annotations.destructive_hint is True
        assert annotations.idempotent_hint is True
    assert policy.contents[0].mime_type == "text/markdown"
    assert "Search before stating where an item is" in policy.contents[0].text
    assert "loaded a Quilombo-specific skill" in policy.contents[0].text
    assert "attributes.schema` as `book`" in policy.contents[0].text
    assert server_instructions == policy.contents[0].text
    move_tool = next(tool for tool in tools.tools if tool.name == "move_inventory")
    assert move_tool.annotations.destructive_hint is True
    assert move_tool.annotations.idempotent_hint is True
    find_tool = next(tool for tool in tools.tools if tool.name == "find_inventory")
    assert "not recorded" in find_tool.description
    assert server_info.version == settings.APP_VERSION
    assert server_info.website_url == settings.PUBLIC_BASE_URL
    assert len(server_info.icons) == 1
    assert server_info.icons[0].src == (
        f"{settings.PUBLIC_BASE_URL}/static/inventory/quilombo-mark.png"
    )
    assert server_info.icons[0].mime_type == "image/png"
    assert server_info.icons[0].sizes == ["64x64"]
    assert mutation_result.is_error is False
    assert replay_result.is_error is False
    assert replay_result.structured_content["replayed"] is True
    mcp_event = workspace.inventory_events.get(idempotency_key="mcp-client-provenance")
    assert mcp_event.source_kind == InventoryEvent.SourceKind.AGENT
    assert mcp_event.metadata["server_mcp_client"] == {
        "name": "quilombo-test-client",
        "version": "1.2.3",
    }
    assert read_result.is_error is False
    assert write_result.is_error is True
    assert "read-only" in write_result.content[0].text
    assert write_result.structured_content == {
        "code": "authorization",
        "message": "This inventory is shared as read-only.",
    }
    assert result.is_error is False
    assert {row["item_key"] for row in result.structured_content["results"]} == {item.key}
    assert all(
        row["location_key"] != outside_location.key for row in result.structured_content["results"]
    )
    first_result = result.structured_content["results"][0]
    assert first_result["location_key"] == "drawer-1-a"
    assert result.structured_content["truncated"] is False
    assert first_result["search"]["match_type"] == "complete"
    assert first_result["item_description"] == "Red box with white lettering"
    assert [place["key"] for place in first_result["location_path"]] == [
        "workshop",
        "drawer-1-a",
    ]
    assert first_result["nearby_items"][0]["item_key"] == "wall-plugs"
    assert first_result["nearby_items"][0]["holding_id"] == str(neighbor_holding.id)
    assert first_result["nearby_items"][0]["freshness"] == "current"
    assert status_result.is_error is False
    assert status_result.structured_content["items"][0]["recommended_add_quantity"] == "18.000000"
    snapshot = snapshot_result.structured_content
    assert snapshot["limit"] == 100
    assert next(row for row in snapshot["locations"] if row["key"] == "empty-shelf")["id"] == str(
        empty_location.id
    )
    assert next(row for row in snapshot["items"] if row["key"] == "unplaced-tool")["id"] == str(
        empty_item.id
    )


@pytest.mark.django_db(transaction=True)
def test_oauth_pkce_flow_issues_and_refreshes_mcp_access(client, users, workspaces):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="shelf", name="Shelf")
    item = Item.objects.create(workspace=workspace, key="gelman", name="Interrupciones I")
    Holding.objects.create(workspace=workspace, item=item, location=location, quantity=1)
    verifier = "quilombo-oauth-verifier-with-more-than-forty-three-characters"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    challenge = challenge.rstrip("=")

    async def begin_authorization():
        from quilombo.asgi import create_application

        application = create_application()
        transport = httpx2.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as http_client:
                metadata = await http_client.get("/.well-known/oauth-authorization-server")
                openid_metadata = await http_client.get("/.well-known/openid-configuration")
                registration = await http_client.post(
                    "/register",
                    json={
                        "client_name": "Quilombo test client",
                        "redirect_uris": ["http://localhost/callback"],
                        "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "scope": "inventory offline_access",
                    },
                )
                registered_client = registration.json()
                authorization = await http_client.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": registered_client["client_id"],
                        "redirect_uri": "http://localhost/callback",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "scope": "inventory offline_access",
                        "resource": "http://localhost:8000/mcp",
                        "state": "test-state",
                    },
                )
                return (
                    metadata,
                    openid_metadata,
                    registration,
                    registered_client,
                    authorization,
                )

    metadata, openid_metadata, registration, registered_client, authorization = asyncio.run(
        begin_authorization()
    )

    assert metadata.status_code == 200
    assert openid_metadata.status_code == 404
    assert metadata.json()["registration_endpoint"] == "http://localhost:8000/register"
    assert registration.status_code == 201
    assert authorization.status_code == 302

    consent_url = urlsplit(authorization.headers["location"])
    request_id = parse_qs(consent_url.query)["request"][0]
    client.force_login(users[0])
    consent_page = client.get(consent_url.path, {"request": request_id})
    assert consent_page.status_code == 200
    assert "Quilombo test client" in consent_page.content.decode()
    consent = client.post(
        consent_url.path,
        {
            "request": request_id,
            "workspace": str(workspace.id),
            "action": "allow",
            "can_write": "on",
        },
        follow=False,
    )
    callback = urlsplit(consent.headers["location"])
    callback_params = parse_qs(callback.query)
    authorization_code = callback_params["code"][0]
    assert callback_params["state"] == ["test-state"]

    async def exchange_and_use_tokens():
        from quilombo.asgi import create_application

        application = create_application()
        transport = httpx2.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http_client:
                token_response = await http_client.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": registered_client["client_id"],
                        "code": authorization_code,
                        "redirect_uri": "http://localhost/callback",
                        "code_verifier": verifier,
                        "resource": "http://localhost:8000/mcp",
                    },
                )
                tokens = token_response.json()
                http_client.headers["Authorization"] = f"Bearer {tokens['access_token']}"
                mcp_transport = streamable_http_client(
                    "http://testserver/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                )
                async with Client(mcp_transport) as mcp_client:
                    result = await mcp_client.call_tool("find_inventory", {"query": "Gelman"})

                refresh_response = await http_client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": registered_client["client_id"],
                        "refresh_token": tokens["refresh_token"],
                    },
                )
                return token_response, tokens, result, refresh_response

    token_response, tokens, result, refresh_response = asyncio.run(exchange_and_use_tokens())

    assert token_response.status_code == 200
    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"].startswith("qlo_oauth_")
    assert result.is_error is False
    assert result.structured_content["results"][0]["location_key"] == "shelf"
    assert refresh_response.status_code == 200
    assert refresh_response.json()["access_token"] != tokens["access_token"]
    assert OAuthCredential.objects.filter(
        kind=OAuthCredential.Kind.ACCESS, revoked_at__isnull=False
    ).exists()
    assert OAuthCredential.objects.filter(can_write=True).exists()
    assert (
        AccessEvent.objects.filter(
            user=users[0], channel=AccessEvent.Channel.MCP, client_name="Quilombo test client"
        ).count()
        == 1
    )
