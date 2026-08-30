.PHONY: install lint format format-check test postgres-up postgres-down postgres-migrate test-postgres neon-local-status neon-local-url neon-local-refresh neon-local-migrate runserver-neon-local docs docs-open qa help

DOCS_SOURCE := docs
DOCS_BUILD := $(DOCS_SOURCE)/_build
POSTGRES_CONTAINER ?= quilombo-postgres
POSTGRES_IMAGE ?= postgres:17
POSTGRES_URL ?= postgresql://quilombo:quilombo@localhost:5432/quilombo

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

postgres-up: ## Start the local PostgreSQL container used by CI
	@if docker ps --format '{{.Names}}' | grep -Fxq '$(POSTGRES_CONTAINER)'; then \
		echo "$(POSTGRES_CONTAINER) is already running"; \
	else \
		docker run --name $(POSTGRES_CONTAINER) --rm --detach \
			--env POSTGRES_DB=quilombo \
			--env POSTGRES_USER=quilombo \
			--env POSTGRES_PASSWORD=quilombo \
			--publish 5432:5432 \
			$(POSTGRES_IMAGE); \
	fi
	@until docker exec $(POSTGRES_CONTAINER) pg_isready -U quilombo -d quilombo; do sleep 1; done

postgres-down: ## Stop the local PostgreSQL container
	-docker stop $(POSTGRES_CONTAINER)

postgres-migrate: postgres-up ## Apply migrations to the local PostgreSQL database
	DATABASE_URL=$(POSTGRES_URL) uv run python manage.py migrate

test-postgres: postgres-up ## Run the test suite against PostgreSQL
	DATABASE_URL=$(POSTGRES_URL) uv run pytest -q

neon-local-status: ## Show the isolated Neon branch used by local PR testing
	@scripts/neon-local-db.sh status

neon-local-url: ## Print the isolated Neon connection URL
	@scripts/neon-local-db.sh url

neon-local-refresh: ## Recreate the isolated Neon branch from the production snapshot
	@scripts/neon-local-db.sh refresh

neon-local-migrate: ## Apply current Django migrations to the isolated Neon branch
	@DATABASE_URL="$$(scripts/neon-local-db.sh url)" IS_PROD=0 RESEND_API_KEY= uv run python manage.py migrate

runserver-neon-local: ## Run the web app against the isolated Neon branch
	@set -a; [ ! -f .env ] || . ./.env; set +a; unset PROD_DATABASE_URL; DATABASE_URL="$$(scripts/neon-local-db.sh url)" IS_PROD=0 RESEND_API_KEY= PUBLIC_BASE_URL=http://localhost:8000 uv run python manage.py runserver 127.0.0.1:8000

docs: ## Build strict Sphinx/MyST documentation
	uv run --group docs sphinx-build -b html -W --keep-going $(DOCS_SOURCE) $(DOCS_BUILD)/html

docs-open: docs ## Build documentation and open it in a browser
	uv run python -m webbrowser $(DOCS_BUILD)/html/index.html

qa: lint format-check test docs ## Run the local quality gate

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help
