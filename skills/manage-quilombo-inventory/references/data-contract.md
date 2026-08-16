# Quilombo Data Contract

## Concepts

- **Workspace:** Tenant boundary selected by the API token.
- **Location:** A stable place key. Nest locations with `parent_key` for containment.
- **Location relation:** A directional spatial fact between locations: `left_of`, `right_of`,
  `above`, `below`, or `near`.
- **Item:** A reusable description of an object type or identified object.
- **Holding:** The current quantity of one item at one location.
- **Inventory event:** Immutable audit metadata for a mutation, including client-reported
  provenance. It does not retain a photo or video.

Use concise, stable ASCII keys such as `drawer-1-cell-b4` and `fix-screw-35mm`. Put human wording,
accents, synonyms, brands, authors, and common misspellings in names or aliases.

Use `Item.description` for a short identification sentence. Put reusable structured traits in
`Item.attributes`; for visual identification prefer an `appearance` object with explicit keys such
as `spine_color`, `lettering_color`, `width`, `material`, or `distinctive_marks`. Put facts about a
particular physical copy, such as a torn cover or handwritten dedication, in `Holding.notes`.

Example for a book:

```json
{
  "description": "Edición ancha con lomo rojo y letras blancas",
  "attributes": {
    "schema": "book",
    "appearance": {
      "spine_color": "red",
      "lettering_color": "white",
      "width": "wide"
    }
  }
}
```

Search results include the full containment path and a bounded list of other recorded items at the
same location. These are finding clues, not proof that the physical arrangement is still current.

Set `minimum_quantity` on an item to include it in missing/low-stock reports. Optionally set
`target_quantity` to the desired quantity after replenishment; it must be at least the minimum.
These thresholds use the item's `unit`. Without a target, the minimum is also the replenishment
target.

## Bulk upsert

`bulk_upsert_inventory` accepts optional lists and commits all of them in one transaction:

```json
{
  "idempotency_key": "observation-20260814-workshop-001",
  "provenance": {
    "client_actor": "inventory-agent/1.0",
    "source_kind": "agent",
    "source_reference": "Confirmed by the user during voice session on 2026-08-14",
    "observed_at": "2026-08-14T15:30:00Z",
    "metadata": {}
  },
  "locations": [
    {
      "key": "drawer-1-cell-b4",
      "name": "Drawer 1, compartment B4",
      "parent_key": "drawer-1",
      "kind": "compartment",
      "aliases": ["cajon 1 B4"],
      "metadata": {}
    }
  ],
  "items": [
    {
      "key": "fix-screw-35mm",
      "name": "FIX 35 mm screw",
      "description": "",
      "category": "wood screws",
      "aliases": ["tornillo para madera", "tornillo fix de 35mm"],
      "attributes": {"length_mm": 35, "brand": "FIX"},
      "tracking_mode": "bulk",
      "unit": "piece"
    }
  ],
  "holdings": [
    {
      "item_key": "fix-screw-35mm",
      "location_key": "drawer-1-cell-b4",
      "quantity": "80",
      "approximate": true,
      "notes": "Visual estimate confirmed by user"
    }
  ],
  "location_relations": [
    {
      "subject_key": "middle-left",
      "relation": "left_of",
      "object_key": "middle-center"
    }
  ]
}
```

Every supplied holding quantity is the new current value. Omitted records remain unchanged.
Upserting a parent requires either an existing parent or that parent in the same request. Relative
relations supplement containment; they do not replace `parent_key`.

Use `tracking_mode: "discrete"` for books, tools, and other whole-count objects. Discrete holdings
require whole quantities. Use `bulk` for divisible or estimated stock.

## Provenance

Allowed `source_kind` values are `manual`, `photo`, `video`, `import`, `agent`, and `other`.
`source_reference` is a short client assertion about origin, not a media object. `observed_at` is
when the physical state was observed; omit it when unknown rather than inventing it.

## Move

`move_inventory` atomically subtracts and adds a positive quantity:

```json
{
  "item_key": "fix-screw-35mm",
  "from_location_key": "drawer-3-cell-a1",
  "to_location_key": "drawer-1-cell-b4",
  "quantity": "20",
  "idempotency_key": "move-20260814-fix-001",
  "provenance": {
    "client_actor": "inventory-agent/1.0",
    "source_kind": "manual",
    "source_reference": "Move confirmed by user on 2026-08-14"
  }
}
```

The source and destination must already exist. The source must contain enough quantity. A zero
source holding is removed. A repeated identical request with the same key returns the prior event
without moving twice.
