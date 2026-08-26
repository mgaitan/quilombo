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

## Guide a first inventory

When the workspace is empty, or the user says they want to start organizing a physical area, make
the first session feel like a conversation rather than data entry:

1. Ask what physical area they want to organize and use their name for it, such as `Workshop` or
   `Library`.
2. Explain that you will work through one zone at a time. Ask for the first zone only: a drawer,
   shelf, box, or other clearly bounded place. The agent or client may interpret a photo/video;
   Quilombo receives only the resulting facts and a short provenance reference.
3. Ask what is in that zone. Preserve mixed contents as separate items, reuse existing names and
   aliases, and keep unknown quantities approximate instead of inventing counts.
4. Show a compact draft in everyday language: the area, zone, objects, quantities, and any
   uncertainty. Ask for confirmation before writing anything.
5. After confirmation, create or reuse the area and zone locations and write that zone's items in
   one `bulk_upsert_inventory` call. Use a fresh stable idempotency key for the confirmed zone.
6. Report what was saved, then ask whether to continue with the next zone. Do not turn the first
   session into a full questionnaire or require the user to understand workspaces, keys, or JSON.

If the user sends a photo or video, interpret it in the agent or client when vision is available,
then send Quilombo only the resulting facts and provenance. If vision is unavailable, ask the user
or client to provide a structured description instead. Never upload the media to Quilombo. Keep
uncertainty in `approximate` and `notes`, and record provenance such as `Processed a workshop photo
on 2026-08-14`.

## Locate objects

1. Call `find_inventory` with the user's concise wording; include category or location only when known.
   If a tool lookup appears not to expose search, inspect the available inventory tools and retry
   `find_inventory` before telling the user that search is unavailable.
2. Report item name, precise recorded location, quantity, unit, and whether it is approximate.
3. Include useful identification clues when present: description, appearance attributes, the full
   location path, copy-specific notes, and a few recorded items nearby.
4. Distinguish "not recorded" from "not present" when no result matches.
5. Read the result's `search` diagnostics. A partial result can be useful; state which terms
   matched and which did not.
6. If the user cannot find an item at the recorded location, offer the stored clues and nearby
   items, then propose re-checking that location. Do not treat the stale record as physical truth.
7. If the first search misses, try concise synonyms or translations in separate searches. Never
   concatenate unrelated alternatives into one query, and never claim semantic matching came from
   Quilombo.
8. Call `get_inventory_snapshot` when the question depends on spatial relations, a full location,
   or several scattered holdings.

## Re-verify a location

Use `audit_inventory` after physically checking a known location. Include only holdings that were
actually checked: omitted holdings stay unchanged. Mark a fact `confirmed` only when observed and
use `unknown` when the audit could not establish whether it remains accurate. The same call may
correct a known holding's quantity, approximation flag, or notes. Show those corrections before
writing and attach the observation time and source as provenance.

Suggest opportunistic re-verification only when the user is already accessing the exact location
and another holding there is stale or unknown. Ask at most one compact follow-up about facts that
are quick to check. Do not ask about recently verified holdings, widen the audit to other
locations, or turn routine searches into repeated inventory questionnaires. Use the freshness of
`nearby_items` returned by search; request a location-scoped snapshot only when the user agrees to
a broader audit.

## Record observations

For a book with a visible ISBN, call `lookup_book_by_isbn` to obtain a bibliographic draft. Show or
reason over the suggested fields before saving them. The lookup is read-only; include its
`source_url` and retrieval time in the provenance of any later upsert.

1. Convert the observation into stable location, item, holding, and relation records.
2. Search or inspect a snapshot first to reuse existing keys and aliases. Preserve useful user
   vocabulary in aliases; include known translations and spelling variants, but do not invent
   uncertain translations.
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

## Check supplies

Call `get_inventory_status` when the user asks what is missing, low, or needs replenishment. Explain
that this compares workspace-wide recorded quantities with user-configured minimum and target
values; it does not forecast consumption or apply per-location thresholds.

## Handle corrections and failures

- Accept a human correction as a new observation and preserve its provenance. Search first, then
  prefer `update_inventory_item` with the returned stable UUID when the intended record is known.
- Use `delete_inventory_item` only after there is enough evidence that the record is erroneous or a
  duplicate. Show the exact item and affected holdings and obtain confirmation before deletion.
- On insufficient source quantity, unknown keys, or an idempotency conflict, do not guess. Read the
  current state, explain the mismatch, and draft a corrected operation.
- On an ambiguous request such as "move the screws", ask which matching item, source, destination,
  and quantity before writing.
- Never retry a failed write under a different key until its outcome is known; first read the
  affected state to avoid duplicate physical changes.
