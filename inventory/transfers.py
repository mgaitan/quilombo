import csv
import io
import json

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Holding, InventoryEvent, Item, Location, LocationRelation, Workspace
from .serializers import InventoryDocumentSerializer

FORMAT_VERSION = "1.0"
CSV_FIELDS = [
    "record_type",
    "id",
    "key",
    "name",
    "description",
    "kind",
    "parent_id",
    "aliases",
    "metadata",
    "category",
    "attributes",
    "tracking_mode",
    "unit",
    "minimum_quantity",
    "target_quantity",
    "item_id",
    "location_id",
    "quantity",
    "approximate",
    "notes",
    "subject_id",
    "relation",
    "object_id",
]


class InventoryTransferError(Exception):
    pass


def export_inventory_document(workspace):
    return {
        "format_version": FORMAT_VERSION,
        "workspace": {
            "id": str(workspace.id),
            "name": workspace.name,
            "slug": workspace.slug,
        },
        "exported_at": timezone.now().isoformat(),
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


def export_inventory_csv(document):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for collection, record_type in (
        ("locations", "location"),
        ("items", "item"),
        ("holdings", "holding"),
        ("location_relations", "location_relation"),
    ):
        for source in document[collection]:
            row = {field: "" for field in CSV_FIELDS}
            row.update(source)
            row["record_type"] = record_type
            for field in ("aliases", "metadata", "attributes"):
                if field in source:
                    row[field] = json.dumps(
                        source[field], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
            if "approximate" in source:
                row["approximate"] = "true" if source["approximate"] else "false"
            writer.writerow(row)
    return output.getvalue()


def parse_inventory_document(*, format_name, document=None, content=None):
    if document is None:
        if content is None:
            raise InventoryTransferError("Import content is required.")
        try:
            document = json.loads(content) if format_name == "json" else _parse_csv(content)
        except (csv.Error, json.JSONDecodeError, UnicodeError, ValueError) as error:
            raise InventoryTransferError(
                f"Invalid {format_name.upper()} document: {error}"
            ) from error

    serializer = InventoryDocumentSerializer(data=document)
    if not serializer.is_valid():
        raise InventoryTransferError(f"Invalid inventory document: {serializer.errors}")
    return serializer.validated_data


def _parse_csv(content):
    document = {
        "format_version": FORMAT_VERSION,
        "locations": [],
        "items": [],
        "holdings": [],
        "location_relations": [],
    }
    readers = {
        "location": ("locations", _csv_location),
        "item": ("items", _csv_item),
        "holding": ("holdings", _csv_holding),
        "location_relation": ("location_relations", _csv_relation),
    }
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames != CSV_FIELDS:
        raise ValueError("CSV header does not match format version 1.0.")
    for line_number, row in enumerate(reader, start=2):
        target = readers.get(row.get("record_type", ""))
        if not target:
            raise ValueError(f"Unknown record_type on line {line_number}.")
        collection, converter = target
        try:
            document[collection].append(converter(row))
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"Invalid value on line {line_number}: {error}") from error
    return document


def _json_cell(row, field, default):
    value = row.get(field, "")
    return json.loads(value) if value else default


def _csv_location(row):
    return {
        "id": row["id"],
        "key": row["key"],
        "name": row["name"],
        "description": row.get("description", ""),
        "kind": row.get("kind", ""),
        "parent_id": row.get("parent_id") or None,
        "aliases": _json_cell(row, "aliases", []),
        "metadata": _json_cell(row, "metadata", {}),
    }


def _csv_item(row):
    return {
        "id": row["id"],
        "key": row["key"],
        "name": row["name"],
        "description": row.get("description", ""),
        "category": row.get("category", ""),
        "aliases": _json_cell(row, "aliases", []),
        "attributes": _json_cell(row, "attributes", {}),
        "tracking_mode": row["tracking_mode"],
        "unit": row["unit"],
        "minimum_quantity": row.get("minimum_quantity") or None,
        "target_quantity": row.get("target_quantity") or None,
    }


def _csv_holding(row):
    approximate = row.get("approximate", "").casefold()
    if approximate not in {"true", "false"}:
        raise ValueError("approximate must be true or false.")
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "location_id": row["location_id"],
        "quantity": row["quantity"],
        "approximate": approximate == "true",
        "notes": row.get("notes", ""),
    }


def _csv_relation(row):
    return {
        "id": row["id"],
        "subject_id": row["subject_id"],
        "relation": row["relation"],
        "object_id": row["object_id"],
    }


def _validate_target(workspace, document):
    model_rows = (
        (Location, document["locations"]),
        (Item, document["items"]),
        (Holding, document["holdings"]),
        (LocationRelation, document["location_relations"]),
    )
    for model, rows in model_rows:
        ids = [row["id"] for row in rows]
        if model.objects.filter(id__in=ids).exclude(workspace=workspace).exists():
            raise InventoryTransferError(
                f"A {model._meta.verbose_name} ID belongs to another workspace."
            )

    for model, rows in ((Location, document["locations"]), (Item, document["items"])):
        key_to_id = dict(model.objects.filter(workspace=workspace).values_list("key", "id"))
        if any(row["key"] in key_to_id and key_to_id[row["key"]] != row["id"] for row in rows):
            raise InventoryTransferError(
                f"A {model._meta.verbose_name} key is already assigned to another ID."
            )

    holding_by_pair = {
        (item_id, location_id): holding_id
        for holding_id, item_id, location_id in Holding.objects.filter(
            workspace=workspace
        ).values_list("id", "item_id", "location_id")
    }
    if any(
        (row["item_id"], row["location_id"]) in holding_by_pair
        and holding_by_pair[(row["item_id"], row["location_id"])] != row["id"]
        for row in document["holdings"]
    ):
        raise InventoryTransferError("A holding already exists under another stable ID.")

    relation_by_key = {
        (subject_id, relation, object_id): relation_id
        for relation_id, subject_id, relation, object_id in LocationRelation.objects.filter(
            workspace=workspace
        ).values_list("id", "subject_id", "relation", "object_id")
    }
    if any(
        (row["subject_id"], row["relation"], row["object_id"]) in relation_by_key
        and relation_by_key[(row["subject_id"], row["relation"], row["object_id"])] != row["id"]
        for row in document["location_relations"]
    ):
        raise InventoryTransferError("A location relation exists under another stable ID.")


def _import_summary(workspace, document):
    summary = {}
    for name, model in (
        ("locations", Location),
        ("items", Item),
        ("holdings", Holding),
        ("location_relations", LocationRelation),
    ):
        ids = [row["id"] for row in document[name]]
        updated = model.objects.filter(workspace=workspace, id__in=ids).count()
        summary[name] = {"created": len(ids) - updated, "updated": updated}
    return summary


@transaction.atomic
def import_inventory_document(
    *, workspace, actor, document, dry_run, idempotency_key, provenance, request_hash
):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    _validate_target(workspace, document)
    summary = _import_summary(workspace, document)
    if dry_run:
        return summary, None, False

    existing_event = InventoryEvent.objects.filter(
        workspace=workspace, idempotency_key=idempotency_key
    ).first()
    if existing_event:
        if existing_event.request_hash != request_hash:
            raise InventoryTransferError(
                "Idempotency key was already used with a different payload."
            )
        return existing_event.summary, existing_event, True

    try:
        locations = {}
        for row in document["locations"]:
            values = {key: value for key, value in row.items() if key not in {"id", "parent_id"}}
            location, _ = Location.objects.update_or_create(
                id=row["id"], defaults={"workspace": workspace, "parent": None, **values}
            )
            locations[location.id] = location
        for row in document["locations"]:
            location = locations[row["id"]]
            location.parent = locations.get(row["parent_id"])
            location.save(update_fields=["parent", "updated_at"])

        items = {}
        for row in document["items"]:
            values = {key: value for key, value in row.items() if key != "id"}
            item, _ = Item.objects.update_or_create(
                id=row["id"], defaults={"workspace": workspace, **values}
            )
            items[item.id] = item

        for row in document["holdings"]:
            Holding.objects.update_or_create(
                id=row["id"],
                defaults={
                    "workspace": workspace,
                    "item": items[row["item_id"]],
                    "location": locations[row["location_id"]],
                    "quantity": row["quantity"],
                    "approximate": row["approximate"],
                    "notes": row["notes"],
                },
            )

        for row in document["location_relations"]:
            LocationRelation.objects.update_or_create(
                id=row["id"],
                defaults={
                    "workspace": workspace,
                    "subject": locations[row["subject_id"]],
                    "relation": row["relation"],
                    "object": locations[row["object_id"]],
                },
            )
    except IntegrityError as error:
        raise InventoryTransferError(f"Import violates an inventory constraint: {error}") from error

    event = InventoryEvent.objects.create(
        workspace=workspace,
        kind=InventoryEvent.Kind.BULK_UPSERT,
        actor=actor,
        client_actor=provenance.get("client_actor", ""),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        source_kind=provenance.get("source_kind", InventoryEvent.SourceKind.IMPORT),
        source_reference=provenance.get("source_reference", ""),
        observed_at=provenance.get("observed_at"),
        metadata={
            **provenance.get("metadata", {}),
            "transfer_format_version": FORMAT_VERSION,
        },
        summary=summary,
    )
    return summary, event, False
