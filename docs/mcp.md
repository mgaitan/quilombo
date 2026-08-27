# MCP integration

The hosted Streamable HTTP endpoint is:

```text
https://quilombo.life/mcp
```

OAuth-capable clients can discover the authorization endpoints and ask the user to authorize a
workspace. Clients that do not support OAuth can use a workspace bearer token in the `Authorization`
header.

For token authentication:

```http
Authorization: Bearer qlo_...
```

## Tools

| Tool | Arguments | Access | Purpose |
| --- | --- | --- | --- |
| `find_inventory` | `query`; optional `category`, `location_key`, `include_descendants`, `limit`, `cursor` | read-only | Find ranked holdings and their locations. |
| `get_inventory_snapshot` | optional `location_key`, `category`, `include_descendants`, `limit`, `cursor` | read-only | Read bounded locations, relations, items, and holdings together. |
| `get_inventory_status` | none | read-only | Find recorded quantities below their configured minimum. |
| `lookup_book_by_isbn` | `isbn` | external read | Fetch a bibliographic draft from Open Library. |
| `audit_inventory` | `location_key`, `location_status`, `idempotency_key`; optional `holdings`, `provenance` | idempotent write | Verify a location and selected holdings, with optional corrections. |
| `bulk_upsert_inventory` | `idempotency_key`; optional `locations`, `items`, `holdings`, `location_relations`, `provenance` | idempotent write | Transactionally create or replace related inventory facts. |
| `move_inventory` | `item_key`, `from_location_key`, `to_location_key`, `quantity`, `idempotency_key`; optional `provenance` | idempotent write | Move a holding between locations. |
| `update_inventory_item` | `item_id`, `idempotency_key`; optional `item`, `holdings`, `provenance` | idempotent write | Correct a known item and its holdings by stable UUID. |
| `delete_inventory_item` | `item_id`, `idempotency_key`; optional `provenance` | idempotent write | Remove a confirmed erroneous or duplicate item by stable UUID. |

`find_inventory` accepts limits from 1 to 500 and defaults to 100. Its results are ordered by
match quality and stable item, location, and holding identifiers. `get_inventory_snapshot` accepts
limits from 1 to 500 and defaults to 100 per collection. For broad snapshots, provide a
`location_key` or `category`.

The mutation tools write immediately. Drafts, human confirmation, and interpretation of photos or
language belong to the client skill. Always provide a unique idempotency key and provenance for
mutations.

The collection reads `find_inventory` and `get_inventory_snapshot` return `truncated` and an opaque
`next_cursor` when another page is available. Snapshot responses also return
`truncated_collections` for each collection. Pass `next_cursor` back to the same tool without
changing its filters or limit. Cursors are workspace-scoped, signed, and expire after 15 minutes;
invalid or expired cursors return a tool error.

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
