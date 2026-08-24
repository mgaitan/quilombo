#!/usr/bin/env bash
# Exit on error
set -o errexit

uv sync --locked --no-dev

# Convert static asset files
uv run --no-sync python manage.py collectstatic --no-input

# Apply any outstanding database migrations
uv run --no-sync python manage.py migrate

if [[ "${IS_PROD:-}" == "1" && -n "${QUILOMBO_ADMIN_USERNAME:-}" ]]; then
    uv run --no-sync python manage.py ensure_admin "$QUILOMBO_ADMIN_USERNAME"
fi
