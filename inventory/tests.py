from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from .models import Holding, Item, Location, Membership, Workspace


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
