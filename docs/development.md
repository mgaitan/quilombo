# Development

## Environment

Install the application and documentation dependencies with:

```bash
uv sync --group docs
```

Run the application checks:

```bash
uv run pytest
uv run ruff check .
uv run python manage.py check
```

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
