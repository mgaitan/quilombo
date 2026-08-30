# Import and export

Quilombo can export a workspace as a versioned JSON or CSV document and import that document into
an authorized workspace. PostgreSQL remains canonical storage; these formats are for backup,
transfer, inspection, and deterministic interchange. OKF projection is not part of the current
contract.

## Export

Use `GET /api/workspaces/{slug}/export/?format=json` or `?format=csv`. The response is a UTF-8
attachment. Both formats include stable UUIDs for locations, items, labels, label aliases, label
assertions, holdings, and location relations.

JSON uses this top-level shape:

```json
{
  "format_version": "1.1",
  "workspace": {"id": "...", "name": "Workshop", "slug": "workshop"},
  "exported_at": "2026-08-22T12:00:00Z",
  "locations": [],
  "items": [],
  "labels": [],
  "label_aliases": [],
  "item_labels": [],
  "holdings": [],
  "location_relations": []
}
```

CSV uses one table with a `record_type` column. Label records use `label`, `label_alias`, and
`item_label`; fields that are structured in the domain model, such as aliases, metadata, and
attributes, contain compact JSON values.

## Import

POST to `/api/workspaces/{slug}/import/` with `format`, a unique `idempotency_key`, optional
`provenance`, and exactly one input source:

- `document`: a parsed JSON object;
- `content`: a JSON or CSV string;
- `file`: a UTF-8 JSON or CSV upload.

Set `dry_run: true` to validate the entire document and return created/updated counts without
writing records or an inventory event. A non-dry-run import commits all records and its provenance
in one transaction. Repeating it with the same idempotency key and payload returns the original
summary.

Version 1.1 adds the label vocabulary and assertions. Imports continue to accept version 1.0 JSON
and CSV documents, treating their absent label collections as empty. An assertion's authenticated
`created_by_id` is preserved for backups in the same installation; a portable transfer must set it
to `null` when that user does not exist in the target installation.

Imports preserve supplied UUIDs and merge the document into the target workspace; records omitted
from a document are not deleted. References must resolve within the document. UUIDs from another
workspace, conflicting stable keys, invalid
hierarchies, cross-workspace relationships, negative quantities, and fractional quantities for
discrete items are rejected before commit.
