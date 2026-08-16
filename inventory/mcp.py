from typing import Any

from django.conf import settings
from django.db.models import Q
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .catalogs import CatalogLookupError
from .catalogs import lookup_book_by_isbn as lookup_book_catalog
from .models import Holding, Location, LocationRelation
from .oauth import QuilomboOAuthProvider, resolve_inventory_token
from .serializers import BulkUpsertSerializer, ProvenanceSerializer
from .services import (
    BulkUpsertError,
    IdempotencyConflict,
    build_holding_clue_context,
    get_stock_status,
    hash_request,
    location_scope_ids,
    search_holdings,
)
from .services import (
    bulk_upsert_inventory as bulk_upsert_service,
)
from .services import (
    move_inventory as move_inventory_service,
)

oauth_provider = QuilomboOAuthProvider()

server = MCPServer(
    name="quilombo",
    title="Quilombo physical inventory",
    version="0.1.0",
    instructions=(
        "Quilombo stores user-authorized physical inventory facts and does not infer facts. "
        "Search before mutation. Mutating tools write immediately: apply any draft and human "
        "confirmation policy in the client before calling them. Supply a unique idempotency key "
        "and provenance for every mutation. Never claim that source media was uploaded."
    ),
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=settings.PUBLIC_BASE_URL.rstrip("/"),
        resource_server_url=f"{settings.PUBLIC_BASE_URL}/mcp",
        service_documentation_url=f"{settings.PUBLIC_BASE_URL}/connect/",
        required_scopes=["inventory"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["inventory", "offline_access"],
            default_scopes=["inventory", "offline_access"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
EXTERNAL_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
MOVE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _token_from_context(ctx: Context):
    headers = ctx.headers or {}
    authorization = headers.get("authorization", "")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ToolError("A Quilombo bearer token is required.")
    token = resolve_inventory_token(parts[1])
    if not token:
        raise ToolError("Invalid or revoked Quilombo bearer token.")
    return token


def _serialize_holding(holding, clue_context=None):
    clue_context = clue_context or {}
    serialized = {
        "item_key": holding.item.key,
        "item_name": holding.item.name,
        "item_description": holding.item.description,
        "item_aliases": holding.item.aliases,
        "category": holding.item.category,
        "attributes": holding.item.attributes,
        "location_key": holding.location.key,
        "location_name": holding.location.name,
        "location_path": clue_context.get("location_paths", {}).get(holding.location_id, []),
        "nearby_items": clue_context.get("nearby_by_holding", {}).get(holding.id, []),
        "quantity": str(holding.quantity),
        "unit": holding.item.unit,
        "approximate": holding.approximate,
        "notes": holding.notes,
    }
    if hasattr(holding, "_search_match"):
        serialized["search"] = holding._search_match
    return serialized


@server.tool(
    title="Find inventory",
    description=(
        "Find stored items and their precise locations using deterministic text, alias, category, "
        "attribute, and location matching. Results are ranked and explain matched and unmatched "
        "terms. Use this before telling a user where something is."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def find_inventory(
    query: str,
    ctx: Context,
    category: str = "",
    location_key: str = "",
    include_descendants: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    token = _token_from_context(ctx)
    if not query.strip():
        raise ToolError("Query cannot be empty.")
    results = search_holdings(
        workspace=token.workspace,
        query=query,
        category=category,
        location=location_key,
        include_descendants=include_descendants,
        limit=min(max(limit, 1), 500),
    )
    clue_context = build_holding_clue_context(workspace=token.workspace, holdings=results)
    return {
        "workspace": token.workspace.slug,
        "query": query,
        "count": len(results),
        "results": [_serialize_holding(holding, clue_context) for holding in results],
    }


@server.tool(
    title="Get missing and low-stock items",
    description=(
        "Report workspace items below their configured minimum. Returns missing or low status "
        "and the quantity needed to reach the target."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def get_inventory_status(ctx: Context) -> dict[str, Any]:
    token = _token_from_context(ctx)
    return get_stock_status(workspace=token.workspace)


@server.tool(
    title="Look up book metadata by ISBN",
    description=(
        "Look up bibliographic metadata in Open Library and return a suggested item payload. "
        "This never writes inventory; confirm useful fields before calling bulk_upsert_inventory."
    ),
    annotations=EXTERNAL_READ,
    structured_output=True,
)
def lookup_book_by_isbn(isbn: str, ctx: Context) -> dict[str, Any]:
    _token_from_context(ctx)
    try:
        return lookup_book_catalog(isbn)
    except (ValueError, CatalogLookupError) as error:
        raise ToolError(str(error)) from error


@server.tool(
    title="Get inventory snapshot",
    description=(
        "Read locations, relative spatial relations, and holdings together. Use this when the user "
        "asks for an overview or when reasoning about how items could be reorganized."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def get_inventory_snapshot(
    ctx: Context,
    location_key: str = "",
    category: str = "",
    include_descendants: bool = True,
    limit: int = 500,
) -> dict[str, Any]:
    token = _token_from_context(ctx)
    workspace = token.workspace
    locations = Location.objects.filter(workspace=workspace).select_related("parent")
    holdings = Holding.objects.filter(workspace=workspace).select_related("item", "location")
    relations = LocationRelation.objects.filter(workspace=workspace).select_related(
        "subject", "object"
    )
    if location_key:
        scope_ids = location_scope_ids(
            workspace=workspace,
            location_key=location_key,
            include_descendants=include_descendants,
        )
        locations = locations.filter(id__in=scope_ids)
        holdings = holdings.filter(location_id__in=scope_ids)
        relations = relations.filter(Q(subject_id__in=scope_ids) | Q(object_id__in=scope_ids))
    if category:
        holdings = holdings.filter(item__category__iexact=category)
    bounded_limit = min(max(limit, 1), 2000)
    location_rows = list(locations[:bounded_limit])
    holding_rows = list(holdings[:bounded_limit])
    relation_rows = list(relations[:bounded_limit])
    clue_context = build_holding_clue_context(workspace=workspace, holdings=holding_rows)
    return {
        "workspace": workspace.slug,
        "locations": [
            {
                "key": location.key,
                "name": location.name,
                "kind": location.kind,
                "parent_key": location.parent.key if location.parent_id else None,
                "aliases": location.aliases,
                "metadata": location.metadata,
            }
            for location in location_rows
        ],
        "location_relations": [
            {
                "subject_key": relation.subject.key,
                "relation": relation.relation,
                "object_key": relation.object.key,
            }
            for relation in relation_rows
        ],
        "holdings": [_serialize_holding(holding, clue_context) for holding in holding_rows],
        "truncated": any(
            len(rows) == bounded_limit for rows in (location_rows, holding_rows, relation_rows)
        ),
    }


@server.tool(
    title="Bulk upsert inventory",
    description=(
        "Create or replace many locations, items, holdings, and relative location relations in one "
        "transaction. Quantities are set to the supplied current values. This writes immediately."
    ),
    annotations=IDEMPOTENT_WRITE,
    structured_output=True,
)
def bulk_upsert_inventory(
    idempotency_key: str,
    ctx: Context,
    provenance: dict[str, Any] | None = None,
    locations: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    location_relations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    token = _token_from_context(ctx)
    payload = {
        "idempotency_key": idempotency_key,
        "provenance": provenance or {},
        "locations": locations or [],
        "items": items or [],
        "holdings": holdings or [],
        "location_relations": location_relations or [],
    }
    serializer = BulkUpsertSerializer(data=payload)
    if not serializer.is_valid():
        raise ToolError(f"Invalid bulk upsert: {serializer.errors}")
    try:
        event, replayed = bulk_upsert_service(
            workspace=token.workspace,
            actor=token.user,
            data=serializer.validated_data,
            request_hash=hash_request(serializer.validated_data),
        )
    except (BulkUpsertError, IdempotencyConflict) as error:
        raise ToolError(str(error)) from error
    return {"event_id": str(event.id), "replayed": replayed, "processed": event.summary}


@server.tool(
    title="Move inventory",
    description=(
        "Move a quantity of one item between two known locations atomically. Use only after the "
        "client has applied its confirmation policy. This writes immediately."
    ),
    annotations=MOVE_WRITE,
    structured_output=True,
)
def move_inventory(
    item_key: str,
    from_location_key: str,
    to_location_key: str,
    quantity: str,
    idempotency_key: str,
    ctx: Context,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _token_from_context(ctx)
    provenance_serializer = ProvenanceSerializer(data=provenance or {})
    if not provenance_serializer.is_valid():
        raise ToolError(f"Invalid provenance: {provenance_serializer.errors}")
    request = {
        "item_key": item_key,
        "from_location_key": from_location_key,
        "to_location_key": to_location_key,
        "quantity": quantity,
        "idempotency_key": idempotency_key,
        "provenance": provenance_serializer.validated_data,
    }
    try:
        event, replayed = move_inventory_service(
            workspace=token.workspace,
            actor=token.user,
            item_key=item_key,
            from_location_key=from_location_key,
            to_location_key=to_location_key,
            quantity=quantity,
            idempotency_key=idempotency_key,
            provenance=provenance_serializer.validated_data,
            request_hash=hash_request(request),
        )
    except (BulkUpsertError, IdempotencyConflict) as error:
        raise ToolError(str(error)) from error
    return {"event_id": str(event.id), "replayed": replayed, "move": event.summary}
