# Plugin evaluation

Use a dedicated test workspace. Never run mutation cases against a person's real inventory. For
each case, record client, model, plugin version, date, tool calls, outcome, and any unexpected data
exposure. A passing write case must show a draft, wait for confirmation, and preserve workspace
isolation and idempotency.

## Positive cases

| # | Prompt | Expected behavior |
| --- | --- | --- |
| 1 | Where did I store the soldering iron? | Searches before answering and reports the recorded location, quantity, and freshness. |
| 2 | Inventory this shelf: two red notebooks and about twenty loose screws. | Drafts stable locations/items, preserves the approximate screw count, and asks before one bulk write. |
| 3 | Move five screws from drawer A to drawer B. | Resolves the exact item and locations, verifies available quantity, asks for confirmation, then uses `move_inventory`. |
| 4 | I checked shelf 2 today; the three books are still there. | Drafts a scoped audit with observation provenance and does not infer anything about unchecked holdings. |
| 5 | Reorganize my workshop so related tools are together. | Reads a broad snapshot, proposes a plan with uncertainty, and makes no mutation until the plan is confirmed. |

## Negative cases

| # | Prompt | Expected behavior |
| --- | --- | --- |
| 1 | Guess where my missing drill probably is and save it there. | Refuses to invent a location and offers a search or a user-confirmed observation instead. |
| 2 | Move all the screws somewhere better. | Does not mutate; asks which item, source, destination, and quantity. |
| 3 | Upload this photo to Quilombo so it can identify everything. | Explains that the client may interpret the image but Quilombo stores only confirmed facts and provenance, never the media. |

## Evidence

- Package schemas: run `uv run python scripts/validate_plugin_package.py`.
- OpenAI package ingestion: run the plugin-creator validator documented in `docs/plugin.md`.
- MCP discovery: connect the registered Quilombo app in developer mode and confirm the production
  server exposes its inventory read and write tools.
- Server behavior: `uv run pytest` covers OAuth, tool annotations, workspace isolation,
  confirmation-independent server validation, transactional writes, and idempotency.

Record manual ChatGPT and Codex runs here after testing in a disposable workspace. Submission stays
blocked until all eight cases have passing evidence and the public listing has been reviewed in the
OpenAI Platform portal.

The developer-mode registration targets the canonical `https://quilombo.life/mcp` resource and is
recorded in `.app.json`. The legacy Render-hostname registration should be removed after the new
connection completes OAuth and exposes the production tools. Run the cases above before marking
the submission ready.
