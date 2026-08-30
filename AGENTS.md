# Quilombo Development Guide

## Workflow

- When the user explicitly requests implementation through a pull request, do not create a new
  tracking issue just for that work. Explain the scope, decisions, and verification in the pull
  request instead.
- Use GitHub issues to record work that may be implemented later. If an implementation does not
  fully cover an existing request, leave that issue open and create/link a follow-up pull request
  for the missing scope.
- Work on a dedicated branch and open a separate pull request for each requested change.
- Never merge a pull request unless the user explicitly asks to merge that specific pull request.
- Never publish a GitHub Release unless the user explicitly asks to publish that specific release.
- Requests to implement work, open a pull request, or prepare a release do not authorize merging or publishing.
- Merge reviewed pull requests before preparing a release.
- Deploy production only by publishing a GitHub Release; branch pushes must not deploy.
- Use Conventional Commits and reference an existing issue in commit bodies when applicable.
- Keep commits focused and leave unrelated working-tree changes untouched.

### Release versioning

- Follow Semantic Versioning for application releases.
- Use a patch release for fixes, documentation, and other changes that do not change behavior.
- Use a minor release for database migrations or changes to business logic and user-visible behavior.
- Reserve major releases for intentionally breaking changes to public contracts.

## Commands

- Install dependencies: `uv sync`
- Run checks: `uv run python manage.py check`
- Run tests: `uv run pytest`
- Lint and format: `uv run ruff check .` and `uv run ruff format --check .`
- Create migrations: `uv run python manage.py makemigrations`

## Domain Invariants

- Every inventory record belongs to exactly one workspace.
- Never accept a related object from a different workspace.
- Inventory writes go through application services and database transactions.
- Quantities use decimals, include a unit, and cannot be negative.
- Bulk writes are idempotent and use stable client-provided keys.
- Provenance is metadata supplied by the client. Quilombo does not upload or process source media.
- Quilombo stores and retrieves facts; semantic reasoning, vision, and confirmation policy belong to clients and agent skills.
- Do not hide mutations inside read operations or MCP tools marked read-only.

## Testing

- Test tenant isolation for every new query or mutation surface.
- Test idempotency and rollback behavior for bulk operations.
- Cover workshop and library examples when changing shared inventory behavior.
- Keep the OpenAPI schema and MCP tool schemas valid.

## Scope

- Prefer a Django monolith and PostgreSQL-native capabilities.
- Do not add GraphQL, background workers, vector search, or media storage until a measured use case requires them.
- The database starts from zero; migrations do not need to preserve the 2024 prototype fixture.
