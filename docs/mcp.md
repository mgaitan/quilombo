# MCP integration

The hosted Streamable HTTP endpoint is:

```text
https://quilombo.life/mcp
```

OAuth-capable clients can discover the authorization endpoints and ask the user to authorize a
workspace. Clients that do not support OAuth can use a workspace bearer token in the `Authorization`
header.

For token authentication:

```text
Authorization: Bearer qlo_...
```

## Tools

| Tool | Arguments | Access | Purpose |
| --- | --- | --- | --- |
| `find_inventory` | `query`; optional `category`, `location_key`, `include_descendants`, `limit`, `cursor` | read-only | Find ranked holdings and their locations. |
| `get_inventory_snapshot` | optional `location_key`, `category`, `include_descendants`, `limit`, `cursor` | read-only | Read bounded locations, relations, items, and holdings together. |
| `get_inventory_status` | none | read-only | Find recorded quantities below their configured minimum. |
| `lookup_book_by_isbn` | `isbn` | external read | Fetch a bibliographic draft from Open Library; never writes inventory. |
| `audit_inventory` | `location_key`, `location_status`, `idempotency_key`; optional `holdings`, `provenance` | idempotent write | Verify a location and selected holdings, with optional corrections. |
| `bulk_upsert_inventory` | `idempotency_key`; optional `locations`, `items`, `holdings`, `location_relations`, `provenance` | idempotent write | Transactionally create or replace related inventory facts. |
| `move_inventory` | `item_key`, `from_location_key`, `to_location_key`, `quantity`, `idempotency_key`; optional `provenance` | idempotent write | Move a holding between locations. |
| `update_inventory_item` | `item_id`, `idempotency_key`; optional `item`, `holdings`, `provenance` | idempotent write | Correct a known item and its holdings by stable UUID. |
| `delete_inventory_item` | `item_id`, `idempotency_key`; optional `provenance` | idempotent write | Remove a confirmed erroneous or duplicate item by stable UUID. |

`find_inventory` accepts limits from 1 to 500 and defaults to 100. Its results are ordered by
match quality and stable item, location, and holding identifiers. `get_inventory_snapshot` accepts
limits from 1 to 500 and defaults to 100 per collection. For broad snapshots, provide a
`location_key` or `category`.

Mutation tools accept at most 100 records in each collection by default and at most 256 KiB of
serialized input. Configure these limits with `MCP_MAX_MUTATION_COLLECTION_ITEMS` and
`MCP_MAX_MUTATION_PAYLOAD_BYTES`; oversized requests are rejected before validation or database
writes.

The mutation tools write immediately. Drafts, human confirmation, and interpretation of photos or
language belong to the client skill. Always provide a unique idempotency key and provenance for
mutations.

Tool annotations distinguish corrective writes (`audit_inventory`, `move_inventory`, and
`update_inventory_item`) from overwriting or destructive writes (`bulk_upsert_inventory` and
`delete_inventory_item`). Every mutation is marked idempotent: retrying the same payload replays
the original event, while reusing its key with a different payload returns a `conflict` error.

`lookup_book_by_isbn` uses a 5-second timeout and retries transient upstream failures at most twice.
Configure these values with `BOOK_CATALOG_TIMEOUT_SECONDS` and `BOOK_CATALOG_MAX_RETRIES`. Invalid
ISBNs return `invalid_input`; missing records return `not_found`; malformed responses, timeouts,
rate limits, and exhausted retries return an `upstream` error with a client-safe message.

### Record a book from an ISBN

Use the lookup as a read-only draft, then confirm the selected metadata before writing it:

1. Call `lookup_book_by_isbn` with the visible ISBN.
2. Show the user the returned `suggested_item`, including its title, authors, identifiers, edition,
   format, description, page count, subjects, cover, and source attribution.
3. After confirmation, copy the selected `suggested_item` fields into `bulk_upsert_inventory` and
   copy the returned `provenance` object into the same request. Add the confirmed holding and a
   unique idempotency key.

For example, the confirmed mutation can use the lookup result like this:

```json
{
  "idempotency_key": "book-9780140328721-20260828-001",
  "provenance": {
    "source_kind": "other",
    "source_reference": "https://openlibrary.org/books/OL7353617M/Matilda",
    "metadata": {
      "provider": "open_library",
      "isbn": "9780140328721",
      "retrieved_at": "2026-08-28T15:30:00+00:00"
    }
  },
  "items": [
    {
      "key": "matilda-9780140328721",
      "name": "Matilda",
      "description": "A clever girl outwits a cruel headmistress.",
      "category": "books",
      "attributes": {
        "schema": "book",
        "identifiers": {"isbn_13": ["9780140328721"], "isbn": ["9780140328721"]},
        "book": {
          "title": "Matilda",
          "authors": ["Roald Dahl"],
          "publishers": ["Puffin"],
          "publication_date": "1988",
          "edition": "",
          "format": "",
          "description": "A clever girl outwits a cruel headmistress.",
          "page_count": 240,
          "subjects": [],
          "cover_url": "https://covers.openlibrary.org/example.jpg",
          "source_url": "https://openlibrary.org/books/OL7353617M/Matilda",
          "retrieved_at": "2026-08-28T15:30:00+00:00"
        }
      },
      "tracking_mode": "discrete",
      "unit": "copy"
    }
  ],
  "holdings": [
    {"item_key": "matilda-9780140328721", "location_key": "bookshelf", "quantity": "1"}
  ]
}
```

The lookup alone never creates an item, holding, or inventory event. The client owns presentation and
confirmation; the confirmed bulk upsert owns persistence and remains transactional.

The collection reads `find_inventory` and `get_inventory_snapshot` return `truncated` and an opaque
`next_cursor` when another page is available. Snapshot responses also return
`truncated_collections` for each collection. Pass `next_cursor` back to the same tool without
changing its filters or limit. Cursors are workspace-scoped, signed, and expire after 15 minutes;
invalid or expired cursors return a tool error.

## Error responses

Tool failures keep `is_error: true` and include the same payload in `structured_content` and the
text content:

```json
{"code":"not_found","message":"The requested item was not found in this workspace."}
```

Clients should branch on `code`, not on the human-readable `message`. The stable codes are:

- `invalid_input`: arguments or requested changes fail validation.
- `authentication`: a bearer token is missing, invalid, or revoked.
- `authorization`: the token is valid but cannot perform the requested operation.
- `not_found`: a requested record is not available in the authorized workspace or catalog.
- `conflict`: the current state conflicts with the request, including idempotency reuse.
- `upstream`: an external catalog service could not complete the request.

Validation and workspace-isolation errors use generic references and do not disclose records from
another workspace.

The server publishes one read-only resource:

| URI | Purpose |
| --- | --- |
| `quilombo://guides/inventory-policy` | Client guidance for searching, freshness, verification, and safe writes. |

Search and snapshot results expose stable item, holding, and location UUIDs for repair operations.
They also expose verification status, last observation, observer, and whether a confirmed fact is
current or stale. The default freshness window is 90 days and can be configured with
`INVENTORY_FRESHNESS_DAYS`.
Prefer `update_inventory_item` when the intended item is known. Delete only after the client has
enough evidence that the record is erroneous or duplicated and has applied its confirmation policy.

## ChatGPT and Claude

Use the web app's `/connect/` guide for current client-specific setup steps. Keep the MCP URL stable
when deploying new server versions; refresh the client's tool list if it caches action metadata.
The curated workflow is available as [`manage-quilombo-inventory`](https://github.com/mgaitan/quilombo/blob/main/skills/manage-quilombo-inventory/SKILL.md)
in the repository.
