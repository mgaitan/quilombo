---
name: manage-quilombo-inventory
description: Find, record, move, and reorganize physical objects with the Quilombo MCP inventory. Use for questions about where an item is, recording workshop/library/storage observations, applying bulk inventory changes, describing nested or relative locations, or proposing and executing a physical reorganization. Also use after a client has interpreted a photo or video; store only the resulting facts and a provenance reference, never the media itself.
---

# Manage Quilombo Inventory

Treat Quilombo as deterministic storage. Perform recognition, semantic interpretation, and
reorganization reasoning yourself; do not imply that the server provides AI or stores media.

Read [references/data-contract.md](references/data-contract.md) before constructing writes or
relative locations.

## Follow the operating rules

- Stay within the workspace selected by the connected bearer token.
- Never invent an item, exact quantity, or precise location. Preserve uncertainty with
  `approximate`, notes, and provenance.
- Search before answering where something is or before creating a possibly duplicate item.
- Treat holding quantities in `bulk_upsert_inventory` as replacement values, not deltas.
- Generate one stable, unique idempotency key per intended mutation. Reuse it only to retry the
  exact same payload. Use a new key after changing any field.
- Present a compact draft and obtain confirmation before calling a mutating tool, unless the user
  has explicitly authorized that exact write in the current request.
- State clearly when a write has completed. Do not describe a proposal as already applied.
- Reply in the user's language and preserve their useful vocabulary as aliases.

## Locate objects

1. Call `find_inventory` with the user's wording; include category or location only when known.
2. Report item name, precise recorded location, quantity, unit, and whether it is approximate.
3. Distinguish "not recorded" from "not present" when no result matches.
4. Try a concise synonym or broader category only when the first deterministic search misses.
   Never claim semantic matching came from Quilombo.
5. Call `get_inventory_snapshot` when the question depends on spatial relations, a full location,
   or several scattered holdings.

## Record observations

1. Convert the observation into stable location, item, holding, and relation records.
2. Search or inspect a snapshot first to reuse existing keys and aliases.
3. Mark uncertain counts as `approximate: true`; keep unstructured uncertainty in `notes`.
4. Build one transactional `bulk_upsert_inventory` call for related changes.
5. Show a draft summarizing created/updated records and current quantities, then confirm and write.

For client-side photo or video analysis, use provenance such as:

```json
{
  "client_actor": "vision-agent/1.0",
  "source_kind": "photo",
  "source_reference": "Changes inferred from workshop photo processed on 2026-08-14"
}
```

Do not upload, embed, or claim to retain the source. Avoid putting secrets or media URLs in the
reference.

## Move and reorganize

For a direct move, inspect the source holding, draft the exact movement, obtain confirmation, then
call `move_inventory`. Use the returned event to confirm completion. Do not simulate a move with
two independent holding writes.

For reorganization:

1. Read a sufficiently broad snapshot.
2. Reason outside Quilombo about grouping, scattered categories, mixed locations, capacity hints,
   and relative layout.
3. Present a short plan with each source, destination, item, and quantity. Explain uncertainty and
   do not assume unrecorded capacity.
4. Obtain confirmation for the plan.
5. Execute each approved movement with its own idempotency key. Stop and report partial completion
   if a movement fails; do not silently recompute physical reality.
6. Read the affected snapshot again and summarize the recorded final state.

Prefer suggestions over mutations when the user asks only for advice.

## Handle corrections and failures

- Accept a human correction as a new observation and preserve its provenance.
- On insufficient source quantity, unknown keys, or an idempotency conflict, do not guess. Read the
  current state, explain the mismatch, and draft a corrected operation.
- On an ambiguous request such as "move the screws", ask which matching item, source, destination,
  and quantity before writing.
- Never retry a failed write under a different key until its outcome is known; first read the
  affected state to avoid duplicate physical changes.
