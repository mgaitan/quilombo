import hashlib
import json
from decimal import Decimal

from .models import Holding, Item, Location, LocationRelation


def capture_inventory_state(workspace):
    return {
        "locations": [
            {
                "id": str(location.id),
                "key": location.key,
                "name": location.name,
                "description": location.description,
                "kind": location.kind,
                "parent_id": str(location.parent_id) if location.parent_id else None,
                "aliases": location.aliases,
                "metadata": location.metadata,
            }
            for location in workspace.locations.order_by("key", "id")
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
                "minimum_quantity": (
                    str(item.minimum_quantity) if item.minimum_quantity is not None else None
                ),
                "target_quantity": (
                    str(item.target_quantity) if item.target_quantity is not None else None
                ),
            }
            for item in workspace.items.order_by("key", "id")
        ],
        "holdings": [
            {
                "id": str(holding.id),
                "item_id": str(holding.item_id),
                "location_id": str(holding.location_id),
                "quantity": str(holding.quantity),
                "approximate": holding.approximate,
                "notes": holding.notes,
            }
            for holding in workspace.holdings.order_by("item_id", "location_id", "id")
        ],
        "location_relations": [
            {
                "id": str(relation.id),
                "subject_id": str(relation.subject_id),
                "relation": relation.relation,
                "object_id": str(relation.object_id),
            }
            for relation in workspace.location_relations.order_by(
                "subject_id", "relation", "object_id", "id"
            )
        ],
    }


def inventory_state_hash(state):
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def restore_inventory_state(workspace, state):
    workspace.location_relations.all().delete()
    workspace.holdings.all().delete()
    workspace.items.all().delete()
    workspace.locations.update(parent=None)
    workspace.locations.all().delete()

    locations = {
        row["id"]: Location(
            id=row["id"],
            workspace=workspace,
            key=row["key"],
            name=row["name"],
            description=row["description"],
            kind=row["kind"],
            aliases=row["aliases"],
            metadata=row["metadata"],
        )
        for row in state["locations"]
    }
    Location.objects.bulk_create(locations.values())
    for row in state["locations"]:
        if row["parent_id"]:
            locations[row["id"]].parent_id = row["parent_id"]
    Location.objects.bulk_update(locations.values(), ["parent"])

    items = {
        row["id"]: Item(
            id=row["id"],
            workspace=workspace,
            key=row["key"],
            name=row["name"],
            description=row["description"],
            category=row["category"],
            aliases=row["aliases"],
            attributes=row["attributes"],
            tracking_mode=row["tracking_mode"],
            unit=row["unit"],
            minimum_quantity=(
                Decimal(row["minimum_quantity"]) if row["minimum_quantity"] is not None else None
            ),
            target_quantity=(
                Decimal(row["target_quantity"]) if row["target_quantity"] is not None else None
            ),
        )
        for row in state["items"]
    }
    Item.objects.bulk_create(items.values())

    Holding.objects.bulk_create(
        [
            Holding(
                id=row["id"],
                workspace=workspace,
                item_id=row["item_id"],
                location_id=row["location_id"],
                quantity=Decimal(row["quantity"]),
                approximate=row["approximate"],
                notes=row["notes"],
            )
            for row in state["holdings"]
        ]
    )
    LocationRelation.objects.bulk_create(
        [
            LocationRelation(
                id=row["id"],
                workspace=workspace,
                subject_id=row["subject_id"],
                relation=row["relation"],
                object_id=row["object_id"],
            )
            for row in state["location_relations"]
        ]
    )
