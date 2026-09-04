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

## Local PR testing with production-shaped data

Local PR testing uses the Neon branch `local-pr-testing`, forked from the production branch. It is
an isolated read-write database: local mutations stay on that branch and cannot change production.
The branch contains a snapshot, not a live replica, so refresh it when current production data is
needed. Refreshing deletes the local branch and all test writes on it.

The helper reads `NEON_API_KEY` from the environment or `.env`, and never reads or writes
`PROD_DATABASE_URL`:

```bash
make neon-local-status
make neon-local-refresh
make neon-local-migrate
make runserver-neon-local
```

`make runserver-neon-local` forces development settings and the console email backend. Do not add
production OAuth or email credentials to the local environment. The copied database may contain
real account and inventory data, so keep the connection string private and use this branch only on
trusted machines. The branch ID, project ID, database, and role can be overridden with
`NEON_PRODUCTION_BRANCH_ID`, `NEON_PROJECT_ID`, `NEON_DATABASE_NAME`, and `NEON_DATABASE_ROLE`.

Use `make help` to list all targets. The release workflow uses `make docs` to build the published
site with warnings treated as errors.

## Staging environment

`quilombo-staging` (`render.yaml`) is a persistent online copy for exercising a branch or a set of
merged branches with a real login. It deploys whatever is on the `staging` branch against its own
Neon branch and a synthetic dataset.

Deploy a branch to it:

```bash
make staging-deploy            # current branch
make staging-deploy REF=integration/open-prs-local
```

`git push origin <ref>:staging --force` triggers a Render deploy. `build.sh` runs migrations and,
because `APP_ENV=staging`, `manage.py seed_demo_data --ensure`.

Sign in with the seeded account: username `demo`, password from `DEMO_USER_PASSWORD` (default
`quilombo-demo`). `APP_ENV=staging` disables OAuth and the mail provider and makes email
verification optional, so no external accounts are needed. It still runs behind HTTPS with
production cookie and HSTS settings. `RENDER_EXTERNAL_HOSTNAME` supplies the allowed hosts,
`PUBLIC_BASE_URL`, and the CSRF/MCP origins automatically.

Rebuild the demo data at any time with `manage.py seed_demo_data --refresh`; it only ever touches
the `demo-*` workspaces and the `demo` user.

On staging the footer shows the deployed commit (`RENDER_GIT_COMMIT`, short form) as a link
to its GitHub diff against the last released tag (`v<version>`), instead of the plain
version. `/health/` also returns `revision` and `environment`.

**Staging is not for real data.** Seed it synthetically; do not fork production into it.

One-time setup: create the `quilombo-staging` service from the blueprint, create a persistent Neon
branch for it and set `DATABASE_URL`, and set `DEMO_USER_PASSWORD` (and optionally `SENTRY_DSN`).

## Releases

Render does not deploy branch pushes. Its deploy hook is stored as the GitHub Actions repository
secret `RENDER_DEPLOY_HOOK_URL`; never commit that URL. Publishing a GitHub release runs the release
workflow, verifies that its tag is `v` followed by the version in `pyproject.toml`, publishes the
documentation, and asks Render to deploy that exact tagged commit.

For a new release, update the project version, merge the change to `main`, and publish the matching
tag. For example, version `0.4.0` must be released with tag `v0.4.0`. The running version appears in
the web footer, the `/health/` response, the OpenAPI schema, and MCP initialization metadata.

### Migration validation gate

Before Render is triggered, the release workflow validates the exact release tag against a
throwaway Neon branch cloned from production:

1. `neondatabase/create-branch-action` forks the production branch as
   `release/<tag>-<run_id>`.
2. `manage.py migrate --noinput`, then `migrate --check`, `check`, and the read-only
   `manage.py release_smoke` command run against that branch using its direct connection
   string.
3. `neondatabase/delete-branch-action` removes the branch on success, failure, or
   cancellation.
4. `deploy-render` needs this job, so a failed migration or smoke check stops the deploy.

The workflow uses a single `production-release` concurrency group so two releases cannot
validate and deploy at the same time.

Required configuration:

| Name | Type | Purpose |
| --- | --- | --- |
| `NEON_API_KEY` | secret | create/delete Neon branches |
| `NEON_PROJECT_ID` | variable | Neon project |
| `NEON_PRODUCTION_BRANCH_ID` | variable | parent branch to clone |

**Limits.** The branch has production-shaped schema and data but no production traffic, so
this check does not prove zero-downtime behaviour, lock duration, or old/new application
overlap. Keep migrations backward-compatible (expand/contract) regardless.

### Versioning policy

Quilombo follows Semantic Versioning. Use patch releases for fixes, documentation, and other
changes that do not alter behavior. Use minor releases when a change adds a database migration or
changes business logic or user-visible behavior. Reserve major releases for intentionally breaking
changes to public contracts.

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
