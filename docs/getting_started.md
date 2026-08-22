# Getting started

This guide gets a local Quilombo instance running and records the first inventory facts.

## Run locally

Quilombo requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

Open `http://127.0.0.1:8000/` and create an account. New accounts receive a private `Home`
workspace automatically.

## Create a workspace token

From an authenticated browser session, issue a token through:

```text
POST /api/workspaces/{workspace-slug}/tokens/
```

The raw `qlo_...` token is displayed once. Store it securely and send it as a bearer token:

```bash
curl \
  -H 'Authorization: Bearer qlo_...' \
  https://quilombo-v1-mgaitan.onrender.com/api/workspaces/home/
```

## Record inventory

Use `POST /api/workspaces/{workspace-slug}/bulk-upsert/` for related changes. Include a unique
`idempotency_key`; the operation is transactional and quantities replace the current recorded
values.

```json
{
  "idempotency_key": "workshop-2026-08-15-001",
  "provenance": {
    "source_kind": "agent",
    "source_reference": "Processed from a workshop photo on 2026-08-15"
  },
  "locations": [
    {"key": "drawer-1", "name": "Drawer 1", "kind": "drawer"}
  ],
  "items": [
    {
      "key": "aaa-batteries",
      "name": "AAA batteries",
      "category": "battery",
      "aliases": ["AAA cells", "AAA battery"],
      "attributes": {"size": "AAA"},
      "unit": "lot"
    }
  ],
  "holdings": [
    {
      "item_key": "aaa-batteries",
      "location_key": "drawer-1",
      "quantity": "1",
      "approximate": true
    }
  ]
}
```

Then search with `GET /api/workspaces/{workspace-slug}/search/?q=AAA%20batteries` or use the MCP
`find_inventory` tool.
