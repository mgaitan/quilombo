import asyncio
from decimal import Decimal

import httpx2
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from rest_framework.test import APIClient

from .models import ApiToken, Holding, InventoryEvent, Item, Location, Membership, Workspace
from .services import hash_request, move_inventory


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
    assert own_response.json()[0]["key"] == "a1"
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
    event = workspace.inventory_events.get()
    assert event.source_kind == InventoryEvent.SourceKind.PHOTO
    assert event.source_reference == "Processed a workshop photo on 2026-08-14"

    replay = client.post("/api/workspaces/workshop/bulk-upsert/", payload, format="json")

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert workspace.inventory_events.count() == 1


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
    assert token_client.get("/api/workspaces/").json()[0]["slug"] == "workshop"


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
def test_public_signup_logs_user_in(client):
    response = client.post(
        "/accounts/signup/",
        {
            "username": "new-user",
            "password1": "correct-horse-battery-staple-917",
            "password2": "correct-horse-battery-staple-917",
        },
    )

    assert response.status_code == 302
    assert response.url == "/api/workspaces/"
    assert client.session.get("_auth_user_id") is not None


@pytest.mark.django_db
def test_health_check_includes_database(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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


@pytest.mark.django_db(transaction=True)
def test_streamable_http_mcp_authenticates_and_searches(users, workspaces):
    workspace, _ = workspaces
    location = Location.objects.create(workspace=workspace, key="drawer-1-a", name="Drawer 1 A")
    item = Item.objects.create(
        workspace=workspace,
        key="fix-35mm",
        name="FIX 35 mm screws",
        aliases=["tornillos para madera"],
    )
    Holding.objects.create(
        workspace=workspace, item=item, location=location, quantity=Decimal("12")
    )
    _, raw_token = ApiToken.issue(workspace=workspace, user=users[0], name="MCP test")

    async def exercise_mcp():
        from quilombo.asgi import application

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
                async with Client(mcp_transport) as mcp_client:
                    tools = await mcp_client.list_tools()
                    result = await mcp_client.call_tool(
                        "find_inventory", {"query": "tornillos madera"}
                    )
                    return tools, result

    tools, result = asyncio.run(exercise_mcp())

    assert {tool.name for tool in tools.tools} == {
        "bulk_upsert_inventory",
        "find_inventory",
        "get_inventory_snapshot",
        "move_inventory",
    }
    assert result.is_error is False
    assert result.structured_content["results"][0]["location_key"] == "drawer-1-a"
