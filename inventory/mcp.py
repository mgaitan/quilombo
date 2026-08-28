import json
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db.models import Q
from django.db.models.query import QuerySet
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import ValidationError

from .catalogs import CatalogLookupError, CatalogRecordNotFound
from .catalogs import lookup_book_by_isbn as lookup_book_catalog
from .models import Holding, InventoryEvent, Item, Location, LocationRelation
from .oauth import QuilomboOAuthProvider, resolve_inventory_token
from .serializers import (
    BulkUpsertSerializer,
    InventoryAuditSerializer,
    ItemDeleteSerializer,
    ItemRepairSerializer,
    ProvenanceSerializer,
)
from .services import (
    BulkUpsertError,
    IdempotencyConflict,
    InventoryConflictError,
    InventoryNotFoundError,
    add_search_match_details,
    build_holding_clue_context,
    get_stock_status,
    hash_request,
    location_scope_ids,
    search_holdings,
)
from .services import audit_inventory as audit_inventory_service
from .services import (
    bulk_upsert_inventory as bulk_upsert_service,
)
from .services import (
    delete_inventory_item as delete_inventory_item_service,
)
from .services import (
    move_inventory as move_inventory_service,
)
from .services import (
    update_inventory_item as update_inventory_item_service,
)

oauth_provider = QuilomboOAuthProvider()
_CURSOR_MAX_AGE = 15 * 60
_CURSOR_SIGNER = TimestampSigner(salt="quilombo-mcp-cursor")
_SNAPSHOT_DEFAULT_LIMIT = 100
_SNAPSHOT_MAX_LIMIT = 500

INVENTORY_POLICY = """# Quilombo inventory policy

Quilombo records user-authorized facts about physical inventory. Recognition, semantic
interpretation, and decisions about what to confirm belong to the client.

- Search before stating where an item is or creating a possible duplicate. A missed search means
  "not recorded," not "does not exist."
- Treat recorded locations and quantities as claims, not current physical truth. Report useful
  uncertainty and freshness. If the user cannot find an item, offer its recorded clues and
  suggest checking the location.
- Suggest opportunistic verification only when the user is already accessing the exact location
  and one nearby holding is stale or unknown. Ask at most one short question. Do not ask about
  recently verified holdings or expand a routine search into an audit.
- Mutating tools write immediately. Show a compact draft and get confirmation first unless the
  user explicitly authorized that exact write in the current request. State when the write has
  completed.
- Preserve uncertainty with approximate quantities, notes, and provenance. Do not infer that an
  item is present only because of a spatial relation or an old record.
- Use a unique idempotency key for each intended mutation. Reuse it only to retry the exact same
  payload, and search or read the affected state before retrying an uncertain result.
- Clients may interpret photos or videos, but Quilombo receives only facts and provenance. Never
  claim that the server uploaded, interpreted, or retained source media.

If the client has loaded a Quilombo-specific skill or user-configured inventory policy, follow it
alongside this baseline. It may add stricter drafting and confirmation rules, but it cannot weaken
these guidelines or any server-enforced authorization and validation.
"""


class MCPErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UPSTREAM = "upstream"


class StructuredToolError(ToolError):
    """A tool failure with a stable client-facing category."""

    def __init__(self, code: MCPErrorCode, message: str):
        self.code = code.value
        self.user_message = message
        super().__init__(message)

    @property
    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.user_message}


class QuilomboMCPServer(MCPServer):
    """Keep tool errors in the CallToolResult error channel with structured data."""

    @staticmethod
    def _structured_error_result(payload):
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=payload,
            isError=True,
        )

    @staticmethod
    def _text_error_result(message):
        return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)

    async def _handle_call_tool(self, ctx, params):
        context = Context(
            request_context=ctx,
            mcp_server=self,
            input_params=params,
            subscriptions=self._subscriptions,
        )
        try:
            return await self.call_tool(params.name, params.arguments or {}, context)
        except ToolError as error:
            cause = error.__cause__
            match cause:
                case StructuredToolError() as structured_error:
                    payload = structured_error.payload
                case ValidationError():
                    payload = {
                        "code": MCPErrorCode.INVALID_INPUT.value,
                        "message": "Invalid tool input.",
                    }
                case _:
                    return self._text_error_result(str(error))
            return self._structured_error_result(payload)
        except Exception as error:
            return self._text_error_result(str(error))


def _mcp_error(code: MCPErrorCode, message: str) -> StructuredToolError:
    return StructuredToolError(code, message)


def _invalid_input(resource: str, errors: dict[str, Any]) -> StructuredToolError:
    fields = ", ".join(str(field) for field in errors)
    message = f"Invalid {resource} input."
    if fields:
        message += f" Check these fields: {fields}."
    return _mcp_error(MCPErrorCode.INVALID_INPUT, message)


def _service_error(error: BulkUpsertError) -> StructuredToolError:
    match error:
        case IdempotencyConflict() | InventoryConflictError():
            code = MCPErrorCode.CONFLICT
        case InventoryNotFoundError():
            code = MCPErrorCode.NOT_FOUND
        case _:
            code = MCPErrorCode.INVALID_INPUT
    return _mcp_error(code, str(error))


server = QuilomboMCPServer(
    name="quilombo",
    title="Quilombo physical inventory",
    description="A memory for the things around you.",
    website_url=settings.PUBLIC_BASE_URL,
    icons=[
        Icon(
            src=f"{settings.PUBLIC_BASE_URL}{settings.STATIC_URL}inventory/quilombo-mark.png",
            mime_type="image/png",
            sizes=["64x64"],
        )
    ],
    version=settings.APP_VERSION,
    instructions=INVENTORY_POLICY,
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


def _token_from_context(ctx: Context):
    headers = ctx.headers or {}
    authorization = headers.get("authorization", "")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _mcp_error(MCPErrorCode.AUTHENTICATION, "A Quilombo bearer token is required.")
    token = resolve_inventory_token(parts[1])
    if not token:
        raise _mcp_error(
            MCPErrorCode.AUTHENTICATION,
            "The Quilombo bearer token is invalid or revoked.",
        )
    return token


@server.resource(
    "quilombo://guides/inventory-policy",
    name="inventory-policy",
    title="Quilombo inventory policy",
    description=(
        "Client guidance for searching, reporting freshness, verifying facts, and writing safely."
    ),
    mime_type="text/markdown",
)
def inventory_policy() -> str:
    return INVENTORY_POLICY


def _write_token_from_context(ctx: Context):
    token = _token_from_context(ctx)
    if not token.can_write:
        raise _mcp_error(MCPErrorCode.AUTHORIZATION, "This inventory is shared as read-only.")
    return token


def _with_mcp_provenance(data: dict[str, Any], ctx: Context) -> dict[str, Any]:
    enriched = dict(data)
    provenance = dict(enriched.get("provenance", {}))
    client_params = ctx.session.client_params
    if client_params:
        client_info = client_params.client_info
        provenance["_mcp_client"] = {
            "name": client_info.name,
            "version": client_info.version,
        }
    provenance.setdefault("source_kind", InventoryEvent.SourceKind.AGENT)
    enriched["provenance"] = provenance
    return enriched


def _encode_cursor(*, tool, workspace, filters, positions):
    return _CURSOR_SIGNER.sign_object(
        {
            "version": 1,
            "tool": tool,
            "workspace": workspace.slug,
            "filters": filters,
            "positions": positions,
        }
    )


def _decode_cursor(*, cursor, tool, workspace, filters, collections):
    if not cursor:
        return {collection: 0 for collection in collections}
    try:
        payload = _CURSOR_SIGNER.unsign_object(cursor, max_age=_CURSOR_MAX_AGE)
    except (BadSignature, SignatureExpired, ValueError, TypeError) as error:
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, "Invalid or expired cursor.") from error
    if not isinstance(payload, dict):
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, "Invalid or expired cursor.")
    if (
        payload.get("version") != 1
        or payload.get("tool") != tool
        or payload.get("workspace") != workspace.slug
        or payload.get("filters") != filters
    ):
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, "Invalid or expired cursor.")
    positions = payload.get("positions")
    if not isinstance(positions, dict) or set(positions) != set(collections):
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, "Invalid or expired cursor.")
    if any(not isinstance(position, int) or position < 0 for position in positions.values()):
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, "Invalid or expired cursor.")
    return positions


def _serialize_holding(holding, clue_context=None):
    clue_context = clue_context or {}
    serialized = {
        "holding_id": str(holding.id),
        "item_id": str(holding.item.id),
        "item_key": holding.item.key,
        "item_name": holding.item.name,
        "item_description": holding.item.description,
        "item_aliases": holding.item.aliases,
        "category": holding.item.category,
        "attributes": holding.item.attributes,
        "location_key": holding.location.key,
        "location_id": str(holding.location.id),
        "location_name": holding.location.name,
        "location_path": clue_context.get("location_paths", {}).get(holding.location_id, []),
        "nearby_items": clue_context.get("nearby_by_holding", {}).get(holding.id, []),
        "quantity": str(holding.quantity),
        "unit": holding.item.unit,
        "approximate": holding.approximate,
        "notes": holding.notes,
        "verification_status": holding.verification_status,
        "freshness": holding.freshness_status,
        "last_observed_at": (
            holding.last_observed_at.isoformat() if holding.last_observed_at else None
        ),
        "last_observed_by": (
            holding.last_observed_by.get_username() if holding.last_observed_by_id else None
        ),
    }
    if hasattr(holding, "_search_match"):
        serialized["search"] = holding._search_match
    return serialized


@server.tool(
    title="Find inventory",
    description=(
        "Find stored items and their precise locations using deterministic text, alias, category, "
        "attribute, and location matching. Results are ranked and explain matched and unmatched "
        "terms. Use this before telling a user where something is. Treat no match as not recorded, "
        "not proof that the item does not exist. Report relevant freshness and use nearby_items "
        "only for identification or one opportunistic check at the exact location. Use the "
        "returned "
        "next_cursor to continue a truncated result set without changing the filters."
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
    cursor: str = "",
) -> dict[str, Any]:
    token = _token_from_context(ctx)
    if not query.strip():
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, "Query cannot be empty.")
    bounded_limit = min(max(limit, 1), 500)
    filters = {
        "query": query,
        "category": category,
        "location_key": location_key,
        "include_descendants": include_descendants,
        "limit": bounded_limit,
    }
    positions = _decode_cursor(
        cursor=cursor,
        tool="find_inventory",
        workspace=token.workspace,
        filters=filters,
        collections=("results",),
    )
    results = search_holdings(
        workspace=token.workspace,
        query=query,
        category=category,
        location=location_key,
        include_descendants=include_descendants,
        limit=bounded_limit + 1,
        offset=positions["results"],
    )
    result_count = results.count() if isinstance(results, QuerySet) else len(results)
    truncated = result_count > bounded_limit
    results = results[:bounded_limit]
    add_search_match_details(results, query)
    next_cursor = None
    if truncated:
        next_cursor = _encode_cursor(
            tool="find_inventory",
            workspace=token.workspace,
            filters=filters,
            positions={"results": positions["results"] + bounded_limit},
        )
    clue_context = build_holding_clue_context(workspace=token.workspace, holdings=results)
    return {
        "workspace": token.workspace.slug,
        "query": query,
        "count": min(result_count, bounded_limit),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "results": [_serialize_holding(holding, clue_context) for holding in results],
    }


@server.tool(
    title="Get missing and low-stock items",
    description=(
        "Report recorded workspace quantities below their configured minimum. Returns missing or "
        "low status and the quantity needed to reach the target; it does not forecast consumption "
        "or prove what is physically present."
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
        "This never writes inventory. Confirm useful fields and carry the source URL and retrieval "
        "time into the provenance of any later bulk upsert."
    ),
    annotations=EXTERNAL_READ,
    structured_output=True,
)
def lookup_book_by_isbn(isbn: str, ctx: Context) -> dict[str, Any]:
    _token_from_context(ctx)
    try:
        return lookup_book_catalog(isbn)
    except ValueError as error:
        raise _mcp_error(MCPErrorCode.INVALID_INPUT, str(error)) from error
    except CatalogRecordNotFound as error:
        raise _mcp_error(MCPErrorCode.NOT_FOUND, str(error)) from error
    except CatalogLookupError as error:
        raise _mcp_error(MCPErrorCode.UPSTREAM, str(error)) from error


@server.tool(
    title="Get inventory snapshot",
    description=(
        "Read locations, relative spatial relations, and holdings together. Use this when the user "
        "asks for an overview, agrees to a broader location audit, or needs reorganization advice. "
        "For broad inventories, provide a location_key or category. Freshness describes records, "
        "not guaranteed physical presence. Use the returned next_cursor to continue a truncated "
        "collection set without changing the filters."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def get_inventory_snapshot(
    ctx: Context,
    location_key: str = "",
    category: str = "",
    include_descendants: bool = True,
    limit: int = _SNAPSHOT_DEFAULT_LIMIT,
    cursor: str = "",
) -> dict[str, Any]:
    token = _token_from_context(ctx)
    workspace = token.workspace
    bounded_limit = min(max(limit, 1), _SNAPSHOT_MAX_LIMIT)
    filters = {
        "location_key": location_key,
        "category": category,
        "include_descendants": include_descendants,
        "limit": bounded_limit,
    }
    positions = _decode_cursor(
        cursor=cursor,
        tool="get_inventory_snapshot",
        workspace=workspace,
        filters=filters,
        collections=("locations", "items", "holdings", "location_relations"),
    )
    locations = (
        Location.objects.filter(workspace=workspace)
        .select_related("parent", "last_observed_by")
        .order_by("key", "id")
    )
    items = Item.objects.filter(workspace=workspace).order_by("key", "id")
    holdings = (
        Holding.objects.filter(workspace=workspace)
        .select_related("item", "location", "last_observed_by")
        .order_by("item__key", "location__key", "id")
    )
    relations = (
        LocationRelation.objects.filter(workspace=workspace)
        .select_related("subject", "object")
        .order_by("subject__key", "relation", "object__key", "id")
    )
    if location_key:
        scope_ids = location_scope_ids(
            workspace=workspace,
            location_key=location_key,
            include_descendants=include_descendants,
        )
        locations = locations.filter(id__in=scope_ids)
        items = items.filter(holdings__location_id__in=scope_ids).distinct()
        holdings = holdings.filter(location_id__in=scope_ids)
        relations = relations.filter(Q(subject_id__in=scope_ids) | Q(object_id__in=scope_ids))
    if category:
        items = items.filter(category__iexact=category)
        holdings = holdings.filter(item__category__iexact=category)

    def page(queryset, collection):
        rows = list(queryset[positions[collection] : positions[collection] + bounded_limit + 1])
        return rows[:bounded_limit], len(rows) > bounded_limit

    location_rows, locations_truncated = page(locations, "locations")
    item_rows, items_truncated = page(items, "items")
    holding_rows, holdings_truncated = page(holdings, "holdings")
    relation_rows, relations_truncated = page(relations, "location_relations")
    truncated = {
        "locations": locations_truncated,
        "items": items_truncated,
        "holdings": holdings_truncated,
        "location_relations": relations_truncated,
    }
    next_cursor = None
    if any(truncated.values()):
        next_cursor = _encode_cursor(
            tool="get_inventory_snapshot",
            workspace=workspace,
            filters=filters,
            positions={
                collection: positions[collection] + bounded_limit for collection in positions
            },
        )
    clue_context = build_holding_clue_context(workspace=workspace, holdings=holding_rows)
    return {
        "workspace": workspace.slug,
        "limit": bounded_limit,
        "locations": [
            {
                "id": str(location.id),
                "key": location.key,
                "name": location.name,
                "kind": location.kind,
                "parent_key": location.parent.key if location.parent_id else None,
                "aliases": location.aliases,
                "metadata": location.metadata,
                "verification_status": location.verification_status,
                "freshness": location.freshness_status,
                "last_observed_at": (
                    location.last_observed_at.isoformat() if location.last_observed_at else None
                ),
                "last_observed_by": (
                    location.last_observed_by.get_username()
                    if location.last_observed_by_id
                    else None
                ),
            }
            for location in location_rows
        ],
        "items": [
            {
                "id": str(item.id),
                "key": item.key,
                "name": item.name,
                "description": item.description,
                "category": item.category,
                "aliases": item.aliases,
                "attributes": item.attributes,
                "tracking_mode": item.tracking_mode,
                "unit": item.unit,
            }
            for item in item_rows
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
        "truncated": any(truncated.values()),
        "truncated_collections": truncated,
        "next_cursor": next_cursor,
    }


@server.tool(
    title="Audit an inventory location",
    description=(
        "Record a location audit and its provenance. Confirm or mark the location and selected "
        "holdings unknown; optionally correct quantity, approximation, or notes for a known "
        "holding. Holdings omitted from the request are not changed. Draft corrections before "
        "calling; do not use routine searches as a reason to audit unrelated or recent facts."
    ),
    annotations=IDEMPOTENT_WRITE,
    structured_output=True,
)
def audit_inventory(
    location_key: str,
    location_status: str,
    idempotency_key: str,
    ctx: Context,
    holdings: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _write_token_from_context(ctx)
    payload = {
        "location_key": location_key,
        "location_status": location_status,
        "holdings": holdings or [],
        "idempotency_key": idempotency_key,
        "provenance": provenance or {},
    }
    serializer = InventoryAuditSerializer(data=payload)
    if not serializer.is_valid():
        raise _invalid_input("inventory audit", serializer.errors)
    request_hash = hash_request(serializer.validated_data)
    data = _with_mcp_provenance(serializer.validated_data, ctx)
    try:
        event, replayed = audit_inventory_service(
            workspace=token.workspace,
            actor=token.user,
            data=data,
            request_hash=request_hash,
        )
    except BulkUpsertError as error:
        raise _service_error(error) from error
    return {"event_id": str(event.id), "replayed": replayed, "audit": event.summary}


@server.tool(
    title="Bulk upsert inventory",
    description=(
        "Create or replace many locations, items, holdings, and relative location relations in "
        "one transaction. Search first to reuse known records. Quantities replace current values "
        "rather than adding deltas. Call only after the client has shown and confirmed the exact "
        "draft; this writes immediately."
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
    token = _write_token_from_context(ctx)
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
        raise _invalid_input("bulk upsert", serializer.errors)
    request_hash = hash_request(serializer.validated_data)
    data = _with_mcp_provenance(serializer.validated_data, ctx)
    try:
        event, replayed = bulk_upsert_service(
            workspace=token.workspace,
            actor=token.user,
            data=data,
            request_hash=request_hash,
        )
    except BulkUpsertError as error:
        raise _service_error(error) from error
    return {"event_id": str(event.id), "replayed": replayed, "processed": event.summary}


@server.tool(
    title="Move inventory",
    description=(
        "Move a quantity of one item between two known locations atomically. Use only after the "
        "client has applied its confirmation policy. This writes immediately."
    ),
    annotations=IDEMPOTENT_WRITE,
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
    token = _write_token_from_context(ctx)
    provenance_serializer = ProvenanceSerializer(data=provenance or {})
    if not provenance_serializer.is_valid():
        raise _invalid_input("provenance", provenance_serializer.errors)
    request = {
        "item_key": item_key,
        "from_location_key": from_location_key,
        "to_location_key": to_location_key,
        "quantity": quantity,
        "idempotency_key": idempotency_key,
        "provenance": provenance_serializer.validated_data,
    }
    provenance = _with_mcp_provenance({"provenance": provenance_serializer.validated_data}, ctx)[
        "provenance"
    ]
    try:
        event, replayed = move_inventory_service(
            workspace=token.workspace,
            actor=token.user,
            item_key=item_key,
            from_location_key=from_location_key,
            to_location_key=to_location_key,
            quantity=quantity,
            idempotency_key=idempotency_key,
            provenance=provenance,
            request_hash=hash_request(request),
        )
    except BulkUpsertError as error:
        raise _service_error(error) from error
    return {"event_id": str(event.id), "replayed": replayed, "move": event.summary}


@server.tool(
    title="Update an inventory item",
    description=(
        "Correct a known item and optionally its known holdings by stable UUID. Search first and "
        "supply only confirmed fields. Holding location_id moves that complete holding; quantity "
        "replaces its current quantity. This writes immediately."
    ),
    annotations=IDEMPOTENT_WRITE,
    structured_output=True,
)
def update_inventory_item(
    item_id: str,
    idempotency_key: str,
    ctx: Context,
    item: dict[str, Any] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _write_token_from_context(ctx)
    payload = {
        "item_id": item_id,
        "idempotency_key": idempotency_key,
        "item": item or {},
        "holdings": holdings or [],
        "provenance": provenance or {},
    }
    serializer = ItemRepairSerializer(data=payload)
    if not serializer.is_valid():
        raise _invalid_input("item update", serializer.errors)
    request_hash = hash_request(serializer.validated_data)
    data = _with_mcp_provenance(serializer.validated_data, ctx)
    try:
        event, replayed = update_inventory_item_service(
            workspace=token.workspace,
            actor=token.user,
            data=data,
            request_hash=request_hash,
        )
    except BulkUpsertError as error:
        raise _service_error(error) from error
    return {"event_id": str(event.id), "replayed": replayed, "processed": event.summary}


@server.tool(
    title="Delete an erroneous inventory item",
    description=(
        "Delete a known erroneous or duplicate item and its holdings by stable UUID. Search first "
        "and use only after the client has enough evidence and applies its confirmation policy."
    ),
    annotations=IDEMPOTENT_WRITE,
    structured_output=True,
)
def delete_inventory_item(
    item_id: str,
    idempotency_key: str,
    ctx: Context,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _write_token_from_context(ctx)
    payload = {
        "item_id": item_id,
        "idempotency_key": idempotency_key,
        "provenance": provenance or {},
    }
    serializer = ItemDeleteSerializer(data=payload)
    if not serializer.is_valid():
        raise _invalid_input("item deletion", serializer.errors)
    request_hash = hash_request(serializer.validated_data)
    data = _with_mcp_provenance(serializer.validated_data, ctx)
    try:
        event, replayed = delete_inventory_item_service(
            workspace=token.workspace,
            actor=token.user,
            data=data,
            request_hash=request_hash,
        )
    except BulkUpsertError as error:
        raise _service_error(error) from error
    return {"event_id": str(event.id), "replayed": replayed, "processed": event.summary}
