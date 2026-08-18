import asyncio
import base64
import hashlib
import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import httpx2
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from rest_framework.test import APIClient

from .models import (
    ApiToken,
    Holding,
    InventoryEvent,
    Item,
    Location,
    Membership,
    OAuthAuthorizationRequest,
    OAuthClient,
    OAuthCredential,
    Workspace,
)
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
    Holding.objects.create(workspace=library, item=dolina, location=shelf, quantity=1)
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
    assert result["nearby_items"] == [
        {
            "item_key": "cronicas-angel-gris",
            "item_name": "Crónicas del Ángel Gris",
            "description": "Edición con lomo azul",
            "attributes": {"schema": "book", "appearance": {"spine_color": "blue"}},
        }
    ]


@pytest.mark.django_db
def test_stock_status_reports_missing_and_low_items_within_location(users, workspaces):
    workshop, library = workspaces
    root = Location.objects.create(workspace=workshop, key="taller", name="Taller")
    drawer = Location.objects.create(workspace=workshop, parent=root, key="cajon", name="Cajón")
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
            "current_quantity": "3.000000",
            "minimum_quantity": "10.000000",
            "target_quantity": "25.000000",
            "recommended_add_quantity": "22.000000",
            "unit": "units",
            "locations": [
                {
                    "location_key": "cajon",
                    "location_name": "Cajón",
                    "quantity": "3.000000",
                }
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
    assert response.url == "/app/"
    assert client.session.get("_auth_user_id") is not None
    user = get_user_model().objects.get(username="new-user")
    workspace = user.workspaces.get()
    assert workspace.name == "Home"
    assert workspace.slug == f"home-{str(user.id)[:8]}"
    assert workspace.memberships.get(user=user).role == Membership.Role.OWNER


@pytest.mark.django_db
def test_public_home_and_connector_guide(client):
    home_response = client.get("/")
    connector_response = client.get("/connect/")
    login_response = client.get("/accounts/login/")
    signup_response = client.get("/accounts/signup/")

    assert home_response.status_code == 200
    assert "organizar el mundo físico" in home_response.content.decode()
    assert connector_response.status_code == 200
    assert "http://localhost:8000/mcp" in connector_response.content.decode()
    assert "ChatGPT" in connector_response.content.decode()
    assert "Claude" in connector_response.content.decode()
    assert login_response.status_code == 200
    assert signup_response.status_code == 200


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

    guide = client.get(f"/app/{workshop.slug}/first-inventory/")
    other_workspace = client.get(f"/app/{library.slug}/first-inventory/")

    assert guide.status_code == 200
    assert "una zona por vez" in guide.content.decode()
    assert other_workspace.status_code == 404


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
    neighbor = Item.objects.create(
        workspace=workspace,
        key="wall-plugs",
        name="Wall plugs",
    )
    Holding.objects.create(
        workspace=workspace,
        item=neighbor,
        location=location,
        quantity=Decimal("8"),
    )
    _, raw_token = ApiToken.issue(workspace=workspace, user=users[0], name="MCP test")

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
                async with Client(mcp_transport) as mcp_client:
                    tools = await mcp_client.list_tools()
                    result = await mcp_client.call_tool(
                        "find_inventory", {"query": "tornillos madera"}
                    )
                    status_result = await mcp_client.call_tool("get_inventory_status", {})
                    return tools, result, status_result

    tools, result, status_result = asyncio.run(exercise_mcp())

    assert {tool.name for tool in tools.tools} == {
        "bulk_upsert_inventory",
        "find_inventory",
        "get_inventory_status",
        "get_inventory_snapshot",
        "lookup_book_by_isbn",
        "move_inventory",
    }
    assert result.is_error is False
    first_result = result.structured_content["results"][0]
    assert first_result["location_key"] == "drawer-1-a"
    assert first_result["search"]["match_type"] == "complete"
    assert first_result["item_description"] == "Red box with white lettering"
    assert [place["key"] for place in first_result["location_path"]] == [
        "workshop",
        "drawer-1-a",
    ]
    assert first_result["nearby_items"][0]["item_key"] == "wall-plugs"
    assert status_result.is_error is False
    assert status_result.structured_content["items"][0]["recommended_add_quantity"] == "18.000000"


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
                return metadata, registration, registered_client, authorization

    metadata, registration, registered_client, authorization = asyncio.run(begin_authorization())

    assert metadata.status_code == 200
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
        {"request": request_id, "workspace": str(workspace.id), "action": "allow"},
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
