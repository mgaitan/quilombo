#!/usr/bin/env bash
# Exit on error
set -o errexit

uv sync --locked --no-dev

# Convert static asset files
uv run --no-sync python manage.py collectstatic --no-input

# Apply any outstanding database migrations
uv run --no-sync python manage.py migrate

# Staging keeps a synthetic demo dataset so the app is usable right after deploy
if [ "${APP_ENV:-}" = "staging" ]; then
  uv run --no-sync python manage.py seed_demo_data --ensure
fi
