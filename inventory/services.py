import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.utils import timezone

from .models import Holding, InventoryEvent, Item, Location, LocationRelation, Workspace


class BulkUpsertError(Exception):
    pass


class IdempotencyConflict(BulkUpsertError):
    pass


def location_scope_ids(*, workspace, location_key, include_descendants=True):
    rows = list(workspace.locations.values_list("id", "parent_id", "key"))
    matching_ids = {location_id for location_id, _, key in rows if key == location_key}
    if not include_descendants or not matching_ids:
        return matching_ids

    children_by_parent = {}
    for location_id, parent_id, _ in rows:
        children_by_parent.setdefault(parent_id, set()).add(location_id)

    pending = list(matching_ids)
    while pending:
        children = children_by_parent.get(pending.pop(), set()) - matching_ids
        matching_ids.update(children)
        pending.extend(children)
    return matching_ids


def search_holdings(
    *, workspace, query, category="", location="", include_descendants=True, limit=100
):
    holdings = (
        Holding.objects.filter(workspace=workspace)
        .select_related("item", "location")
        .annotate(
            item_aliases_text=Cast("item__aliases", TextField()),
            item_attributes_text=Cast("item__attributes", TextField()),
            location_aliases_text=Cast("location__aliases", TextField()),
        )
    )
    for term in query.strip().split():
        holdings = holdings.filter(
            Q(item__key__icontains=term)
            | Q(item__name__icontains=term)
            | Q(item__description__icontains=term)
            | Q(item__category__icontains=term)
            | Q(item_aliases_text__icontains=term)
            | Q(item_attributes_text__icontains=term)
            | Q(location__key__icontains=term)
            | Q(location__name__icontains=term)
            | Q(location_aliases_text__icontains=term)
        )
    if category:
        holdings = holdings.filter(item__category__iexact=category)
    if location:
        holdings = holdings.filter(
            location_id__in=location_scope_ids(
                workspace=workspace,
                location_key=location,
                include_descendants=include_descendants,
            )
        )
    return list(holdings[:limit])


def hash_request(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


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
            aliases=row.get("aliases", []),
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
            changed_parents.append(location)
    if changed_parents:
        Location.objects.bulk_update(changed_parents, ["parent"])

    items = [
        Item(
            workspace=workspace,
            key=row["key"],
            name=row["name"],
            description=row.get("description", ""),
            category=row.get("category", ""),
            aliases=row.get("aliases", []),
            attributes=row.get("attributes", {}),
            tracking_mode=row.get("tracking_mode", Item.TrackingMode.BULK),
            unit=row.get("unit", "unit"),
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
            update_fields=["quantity", "approximate", "notes", "updated_at"],
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
        metadata=provenance.get("metadata", {}),
        summary=summary,
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
        metadata=provenance.get("metadata", {}),
        summary=summary,
    )
    return event, False
