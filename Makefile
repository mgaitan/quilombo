.PHONY: install lint format format-check test docs docs-open qa help

DOCS_SOURCE := docs
DOCS_BUILD := $(DOCS_SOURCE)/_build

install: ## Install application and documentation dependencies
	uv sync --all-groups

lint: ## Run Ruff lint checks
	uv run ruff check .

format: ## Format Python files with Ruff
	uv run ruff format .

format-check: ## Check Python formatting without changing files
	uv run ruff format --check .

test: ## Run the test suite
	uv run pytest -q

docs: ## Build strict Sphinx/MyST documentation
	uv run --group docs sphinx-build -b html -W --keep-going $(DOCS_SOURCE) $(DOCS_BUILD)/html

docs-open: docs ## Build documentation and open it in a browser
	uv run python -m webbrowser $(DOCS_BUILD)/html/index.html

qa: lint format-check test docs ## Run the local quality gate

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help
