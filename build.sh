#!/usr/bin/env bash
# Exit on error
set -o errexit

uv sync --frozen --no-dev

# Convert static asset files
uv run --no-sync python manage.py collectstatic --no-input

# Apply any outstanding database migrations
uv run --no-sync python manage.py migrate
