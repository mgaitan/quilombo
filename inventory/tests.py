from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from .models import Holding, InventoryEvent, Item, Location, Membership, Workspace


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
