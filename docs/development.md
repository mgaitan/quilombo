# Development

## Environment

Install the application dependencies with:

```bash
uv sync
```

Quilombo uses PostgreSQL in production. For a production-equivalent local environment, run a
PostgreSQL database and configure its connection before applying migrations. The CI environment
uses the official PostgreSQL 17 image; the same setup works locally with Docker:

```bash
make postgres-up
make postgres-migrate
```

The inventory search uses PostgreSQL full-text search, `unaccent`, and `pg_trgm`. The inventory
migration enables the required extensions when the connected database is PostgreSQL. The default
SQLite configuration remains useful for quick local checks, but it exercises the compatibility
search path rather than the production search path.

Run the tests against PostgreSQL and stop the local database when finished:

```bash
make test-postgres
make postgres-down
```

Documentation dependencies are optional and can be installed separately:

```bash
uv sync --group docs
```

Run the application checks:

```bash
uv run pytest
uv run ruff check .
uv run python manage.py check
```

The repository `Makefile` provides shortcuts for the recurring workflows. `make install` is the
equivalent of `uv sync --all-groups`, so it also installs the optional documentation dependencies.
`make qa` runs the complete local quality gate: linting, format checks, tests, and a strict docs
build.

```bash
make install
make qa
make docs-open
```

Use `make help` to list all targets. The release workflow uses `make docs` to build the published
site with warnings treated as errors.

## Releases

Render does not deploy branch pushes. Its deploy hook is stored as the GitHub Actions repository
secret `RENDER_DEPLOY_HOOK_URL`; never commit that URL. Publishing a GitHub release runs the release
workflow, verifies that its tag is `v` followed by the version in `pyproject.toml`, publishes the
documentation, and asks Render to deploy that exact tagged commit.

For a new release, update the project version, merge the change to `main`, and publish the matching
tag. For example, version `0.2.0` must be released with tag `v0.2.0`. The running version appears in
the web footer, the `/health/` response, the OpenAPI schema, and MCP initialization metadata.

## Build the documentation

```bash
uv run --group docs sphinx-build -W --keep-going docs docs/_build/html
```

Open `docs/_build/html/index.html` in a browser. Markdown files use MyST, including fenced
directives such as Mermaid diagrams.

## Project workflow

Work on `integration/v1`, track implementation in a GitHub issue, and use Conventional Commits.
Keep tenant isolation, idempotent bulk writes, and the distinction between stored facts and client
reasoning explicit in code and documentation.
