# Quilombo

[![CI](https://github.com/mgaitan/quilombo/actions/workflows/ci.yml/badge.svg)](https://github.com/mgaitan/quilombo/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/mgaitan/quilombo)](https://github.com/mgaitan/quilombo/releases/latest)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](pyproject.toml)

> **quilombo** /kee-LOM-bo/
>
> *noun, Rioplatense lunfardo.* A mess, a chaotic tangle, a situation that has gotten out of hand.

Quilombo is an **MCP-powered inventory management system**. It keeps track of physical things so
an AI agent can help you find them again.

The project started after a search for some hinges ended with buying another pair, only for the
originals to turn up later. A workshop drawer, a bookshelf, a storage room, or a whole house can
all become inventories.

You describe what you have and where it is in ordinary language. An agent turns that description
into structured facts and stores them in Quilombo. Later, the same agent can answer questions such
as "where are the 6 mm drill bits?" or update the inventory while you are already looking inside a
drawer.

Vision and semantic interpretation run in the client or agent. Quilombo has the narrower job of
storing locations, items, quantities, spatial relations, freshness, and the provenance of each
change. It exposes that data through a web interface, a REST API and, most importantly, a remote
Streamable HTTP MCP server.

[Read the longer account of why the project
exists](https://mgaitan.github.io/en/posts/quilombo-agents-to-organize-real-life/).

## How the inventory works

Locations form a tree whose level of detail can grow over time:

```text
Workshop
`-- Cabinet
    `-- Red toolbox
        `-- Compartment A4
```

You can begin with "the electronics are in the red toolbox" and later record that twelve green
LEDs are in compartment A4. Relative relations such as `left_of`, `above`, and `near` provide clues
that do not fit the containment tree.

An item describes the thing being tracked. A holding records its location and quantity. The same
item can have holdings in several locations. Names, aliases, categories, descriptions, and
free-form attributes give the search enough vocabulary to identify a record without pretending to
understand it semantically.

Every record belongs to one workspace. Workspaces can represent `Home`, `Workshop`, `Library`, or
another independent inventory, and can be shared with people or agents as read/write or read-only.
Queries never cross workspace boundaries.

Audits record when a location or holding was physically checked. Search results expose whether a
fact is current, stale, or unknown, allowing clients to ask useful follow-up questions without
turning every lookup into another inventory session. Quilombo retains client-supplied provenance,
such as `checked drawer 1 on 2026-08-22`; it does not upload or retain the source photo or video.

## Try the hosted app

The public instance is available at
[`quilombo.life`](https://quilombo.life/). Create an account
to get a private `Home` workspace, search from the browser, invite another user, or connect an
agent. The free Render service may take a minute to wake after a period without traffic.

The app shows its running version in the footer. The same version is returned by `/health/`, the
OpenAPI schema, and MCP initialization metadata.

## Connect an MCP client

The hosted Streamable HTTP endpoint is:

```text
https://quilombo.life/mcp
```

ChatGPT, Claude, and other OAuth-capable clients can register dynamically, open Quilombo for login,
and ask which workspace to authorize. Clients without OAuth can use a long-lived workspace bearer
token. Current client-specific setup steps live at [/connect/](https://quilombo.life/connect/) in the web app.

The MCP server provides these tools:

| Tool | Purpose |
| --- | --- |
| `find_inventory` | Find ranked holdings and their recorded locations. |
| `get_inventory_snapshot` | Read locations, relations, and holdings together. |
| `get_inventory_status` | Find recorded quantities below their configured minimum. |
| `lookup_book_by_isbn` | Fetch a bibliographic draft from Open Library. |
| `audit_inventory` | Verify a location and selected holdings. |
| `bulk_upsert_inventory` | Create or replace related inventory facts in one transaction. |
| `move_inventory` | Move a quantity between known locations. |
| `update_inventory_item` | Correct a known item and its holdings. |
| `delete_inventory_item` | Remove an erroneous or duplicate item. |

Mutating tools write immediately. Clients should show the proposed change and obtain confirmation
before calling them. Every intended mutation gets a unique idempotency key and may include a short
provenance reference.

The MCP server sends basic usage guidance to compatible clients. The more detailed conversational
workflow lives in
[`skills/manage-quilombo-inventory`](skills/manage-quilombo-inventory/SKILL.md), including how to
handle uncertainty, drafts, stale records, photos interpreted by the client, and opportunistic
verification.

The repository is also an [Agent Plugins 1.0](https://agent-plugins.org/) package. Compatible
clients discover the same skill and hosted MCP endpoint from `plugin.json` and `mcp.json`; ChatGPT
and Codex use the OpenAI metadata in `.codex-plugin/plugin.json`. See the
[plugin packaging guide](docs/plugin/index.md) and [submission evaluation](docs/plugin/evaluation.md).

## Run locally

Quilombo requires Python 3.14 and [uv](https://docs.astral.sh/uv/). Local development uses SQLite
by default; production uses PostgreSQL.

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

Open `http://127.0.0.1:8000/` for the web app. REST API documentation is available at
`http://127.0.0.1:8000/api/docs/`.

Password login works without additional configuration. To enable Google or GitHub login, set the
matching pair of environment variables:

```bash
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
```

On Render, use these exact variable names and redeploy after saving them. The GitHub OAuth app
must also use `https://quilombo.life/accounts/github/login/callback/` as its authorization callback
URL. The Google callback is `https://quilombo.life/accounts/google/login/callback/`.

Configure the provider applications with these production callback URLs:

```text
https://quilombo.life/accounts/google/login/callback/
https://quilombo.life/accounts/github/login/callback/
```

For local provider applications, replace the origin with `http://127.0.0.1:8000`. A provider whose
credential pair is absent is not shown on the login or signup page.

Run the project checks with:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
```

Pull requests to `main` run these checks against PostgreSQL, verify that model changes include
migrations, and validate the generated OpenAPI schema. CI does not deploy the application.

## Use the REST API

1. Create an account. Quilombo creates a private `Home` workspace automatically.
2. Create another workspace with `POST /api/workspaces/` or use the web interface.
3. Issue a token with `POST /api/workspaces/{slug}/tokens/`.
4. Save the returned `qlo_...` value. Quilombo stores only its hash and shows the raw token once.
5. Send it as `Authorization: Bearer qlo_...` to REST or MCP.

The main write endpoint is `POST /api/workspaces/{slug}/bulk-upsert/`. It applies related changes in
one transaction. Holding quantities are replacement values, rather than deltas.

Collection responses use a stable paginated envelope:

```json
{
  "pagination": {
    "count": 123,
    "page": 1,
    "page_size": 50,
    "total_pages": 3,
    "next": "http://localhost:8000/api/workspaces/?page=2",
    "previous": null
  },
  "results": []
}
```

Use `page` and `page_size`; page sizes are capped at 200. Search includes query diagnostics and
reports when its bounded candidate set was truncated. MCP collection reads use their own opaque
`next_cursor` continuation contract; see [MCP integration](docs/mcp.md).

Workspace transfer is available through `GET /api/workspaces/{slug}/export/?format=json|csv` and
`POST /api/workspaces/{slug}/import/`. Imports preserve stable record UUIDs, support a non-mutating
dry run, commit atomically, and record provenance. See
[Import and export](docs/import_export.md) for the versioned contract.

## Documentation

Build the Sphinx/MyST documentation locally:

```bash
uv sync --group docs
uv run --group docs sphinx-build -W --keep-going docs docs/_build/html
```

The published documentation is at
[`mgaitan.github.io/quilombo`](https://mgaitan.github.io/quilombo/). It covers the
[data model](docs/concepts.md), [audits and freshness](docs/audits.md),
[MCP integration](docs/mcp.md), and [architecture](docs/architecture.md).

## Deploy and release

`render.yaml` and `build.sh` describe the Render service. Set `DATABASE_URL` to a pooled PostgreSQL
connection string. A deployment installs from `uv.lock`, collects static files, and runs
migrations. `/health/` checks both the application and its database connection.

`main` is the development branch. Production deploys only when a GitHub release is published:

```bash
gh release create v0.3.1 --generate-notes
```

The release workflow checks that the tag matches the package version, publishes the documentation
to GitHub Pages, and asks Render to deploy that tagged commit. Branch pushes and pull requests do
not deploy production.

To roll back a release, revert the change on `main`, bump the patch version, and publish a new
release. Published version tags are never moved or reused.
