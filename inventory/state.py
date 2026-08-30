import hashlib
import json
from decimal import Decimal

from django.utils.dateparse import parse_datetime

from .models import Holding, Item, ItemLabel, Label, LabelAlias, Location, LocationRelation


def capture_inventory_state(workspace):
    labels = list(workspace.labels.order_by("name", "id"))
    label_aliases = list(workspace.label_aliases.order_by("value", "id")) if labels else []
    item_labels = list(workspace.item_labels.order_by("created_at", "id")) if labels else []
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
                "verification_status": location.verification_status,
                "last_observed_at": (
                    location.last_observed_at.isoformat() if location.last_observed_at else None
                ),
                "last_observed_by_id": (
                    str(location.last_observed_by_id) if location.last_observed_by_id else None
                ),
                "created_at": location.created_at.isoformat(),
                "updated_at": location.updated_at.isoformat(),
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
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in workspace.items.order_by("key", "id")
        ],
        "labels": [
            {
                "id": str(label.id),
                "name": label.name,
                "normalized_key": label.normalized_key,
                "search_key": label.search_key,
                "created_at": label.created_at.isoformat(),
                "updated_at": label.updated_at.isoformat(),
            }
            for label in labels
        ],
        "label_aliases": [
            {
                "id": str(alias.id),
                "label_id": str(alias.label_id),
                "value": alias.value,
                "normalized_key": alias.normalized_key,
                "search_key": alias.search_key,
                "created_at": alias.created_at.isoformat(),
            }
            for alias in label_aliases
        ],
        "item_labels": [
            {
                "id": str(assertion.id),
                "item_id": str(assertion.item_id),
                "label_id": str(assertion.label_id),
                "original_value": assertion.original_value,
                "source": assertion.source,
                "confidence": (
                    str(assertion.confidence) if assertion.confidence is not None else None
                ),
                "source_reference": assertion.source_reference,
                "metadata": assertion.metadata,
                "created_by_id": assertion.created_by_id,
                "created_at": assertion.created_at.isoformat(),
            }
            for assertion in item_labels
        ],
        "holdings": [
            {
                "id": str(holding.id),
                "item_id": str(holding.item_id),
                "location_id": str(holding.location_id),
                "quantity": str(holding.quantity),
                "approximate": holding.approximate,
                "notes": holding.notes,
                "verification_status": holding.verification_status,
                "last_observed_at": (
                    holding.last_observed_at.isoformat() if holding.last_observed_at else None
                ),
                "last_observed_by_id": (
                    str(holding.last_observed_by_id) if holding.last_observed_by_id else None
                ),
                "updated_at": holding.updated_at.isoformat(),
            }
            for holding in workspace.holdings.order_by("item_id", "location_id", "id")
        ],
        "location_relations": [
            {
                "id": str(relation.id),
                "subject_id": str(relation.subject_id),
                "relation": relation.relation,
                "object_id": str(relation.object_id),
                "created_at": relation.created_at.isoformat(),
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
    workspace.item_labels.all().delete()
    workspace.label_aliases.all().delete()
    workspace.labels.all().delete()
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
            verification_status=row["verification_status"],
            last_observed_at=(
                parse_datetime(row["last_observed_at"]) if row["last_observed_at"] else None
            ),
            last_observed_by_id=row["last_observed_by_id"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )
        for row in state["locations"]
    }
    Location.objects.bulk_create(locations.values())
    for row in state["locations"]:
        locations[row["id"]].created_at = parse_datetime(row["created_at"])
        locations[row["id"]].updated_at = parse_datetime(row["updated_at"])
        if row["parent_id"]:
            locations[row["id"]].parent_id = row["parent_id"]
    Location.objects.bulk_update(locations.values(), ["parent", "created_at", "updated_at"])

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
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )
        for row in state["items"]
    }
    Item.objects.bulk_create(items.values())
    for row in state["items"]:
        items[row["id"]].created_at = parse_datetime(row["created_at"])
        items[row["id"]].updated_at = parse_datetime(row["updated_at"])
    Item.objects.bulk_update(items.values(), ["created_at", "updated_at"])

    labels = {
        row["id"]: Label(
            id=row["id"],
            workspace=workspace,
            name=row["name"],
            normalized_key=row["normalized_key"],
            search_key=row["search_key"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )
        for row in state.get("labels", [])
    }
    Label.objects.bulk_create(labels.values())
    for row in state.get("labels", []):
        labels[row["id"]].created_at = parse_datetime(row["created_at"])
        labels[row["id"]].updated_at = parse_datetime(row["updated_at"])
    Label.objects.bulk_update(labels.values(), ["created_at", "updated_at"])

    label_aliases = [
        LabelAlias(
            id=row["id"],
            workspace=workspace,
            label_id=row["label_id"],
            value=row["value"],
            normalized_key=row["normalized_key"],
            search_key=row["search_key"],
            created_at=parse_datetime(row["created_at"]),
        )
        for row in state.get("label_aliases", [])
    ]
    LabelAlias.objects.bulk_create(label_aliases)
    for alias, row in zip(label_aliases, state.get("label_aliases", []), strict=True):
        alias.created_at = parse_datetime(row["created_at"])
    LabelAlias.objects.bulk_update(label_aliases, ["created_at"])

    item_labels = [
        ItemLabel(
            id=row["id"],
            workspace=workspace,
            item_id=row["item_id"],
            label_id=row["label_id"],
            original_value=row["original_value"],
            source=row["source"],
            confidence=Decimal(row["confidence"]) if row["confidence"] is not None else None,
            source_reference=row["source_reference"],
            metadata=row["metadata"],
            created_by_id=row["created_by_id"],
            created_at=parse_datetime(row["created_at"]),
        )
        for row in state.get("item_labels", [])
    ]
    ItemLabel.objects.bulk_create(item_labels)
    for assertion, row in zip(item_labels, state.get("item_labels", []), strict=True):
        assertion.created_at = parse_datetime(row["created_at"])
    ItemLabel.objects.bulk_update(item_labels, ["created_at"])

    holdings = [
        Holding(
            id=row["id"],
            workspace=workspace,
            item_id=row["item_id"],
            location_id=row["location_id"],
            quantity=Decimal(row["quantity"]),
            approximate=row["approximate"],
            notes=row["notes"],
            verification_status=row["verification_status"],
            last_observed_at=(
                parse_datetime(row["last_observed_at"]) if row["last_observed_at"] else None
            ),
            last_observed_by_id=row["last_observed_by_id"],
            updated_at=parse_datetime(row["updated_at"]),
        )
        for row in state["holdings"]
    ]
    Holding.objects.bulk_create(holdings)
    for holding, row in zip(holdings, state["holdings"], strict=True):
        holding.updated_at = parse_datetime(row["updated_at"])
    Holding.objects.bulk_update(holdings, ["updated_at"])
    relations = [
        LocationRelation(
            id=row["id"],
            workspace=workspace,
            subject_id=row["subject_id"],
            relation=row["relation"],
            object_id=row["object_id"],
            created_at=parse_datetime(row["created_at"]),
        )
        for row in state["location_relations"]
    ]
    LocationRelation.objects.bulk_create(relations)
    for relation, row in zip(relations, state["location_relations"], strict=True):
        relation.created_at = parse_datetime(row["created_at"])
    LocationRelation.objects.bulk_update(relations, ["created_at"])
