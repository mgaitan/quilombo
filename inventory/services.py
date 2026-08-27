import hashlib
import json
import re
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib.postgres.lookups import Unaccent
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import (
    BooleanField,
    Case,
    DecimalField,
    FloatField,
    Q,
    Sum,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from .models import Holding, InventoryEvent, Item, Location, LocationRelation, Membership, Workspace
from .state import capture_inventory_state, inventory_state_hash, restore_inventory_state


class BulkUpsertError(Exception):
    pass


class IdempotencyConflict(BulkUpsertError):
    pass


class InventoryUndoError(BulkUpsertError):
    pass


def event_metadata_from_provenance(provenance):
    metadata = dict(provenance.get("metadata", {}))
    metadata.pop("server_mcp_client", None)
    if mcp_client := provenance.get("_mcp_client"):
        metadata["server_mcp_client"] = mcp_client
    return metadata


@transaction.atomic
def create_workspace(*, user, name):
    base = slugify(name)[:60] or "inventory"
    workspace = Workspace.objects.create(name=name, slug=f"{base}-{uuid.uuid4().hex[:10]}")
    Membership.objects.create(
        workspace=workspace,
        user=user,
        role=Membership.Role.OWNER,
        can_write=True,
    )
    return workspace


@transaction.atomic
def rename_workspace(*, workspace, name):
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    locked.name = name
    locked.save(update_fields=["name"])
    return locked


@transaction.atomic
def share_workspace(*, workspace, user, can_write=True):
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    membership = Membership.objects.select_for_update().filter(workspace=locked, user=user).first()
    if membership:
        if membership.role != Membership.Role.OWNER:
            membership.can_write = can_write
            membership.save(update_fields=["can_write"])
        return membership
    membership = Membership.objects.create(
        workspace=locked,
        user=user,
        role=Membership.Role.MEMBER,
        can_write=can_write,
    )
    return membership


@transaction.atomic
def update_workspace_member(*, workspace, user_id, can_write):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    membership = Membership.objects.select_for_update().get(workspace=workspace, user_id=user_id)
    if membership.role == Membership.Role.OWNER:
        return membership
    membership.can_write = can_write
    membership.save(update_fields=["can_write"])
    return membership


@transaction.atomic
def remove_workspace_member(*, workspace, user_id):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    membership = Membership.objects.select_for_update().get(workspace=workspace, user_id=user_id)
    if membership.role == Membership.Role.OWNER:
        return False
    membership.delete()
    return True


@transaction.atomic
def create_location(*, workspace, data):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    location = Location(workspace=workspace, **data)
    location.full_clean()
    location.save()
    return location


@transaction.atomic
def update_location(*, workspace, location, data):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    location = Location.objects.select_for_update().get(pk=location.pk, workspace=workspace)
    for field, value in data.items():
        setattr(location, field, value)
    location.full_clean()
    location.save()
    return location


@transaction.atomic
def create_item_with_holding(*, workspace, item_data, holding_data):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    item = Item(workspace=workspace, **item_data)
    item.full_clean()
    item.save()
    holding = Holding(workspace=workspace, item=item, **holding_data)
    holding.full_clean()
    holding.save()
    return item


@transaction.atomic
def update_item(*, workspace, item, data, actor):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    item = Item.objects.select_for_update().get(pk=item.pk, workspace=workspace)
    changed_fields = sorted(field for field, value in data.items() if getattr(item, field) != value)
    if data.get("tracking_mode", item.tracking_mode) == Item.TrackingMode.DISCRETE:
        quantities = (
            Holding.objects.select_for_update()
            .filter(workspace=workspace, item=item)
            .values_list("quantity", flat=True)
        )
        if any(quantity != quantity.to_integral_value() for quantity in quantities):
            raise ValidationError(
                "All holdings must have whole quantities before using discrete tracking."
            )
    for field, value in data.items():
        setattr(item, field, value)
    item.full_clean()
    item.save()
    InventoryEvent.objects.create(
        workspace=workspace,
        actor=actor,
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        source_kind=InventoryEvent.SourceKind.MANUAL,
        summary={
            "item_id": str(item.id),
            "item_key": item.key,
            "item_fields": changed_fields,
        },
    )
    return item


@transaction.atomic
def remove_item(*, workspace, item):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    item = Item.objects.select_for_update().get(pk=item.pk, workspace=workspace)
    item.delete()


@transaction.atomic
def create_holding(*, workspace, item, data):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    item = Item.objects.select_for_update().get(pk=item.pk, workspace=workspace)
    holding = Holding(workspace=workspace, item=item, **data)
    holding.full_clean()
    holding.save()
    return holding


@transaction.atomic
def update_holding(*, workspace, item, holding, data):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    holding = Holding.objects.select_for_update().get(pk=holding.pk, workspace=workspace, item=item)
    for field, value in data.items():
        setattr(holding, field, value)
    holding.full_clean()
    holding.save()
    return holding


@transaction.atomic
def remove_holding(*, workspace, item, holding):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    holding = Holding.objects.select_for_update().get(pk=holding.pk, workspace=workspace, item=item)
    holding.delete()


SEARCH_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
SEARCH_FIELD_WEIGHTS = {
    "item_key": 10,
    "item_name": 9,
    "item_aliases": 8,
    "item_category": 7,
    "item_attributes": 6,
    "item_description": 5,
    "location_key": 4,
    "location_name": 3,
    "location_aliases": 2,
    "location_description": 2,
    "location_kind": 2,
    "holding_notes": 1,
    "location_metadata": 1,
}
SEARCH_FIELD_LABELS = {
    "item_key": "item key",
    "item_name": "item name",
    "item_aliases": "item alias",
    "item_category": "category",
    "item_attributes": "attribute",
    "item_description": "item description",
    "location_key": "location key",
    "location_name": "location name",
    "location_aliases": "location alias",
    "location_description": "location description",
    "location_kind": "location kind",
    "holding_notes": "holding notes",
    "location_metadata": "location metadata",
}
SEARCH_MAX_CANDIDATES = 5000


def normalize_search_text(value):
    """Fold accents and punctuation while keeping compact technical codes searchable."""
    folded = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(SEARCH_TOKEN_RE.findall(ascii_text.casefold()))


def normalize_aliases(values):
    """Trim and de-duplicate aliases without losing the user's original spelling."""
    aliases = []
    seen = set()
    for value in values or []:
        alias = str(value).strip()
        normalized = normalize_search_text(alias)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(alias)
    return aliases


def _search_tokens(value):
    return normalize_search_text(value).split()


def _query_terms(query):
    """Tokenize punctuation-separated terms while retaining useful display labels."""
    terms = []
    for raw_chunk in str(query or "").split():
        normalized_tokens = _search_tokens(raw_chunk)
        if len(normalized_tokens) == 1:
            terms.append((raw_chunk, normalized_tokens[0]))
        else:
            terms.extend((token, token) for token in normalized_tokens)
    return terms


def _candidate_terms(terms):
    values = set()
    for raw_term, normalized_term in terms:
        values.add(raw_term)
        values.add(normalized_term)
        values.update(_token_variants(normalized_term))
    return {value for value in values if value}


def _candidate_holdings(queryset, terms, limit):
    """Use PostgreSQL as a coarse prefilter before Python normalization and ranking."""
    candidate_filter = Q()
    fields = (
        "item__key",
        "item__name",
        "item__description",
        "item__category",
        "item_aliases_text",
        "item_attributes_text",
        "location__key",
        "location__name",
        "location__description",
        "location__kind",
        "location_aliases_text",
        "location_metadata_text",
        "notes",
    )
    annotated = queryset.annotate(
        item_aliases_text=Cast("item__aliases", TextField()),
        item_attributes_text=Cast("item__attributes", TextField()),
        location_aliases_text=Cast("location__aliases", TextField()),
        location_metadata_text=Cast("location__metadata", TextField()),
    )
    for term in _candidate_terms(terms):
        for field in fields:
            candidate_filter |= Q(**{f"{field}__icontains": term})
    return list(
        annotated.filter(candidate_filter).order_by("item__name", "location__name", "id")[:limit]
    )


def _postgres_search_holdings(queryset, terms, limit, offset=0):
    """Search and rank holdings in PostgreSQL before Django evaluates the page."""
    fields = (
        ("item_key", "item__key", "A"),
        ("item_name", "item__name", "A"),
        ("item_aliases", Cast("item__aliases", TextField()), "B"),
        ("item_category", "item__category", "B"),
        ("item_attributes", Cast("item__attributes", TextField()), "C"),
        ("item_description", "item__description", "C"),
        ("location_key", "location__key", "C"),
        ("location_name", "location__name", "C"),
        ("location_aliases", Cast("location__aliases", TextField()), "D"),
        ("location_description", "location__description", "D"),
        ("location_kind", "location__kind", "D"),
        ("holding_notes", "notes", "D"),
        ("location_metadata", Cast("location__metadata", TextField()), "D"),
    )
    search_vector = sum(
        (
            SearchVector(Unaccent(expression), weight=weight, config="simple")
            for _, expression, weight in fields
        ),
        SearchVector(Value(""), config="simple"),
    )
    item_index_vector = SearchVector(
        "item__key",
        "item__name",
        "item__description",
        "item__category",
        config="simple",
    )
    location_index_vector = SearchVector(
        "location__key",
        "location__name",
        "location__description",
        "location__kind",
        config="simple",
    )
    conditions = []
    for _raw_term, term in terms:
        variants = _token_variants(term)
        if _is_exact_token(term):
            raw_query = term
        else:
            raw_query = " | ".join(f"{variant}:*" for variant in variants)
        term_query = SearchQuery(raw_query, search_type="raw", config="simple")
        conditions.append(
            Q(search_vector=term_query)
            | Q(item_index_vector=term_query)
            | Q(location_index_vector=term_query)
        )

    complete = Q()
    any_match = Q()
    score = Value(0.0, output_field=FloatField())
    for condition, (_raw_term, term) in zip(conditions, terms):
        any_match |= condition
        complete &= condition
        score += Case(
            When(condition, then=Value(max(1, len(term)))),
            default=Value(0),
            output_field=FloatField(),
        )

    rank_query = SearchQuery(
        " | ".join(f"{variant}:*" for _, term in terms for variant in _token_variants(term)),
        search_type="raw",
        config="simple",
    )
    ranked_queryset = queryset.annotate(
        search_vector=search_vector,
        item_index_vector=item_index_vector,
        location_index_vector=location_index_vector,
    )
    if ranked_queryset.filter(complete).exists():
        ranked_queryset = ranked_queryset.filter(complete)
    else:
        ranked_queryset = ranked_queryset.filter(any_match)
    return ranked_queryset.annotate(
        search_complete=Case(
            When(complete, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
        search_score=score,
        search_rank=SearchRank("search_vector", rank_query),
    ).order_by(
        "-search_complete",
        "-search_score",
        "-search_rank",
        "item__name",
        "location__name",
        "id",
    )[offset : offset + limit]


def add_search_match_details(holdings, query):
    """Explain only the already-paginated rows returned by the database."""
    terms = _query_terms(query)
    for holding in holdings:
        holding._search_match = _score_holding(holding, query, terms)
    return holdings


def _is_exact_token(term):
    """Avoid treating AA as a substring of AAA or 35 as a substring of 35mm."""
    return len(term) <= 3 or any(character.isdigit() for character in term)


def _token_variants(term):
    variants = {term}
    if len(term) > 3 and term.endswith("y"):
        variants.add(f"{term[:-1]}ies")
    if len(term) > 4 and term.endswith("ies"):
        variants.add(f"{term[:-3]}y")
    if len(term) > 3 and term.endswith("s"):
        variants.add(term[:-1])
    if len(term) > 3 and term.endswith(("a", "e", "o")):
        variants.add(f"{term}s")
    return variants


def _term_matches(term, candidate_tokens, *, reverse_prefix=True):
    if _is_exact_token(term):
        return term in candidate_tokens
    variants = _token_variants(term)
    return any(
        candidate == variant
        or candidate.startswith(variant)
        or (reverse_prefix and variant.startswith(candidate))
        for candidate in candidate_tokens
        for variant in variants
    )


def _search_fields(holding):
    item = holding.item
    location = holding.location
    return {
        "item_key": item.key,
        "item_name": item.name,
        "item_aliases": " ".join(item.aliases or []),
        "item_category": item.category,
        "item_attributes": json.dumps(item.attributes or {}, ensure_ascii=False, sort_keys=True),
        "item_description": item.description,
        "location_key": location.key,
        "location_name": location.name,
        "location_aliases": " ".join(location.aliases or []),
        "location_description": location.description,
        "location_kind": location.kind,
        "holding_notes": holding.notes,
        "location_metadata": json.dumps(
            location.metadata or {}, ensure_ascii=False, sort_keys=True
        ),
    }


def _score_holding(holding, query, terms, *, reverse_prefix=True):
    fields = _search_fields(holding)
    field_tokens = {field: _search_tokens(value) for field, value in fields.items()}
    matched_terms = []
    matched_fields = {}
    score = 0
    for raw_term, term in terms:
        matching_fields = [
            field
            for field, tokens in field_tokens.items()
            if _term_matches(term, tokens, reverse_prefix=reverse_prefix)
        ]
        if matching_fields:
            matched_terms.append(raw_term)
            matched_fields[raw_term] = [SEARCH_FIELD_LABELS[field] for field in matching_fields]
            score += max(SEARCH_FIELD_WEIGHTS[field] for field in matching_fields)

    normalized_query = normalize_search_text(query)
    normalized_fields = [normalize_search_text(value) for value in fields.values()]
    exact_phrase = bool(
        normalized_query and any(normalized_query in value for value in normalized_fields)
    )
    if exact_phrase:
        score += 15

    coverage = len(matched_terms) / len(terms) if terms else 1
    details = {
        "score": round(coverage * 100 + score, 2),
        "matched_terms": matched_terms,
        "unmatched_terms": [raw_term for raw_term, _ in terms if raw_term not in matched_terms],
        "matched_fields": matched_fields,
        "match_type": "complete" if len(matched_terms) == len(terms) else "partial",
    }
    return details


def location_scope_ids(*, workspace, location_key, include_descendants=True):
    rows = list(workspace.locations.values_list("id", "parent_id", "key"))
    matching_ids = {location_id for location_id, _, key in rows if key == location_key}
    if not include_descendants or not matching_ids:
        return matching_ids

    children_by_parent = {}
    for location_id, parent_id, _location_key in rows:
        children_by_parent.setdefault(parent_id, set()).add(location_id)

    pending = list(matching_ids)
    while pending:
        children = children_by_parent.get(pending.pop(), set()) - matching_ids
        matching_ids.update(children)
        pending.extend(children)
    return matching_ids


def search_holdings(
    *, workspace, query, category="", location="", include_descendants=True, limit=100, offset=0
):
    holdings_query = Holding.objects.filter(workspace=workspace).select_related(
        "item", "location", "last_observed_by"
    )
    if category:
        holdings_query = holdings_query.filter(item__category__iexact=category)
    if location:
        holdings_query = holdings_query.filter(
            location_id__in=location_scope_ids(
                workspace=workspace,
                location_key=location,
                include_descendants=include_descendants,
            )
        )
    terms = _query_terms(query)
    if not terms:
        return list(
            holdings_query.order_by("item__name", "location__name", "id")[offset : offset + limit]
        )

    if connection.vendor == "postgresql":
        return _postgres_search_holdings(holdings_query, terms, limit, offset)

    candidate_limit = min(max(limit * 20, 1000), SEARCH_MAX_CANDIDATES)
    holdings = _candidate_holdings(holdings_query, terms, candidate_limit)
    if not holdings:
        # A database-side substring search cannot remove accents without the optional
        # PostgreSQL unaccent extension. Keep this bounded fallback for accent-only misses.
        holdings = list(
            holdings_query.order_by("item__name", "location__name", "id")[:candidate_limit]
        )

    ranked = []
    for holding in holdings:
        details = _score_holding(holding, query, terms, reverse_prefix=False)
        if details["matched_terms"]:
            holding._search_match = details
            ranked.append(holding)

    has_complete_matches = any(
        holding._search_match["match_type"] == "complete" for holding in ranked
    )
    if has_complete_matches:
        ranked = [
            holding for holding in ranked if holding._search_match["match_type"] == "complete"
        ]
    ranked.sort(
        key=lambda holding: (
            -int(holding._search_match["match_type"] != "complete"),
            -holding._search_match["score"],
            holding.item.name.casefold(),
            holding.location.name.casefold(),
            str(holding.id),
        )
    )
    results = ranked[offset : offset + limit]
    return add_search_match_details(results, query)


def build_holding_clue_context(*, workspace, holdings, nearby_limit=5):
    holding_rows = list(holdings)
    location_rows = list(workspace.locations.values("id", "parent_id", "key", "name"))
    locations_by_id = {row["id"]: row for row in location_rows}

    location_paths = {}
    for location_id in {holding.location_id for holding in holding_rows}:
        path = []
        current_id = location_id
        seen = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            location = locations_by_id.get(current_id)
            if not location:
                break
            path.append({"key": location["key"], "name": location["name"]})
            current_id = location["parent_id"]
        location_paths[location_id] = list(reversed(path))

    location_ids = {holding.location_id for holding in holding_rows}
    colocated = (
        Holding.objects.filter(workspace=workspace, location_id__in=location_ids, quantity__gt=0)
        .select_related("item")
        .order_by("item__name")
    )
    colocated_by_location = {}
    for holding in colocated:
        colocated_by_location.setdefault(holding.location_id, []).append(holding)

    nearby_by_holding = {}
    for holding in holding_rows:
        nearby_by_holding[holding.id] = [
            {
                "holding_id": str(neighbor.id),
                "item_key": neighbor.item.key,
                "item_name": neighbor.item.name,
                "description": neighbor.item.description,
                "attributes": neighbor.item.attributes,
                "verification_status": neighbor.verification_status,
                "freshness": neighbor.freshness_status,
                "last_observed_at": (
                    neighbor.last_observed_at.isoformat() if neighbor.last_observed_at else None
                ),
            }
            for neighbor in colocated_by_location.get(holding.location_id, [])
            if neighbor.item_id != holding.item_id
        ][:nearby_limit]

    return {
        "location_paths": location_paths,
        "nearby_by_holding": nearby_by_holding,
    }


def get_stock_status(*, workspace):
    items = workspace.items.filter(minimum_quantity__isnull=False).annotate(
        current_quantity=Coalesce(
            Sum("holdings__quantity"),
            Value(Decimal("0"), output_field=DecimalField(max_digits=20, decimal_places=6)),
        )
    )
    low_items = [item for item in items if item.current_quantity < item.minimum_quantity]
    holdings_by_item = {}
    if low_items:
        holdings = (
            Holding.objects.filter(
                workspace=workspace,
                item_id__in=[item.id for item in low_items],
            )
            .select_related("location")
            .order_by("item_id", "location__name", "id")
        )
        for holding in holdings:
            holdings_by_item.setdefault(holding.item_id, []).append(holding)

    attention = []
    for item in low_items:
        current = item.current_quantity
        holdings = holdings_by_item.get(item.id, [])
        target = item.target_quantity or item.minimum_quantity
        attention.append(
            {
                "item_key": item.key,
                "item_name": item.name,
                "status": "missing" if current == 0 else "low",
                "current_quantity": current,
                "minimum_quantity": item.minimum_quantity,
                "target_quantity": target,
                "recommended_add_quantity": max(target - current, Decimal("0")),
                "unit": item.unit,
                "locations": [
                    {
                        "location_key": holding.location.key,
                        "location_name": holding.location.name,
                        "quantity": str(holding.quantity),
                    }
                    for holding in holdings
                    if holding.quantity > 0
                ],
            }
        )
    attention.sort(key=lambda row: (row["status"] != "missing", row["item_name"].lower()))
    return {
        "workspace": workspace.slug,
        "count": len(attention),
        "items": attention,
    }


def hash_request(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@transaction.atomic
def audit_inventory(*, workspace, actor, data, request_hash):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    if event := _replayed_event(workspace=workspace, data=data, request_hash=request_hash):
        return event, True

    location = (
        Location.objects.select_for_update()
        .filter(workspace=workspace, key=data["location_key"])
        .first()
    )
    if not location:
        raise BulkUpsertError(f"Unknown location '{data['location_key']}'.")

    rows = data.get("holdings", [])
    holdings = {
        holding.id: holding
        for holding in Holding.objects.select_for_update()
        .filter(
            workspace=workspace,
            location=location,
            id__in=[row["holding_id"] for row in rows],
        )
        .select_related("item")
    }
    if len(holdings) != len(rows):
        raise BulkUpsertError("An audited holding was not found at this location.")

    observed_at = data.get("provenance", {}).get("observed_at") or timezone.now()
    if location.last_observed_at and observed_at < location.last_observed_at:
        raise BulkUpsertError("The location has a newer observation than this audit.")
    if any(
        holding.last_observed_at and observed_at < holding.last_observed_at
        for holding in holdings.values()
    ):
        raise BulkUpsertError("A holding has a newer observation than this audit.")
    location.verification_status = data["location_status"]
    location.last_observed_at = observed_at
    location.last_observed_by = actor
    location.save(
        update_fields=["verification_status", "last_observed_at", "last_observed_by", "updated_at"]
    )

    summaries = []
    for row in rows:
        holding = holdings[row["holding_id"]]
        corrected_fields = []
        for field in ("quantity", "approximate", "notes"):
            if field in row and getattr(holding, field) != row[field]:
                setattr(holding, field, row[field])
                corrected_fields.append(field)
        holding.verification_status = row["status"]
        holding.last_observed_at = observed_at
        holding.last_observed_by = actor
        holding.full_clean()
        holding.save()
        summaries.append(
            {
                "holding_id": str(holding.id),
                "item_key": holding.item.key,
                "status": row["status"],
                "corrected_fields": corrected_fields,
            }
        )

    event = _mutation_event(
        workspace=workspace,
        actor=actor,
        kind=InventoryEvent.Kind.AUDIT,
        data=data,
        request_hash=request_hash,
        summary={
            "location_key": location.key,
            "location_status": data["location_status"],
            "holdings": summaries,
        },
    )
    return event, False


def _validate_location_hierarchy(parent_by_key):
    for key in parent_by_key:
        current = key
        seen = set()
        while current:
            if current in seen:
                raise BulkUpsertError(f"Location hierarchy contains a cycle at '{current}'.")
            seen.add(current)
            current = parent_by_key.get(current)


@transaction.atomic
def bulk_upsert_inventory(*, workspace, actor, data, request_hash):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    idempotency_key = data["idempotency_key"]
    existing_event = InventoryEvent.objects.filter(
        workspace=workspace, idempotency_key=idempotency_key
    ).first()
    if existing_event:
        if existing_event.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was already used with a different payload.")
        return existing_event, True

    before_state = capture_inventory_state(workspace)

    location_rows = data.get("locations", [])
    item_rows = data.get("items", [])
    holding_rows = data.get("holdings", [])
    relation_rows = data.get("location_relations", [])
    now = timezone.now()

    locations = [
        Location(
            workspace=workspace,
            key=row["key"],
            name=row["name"],
            description=row.get("description", ""),
            kind=row.get("kind", ""),
            aliases=normalize_aliases(row.get("aliases", [])),
            metadata=row.get("metadata", {}),
            updated_at=now,
        )
        for row in location_rows
    ]
    if locations:
        Location.objects.bulk_create(
            locations,
            update_conflicts=True,
            unique_fields=["workspace", "key"],
            update_fields=[
                "name",
                "description",
                "kind",
                "aliases",
                "metadata",
                "verification_status",
                "last_observed_at",
                "last_observed_by",
                "updated_at",
            ],
        )

    location_map = {location.key: location for location in workspace.locations.all()}
    parent_by_key = {
        location.key: location.parent.key if location.parent_id else None
        for location in workspace.locations.select_related("parent")
    }
    for row in location_rows:
        parent_key = row.get("parent_key")
        if parent_key and parent_key not in location_map:
            raise BulkUpsertError(f"Unknown parent location '{parent_key}'.")
        parent_by_key[row["key"]] = parent_key
    _validate_location_hierarchy(parent_by_key)

    changed_parents = []
    for row in location_rows:
        location = location_map[row["key"]]
        parent_key = row.get("parent_key")
        parent_id = location_map[parent_key].id if parent_key else None
        if location.parent_id != parent_id:
            location.parent_id = parent_id
            location.verification_status = "unknown"
            location.last_observed_at = None
            location.last_observed_by = None
            changed_parents.append(location)
    if changed_parents:
        Location.objects.bulk_update(
            changed_parents,
            ["parent", "verification_status", "last_observed_at", "last_observed_by"],
        )

    items = [
        Item(
            workspace=workspace,
            key=row["key"],
            name=row["name"],
            description=row.get("description", ""),
            category=row.get("category", ""),
            aliases=normalize_aliases(row.get("aliases", [])),
            attributes=row.get("attributes", {}),
            tracking_mode=row.get("tracking_mode", Item.TrackingMode.BULK),
            unit=row.get("unit", "unit"),
            minimum_quantity=row.get("minimum_quantity"),
            target_quantity=row.get("target_quantity"),
            updated_at=now,
        )
        for row in item_rows
    ]
    if items:
        Item.objects.bulk_create(
            items,
            update_conflicts=True,
            unique_fields=["workspace", "key"],
            update_fields=[
                "name",
                "description",
                "category",
                "aliases",
                "attributes",
                "tracking_mode",
                "unit",
                "minimum_quantity",
                "target_quantity",
                "updated_at",
            ],
        )

    item_map = {item.key: item for item in workspace.items.all()}
    holdings = []
    for row in holding_rows:
        item = item_map.get(row["item_key"])
        location = location_map.get(row["location_key"])
        if not item:
            raise BulkUpsertError(f"Unknown item '{row['item_key']}'.")
        if not location:
            raise BulkUpsertError(f"Unknown location '{row['location_key']}'.")
        quantity = row["quantity"]
        if item.tracking_mode == Item.TrackingMode.DISCRETE and quantity != int(quantity):
            raise BulkUpsertError(f"Discrete item '{item.key}' requires a whole quantity.")
        holdings.append(
            Holding(
                workspace=workspace,
                item=item,
                location=location,
                quantity=quantity,
                approximate=row.get("approximate", False),
                notes=row.get("notes", ""),
                updated_at=now,
            )
        )
    if holdings:
        Holding.objects.bulk_create(
            holdings,
            update_conflicts=True,
            unique_fields=["workspace", "item", "location"],
            update_fields=[
                "quantity",
                "approximate",
                "notes",
                "verification_status",
                "last_observed_at",
                "last_observed_by",
                "updated_at",
            ],
        )

    relations = []
    for row in relation_rows:
        subject = location_map.get(row["subject_key"])
        object_ = location_map.get(row["object_key"])
        if not subject:
            raise BulkUpsertError(f"Unknown location '{row['subject_key']}'.")
        if not object_:
            raise BulkUpsertError(f"Unknown location '{row['object_key']}'.")
        if subject == object_:
            raise BulkUpsertError("A location relation requires two different locations.")
        relations.append(
            LocationRelation(
                workspace=workspace,
                subject=subject,
                relation=row["relation"],
                object=object_,
            )
        )
    if relations:
        LocationRelation.objects.bulk_create(relations, ignore_conflicts=True)

    summary = {
        "locations": len(location_rows),
        "items": len(item_rows),
        "holdings": len(holding_rows),
        "location_relations": len(relation_rows),
    }
    provenance = data.get("provenance", {})
    event = InventoryEvent.objects.create(
        workspace=workspace,
        kind=InventoryEvent.Kind.BULK_UPSERT,
        actor=actor,
        client_actor=provenance.get("client_actor", ""),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        source_kind=provenance.get("source_kind", InventoryEvent.SourceKind.MANUAL),
        source_reference=provenance.get("source_reference", ""),
        observed_at=provenance.get("observed_at"),
        metadata=event_metadata_from_provenance(provenance),
        summary=summary,
        undo_data={
            "before": before_state,
            "after_hash": inventory_state_hash(capture_inventory_state(workspace)),
        },
    )
    return event, False


@transaction.atomic
def move_inventory(
    *,
    workspace,
    actor,
    item_key,
    from_location_key,
    to_location_key,
    quantity,
    idempotency_key,
    provenance,
    request_hash,
):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    existing_event = InventoryEvent.objects.filter(
        workspace=workspace, idempotency_key=idempotency_key
    ).first()
    if existing_event:
        if existing_event.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was already used with a different payload.")
        return existing_event, True
    before_state = capture_inventory_state(workspace)
    if from_location_key == to_location_key:
        raise BulkUpsertError("Source and destination locations must differ.")
    try:
        amount = Decimal(str(quantity))
    except InvalidOperation as error:
        raise BulkUpsertError("Quantity must be a decimal number.") from error
    if amount <= 0:
        raise BulkUpsertError("Quantity must be greater than zero.")

    item = Item.objects.filter(workspace=workspace, key=item_key).first()
    source_location = Location.objects.filter(workspace=workspace, key=from_location_key).first()
    destination_location = Location.objects.filter(workspace=workspace, key=to_location_key).first()
    if not item:
        raise BulkUpsertError(f"Unknown item '{item_key}'.")
    if not source_location:
        raise BulkUpsertError(f"Unknown location '{from_location_key}'.")
    if not destination_location:
        raise BulkUpsertError(f"Unknown location '{to_location_key}'.")
    if item.tracking_mode == Item.TrackingMode.DISCRETE and amount != amount.to_integral_value():
        raise BulkUpsertError(f"Discrete item '{item.key}' requires a whole quantity.")

    source = (
        Holding.objects.select_for_update()
        .filter(workspace=workspace, item=item, location=source_location)
        .first()
    )
    if not source or source.quantity < amount:
        available = source.quantity if source else Decimal("0")
        raise BulkUpsertError(
            f"Insufficient quantity at source; available quantity is {available}."
        )

    destination, _ = Holding.objects.select_for_update().get_or_create(
        workspace=workspace,
        item=item,
        location=destination_location,
        defaults={"quantity": Decimal("0"), "approximate": source.approximate},
    )
    source.quantity -= amount
    destination.quantity += amount
    destination.approximate = destination.approximate or source.approximate
    if source.quantity == 0:
        source.delete()
    else:
        source.save(update_fields=["quantity", "updated_at"])
    destination.save(update_fields=["quantity", "approximate", "updated_at"])

    summary = {
        "item_key": item_key,
        "from_location_key": from_location_key,
        "to_location_key": to_location_key,
        "quantity": str(amount),
        "unit": item.unit,
    }
    event = InventoryEvent.objects.create(
        workspace=workspace,
        kind=InventoryEvent.Kind.MOVE,
        actor=actor,
        client_actor=provenance.get("client_actor", ""),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        source_kind=provenance.get("source_kind", InventoryEvent.SourceKind.MANUAL),
        source_reference=provenance.get("source_reference", ""),
        observed_at=provenance.get("observed_at"),
        metadata=event_metadata_from_provenance(provenance),
        summary=summary,
        undo_data={
            "before": before_state,
            "after_hash": inventory_state_hash(capture_inventory_state(workspace)),
        },
    )
    return event, False


def _mutation_event(*, workspace, actor, kind, data, request_hash, summary):
    provenance = data.get("provenance", {})
    return InventoryEvent.objects.create(
        workspace=workspace,
        kind=kind,
        actor=actor,
        client_actor=provenance.get("client_actor", ""),
        idempotency_key=data["idempotency_key"],
        request_hash=request_hash,
        source_kind=provenance.get("source_kind", InventoryEvent.SourceKind.MANUAL),
        source_reference=provenance.get("source_reference", ""),
        observed_at=provenance.get("observed_at"),
        metadata=event_metadata_from_provenance(provenance),
        summary=summary,
    )


def _replayed_event(*, workspace, data, request_hash):
    event = InventoryEvent.objects.filter(
        workspace=workspace, idempotency_key=data["idempotency_key"]
    ).first()
    if event and event.request_hash != request_hash:
        raise IdempotencyConflict("Idempotency key was already used with a different payload.")
    return event


@transaction.atomic
def update_inventory_item(*, workspace, actor, data, request_hash):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    if event := _replayed_event(workspace=workspace, data=data, request_hash=request_hash):
        return event, True

    item = Item.objects.select_for_update().filter(workspace=workspace, id=data["item_id"]).first()
    if not item:
        raise BulkUpsertError("Item was not found in this workspace.")

    item_fields = data.get("item", {})
    holding_rows = data.get("holdings", [])
    minimum = item_fields.get("minimum_quantity", item.minimum_quantity)
    target = item_fields.get("target_quantity", item.target_quantity)
    if minimum is not None and target is not None and target < minimum:
        raise BulkUpsertError("Target quantity must reach the minimum quantity.")
    for field, value in item_fields.items():
        setattr(item, field, value)
    if item.tracking_mode == Item.TrackingMode.DISCRETE:
        quantity_overrides = {
            row["id"]: row["quantity"] for row in holding_rows if "quantity" in row
        }
        existing_holdings = Holding.objects.select_for_update().filter(
            workspace=workspace, item=item
        )
        if any(
            quantity_overrides.get(holding.id, holding.quantity)
            != quantity_overrides.get(holding.id, holding.quantity).to_integral_value()
            for holding in existing_holdings
        ):
            raise BulkUpsertError(
                "All holdings must have whole quantities before switching to discrete tracking."
            )
    if item_fields:
        item.full_clean()
        item.save(update_fields=[*item_fields, "updated_at"])

    holdings = {
        holding.id: holding
        for holding in Holding.objects.select_for_update().filter(
            workspace=workspace,
            item=item,
            id__in=[row["id"] for row in holding_rows],
        )
    }
    if len(holdings) != len(holding_rows):
        raise BulkUpsertError("A holding was not found for this item in this workspace.")
    location_ids = {row["location_id"] for row in holding_rows if "location_id" in row}
    locations = {
        location.id: location
        for location in Location.objects.filter(workspace=workspace, id__in=location_ids)
    }
    if len(locations) != len(location_ids):
        raise BulkUpsertError("A destination location was not found in this workspace.")

    for row in holding_rows:
        holding = holdings[row["id"]]
        for field, value in row.items():
            if field == "id":
                continue
            if field == "location_id":
                holding.location = locations[value]
            else:
                setattr(holding, field, value)
        holding.item = item
        holding.full_clean()
        try:
            holding.save()
        except IntegrityError as error:
            raise BulkUpsertError(
                "The item already has a holding at the destination location."
            ) from error

    summary = {
        "item_id": str(item.id),
        "item_key": item.key,
        "item_fields": sorted(item_fields),
        "holdings": [str(row["id"]) for row in holding_rows],
    }
    event = _mutation_event(
        workspace=workspace,
        actor=actor,
        kind=InventoryEvent.Kind.ITEM_UPDATE,
        data=data,
        request_hash=request_hash,
        summary=summary,
    )
    return event, False


@transaction.atomic
def delete_inventory_item(*, workspace, actor, data, request_hash):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    if event := _replayed_event(workspace=workspace, data=data, request_hash=request_hash):
        return event, True

    item = Item.objects.select_for_update().filter(workspace=workspace, id=data["item_id"]).first()
    if not item:
        raise BulkUpsertError("Item was not found in this workspace.")
    summary = {
        "item_id": str(item.id),
        "item_key": item.key,
        "item_name": item.name,
        "deleted_holdings": item.holdings.count(),
    }
    item.delete()
    event = _mutation_event(
        workspace=workspace,
        actor=actor,
        kind=InventoryEvent.Kind.ITEM_DELETE,
        data=data,
        request_hash=request_hash,
        summary=summary,
    )
    return event, False


UNDO_TOKEN_SALT = "inventory.undo.preview"
UNDO_TOKEN_MAX_AGE = 15 * 60


def preview_inventory_undo(*, workspace, event):
    if event.workspace_id != workspace.id:
        raise InventoryUndoError(_("Event was not found in this workspace."))
    if event.kind not in {InventoryEvent.Kind.BULK_UPSERT, InventoryEvent.Kind.MOVE}:
        return {"allowed": False, "reason": _("This event type cannot be undone.")}
    if not event.undo_data.get("before") or not event.undo_data.get("after_hash"):
        return {"allowed": False, "reason": _("This event predates safe undo support.")}
    if workspace.inventory_events.filter(
        kind=InventoryEvent.Kind.UNDO,
        summary__undoes_event_id=str(event.id),
    ).exists():
        return {"allowed": False, "reason": _("This event has already been undone.")}
    latest = workspace.inventory_events.order_by("-created_at", "-id").first()
    if latest != event:
        return {"allowed": False, "reason": _("Only the latest inventory event can be undone.")}

    current_hash = inventory_state_hash(capture_inventory_state(workspace))
    if current_hash != event.undo_data["after_hash"]:
        return {
            "allowed": False,
            "reason": _(
                "The inventory changed after this event, so undo would overwrite newer work."
            ),
        }

    before = event.undo_data["before"]
    token = signing.dumps(
        {"event_id": str(event.id), "state_hash": current_hash},
        salt=UNDO_TOKEN_SALT,
    )
    return {
        "allowed": True,
        "reason": "",
        "token": token,
        "restored": {
            "locations": len(before["locations"]),
            "items": len(before["items"]),
            "holdings": len(before["holdings"]),
            "location_relations": len(before["location_relations"]),
        },
    }


@transaction.atomic
def undo_inventory_event(*, workspace, actor, event_id, preview_token):
    try:
        preview = signing.loads(
            preview_token,
            salt=UNDO_TOKEN_SALT,
            max_age=UNDO_TOKEN_MAX_AGE,
        )
    except signing.BadSignature as error:
        raise InventoryUndoError(_("The undo preview is invalid or expired.")) from error
    if preview.get("event_id") != str(event_id):
        raise InventoryUndoError(_("The undo preview does not match this event."))

    workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
    event = (
        InventoryEvent.objects.select_for_update().filter(workspace=workspace, id=event_id).first()
    )
    if not event:
        raise InventoryUndoError(_("Event was not found in this workspace."))
    current_preview = preview_inventory_undo(workspace=workspace, event=event)
    if not current_preview["allowed"]:
        raise InventoryUndoError(current_preview["reason"])
    if preview.get("state_hash") != event.undo_data["after_hash"]:
        raise InventoryUndoError(_("The inventory changed since the undo preview."))

    restore_inventory_state(workspace, event.undo_data["before"])
    undo_event = InventoryEvent.objects.create(
        workspace=workspace,
        kind=InventoryEvent.Kind.UNDO,
        actor=actor,
        source_kind=InventoryEvent.SourceKind.MANUAL,
        summary={
            "undoes_event_id": str(event.id),
            "original_kind": event.kind,
            "restored": current_preview["restored"],
        },
    )
    return undo_event
