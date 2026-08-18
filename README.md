# Quilombo

Quilombo is deterministic, multitenant storage for physical inventories. Agents perform vision,
semantic interpretation, and organization reasoning; Quilombo stores locations, items, current
holdings, spatial relations, and mutation provenance.

The v0.1 API deliberately uses REST plus an HTTP MCP instead of GraphQL. Its main write path is a
transactional, idempotent bulk command, while reads and ordinary CRUD remain simple HTTP resources.

## Run locally

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

Open `http://127.0.0.1:8000/` to create an account, inspect inventories, and read the connector
guide. API documentation is available at `http://127.0.0.1:8000/api/docs/`.

Useful checks:

```bash
uv run pytest
uv run ruff check .
uv run python manage.py check
```

Build the Sphinx/MyST documentation locally:

```bash
uv sync --group docs
uv run --group docs sphinx-build -W --keep-going docs docs/_build/html
```

## Use the API

1. Create an account; it automatically gets a private `Home` workspace. Additional workspaces can
   be created through `POST /api/workspaces/` in a browser session.
2. Issue a workspace token through `POST /api/workspaces/{slug}/tokens/` while logged in.
3. Save the returned `qlo_...` token; only its hash is retained and the raw value is shown once.
4. Send it as `Authorization: Bearer qlo_...` to REST or the MCP endpoint.

Bulk writes use `POST /api/workspaces/{slug}/bulk-upsert/`. Holding quantities are set to the
supplied current values. Every mutation should carry a unique idempotency key and may include a
short provenance reference such as “processed from a workshop photo on 2026-08-14”; source media is
not uploaded or retained.

## Connect an MCP client

The hosted v0.1 app is available at
[`https://quilombo-v1-mgaitan.onrender.com`](https://quilombo-v1-mgaitan.onrender.com). Its
Streamable HTTP endpoint is `https://quilombo-v1-mgaitan.onrender.com/mcp`. ChatGPT, Claude, and
other OAuth-capable clients register dynamically, open Quilombo for login, and ask which workspace
to authorize. Long-lived workspace bearer tokens remain available for clients without OAuth. The
server exposes four tools:

- `find_inventory`
- `get_inventory_snapshot`
- `bulk_upsert_inventory`
- `move_inventory`

The curated client workflow lives in
[`skills/manage-quilombo-inventory`](skills/manage-quilombo-inventory/SKILL.md). It requires drafts
and confirmation on the client side because mutating tools write immediately.

Current ChatGPT and Claude setup steps are maintained at `/connect/` in the app.

## Deploy

`render.yaml` and `build.sh` describe the Render service. Set `DATABASE_URL` to a pooled PostgreSQL
connection string; deployments install from `uv.lock`, collect static files, and run migrations.

The free Render service can sleep while idle, so its first request after inactivity may take longer.
The health endpoint at `/health/` verifies both the app and database connection.
