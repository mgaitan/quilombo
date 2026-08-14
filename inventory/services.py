import hashlib
import json

from django.db import transaction
from django.utils import timezone

from .models import Holding, InventoryEvent, Item, Location, LocationRelation, Workspace


class BulkUpsertError(Exception):
    pass


class IdempotencyConflict(BulkUpsertError):
    pass


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
