# Quilombo Development Guide

## Workflow

- Track implementation work in a GitHub issue before editing code.
- Work on `integration/v1`; do not open a pull request for each commit.
- Use Conventional Commits and reference completed issues in commit bodies.
- Keep commits focused and leave unrelated working-tree changes untouched.

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
