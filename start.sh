#!/bin/bash
set -e

# Get the directory where this script is located (odoo-debugger-agent)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The user's main source directory is expected to be the parent directory
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

# Validate that the user is running this in the correct workspace
if [ ! -d "$SOURCE_DIR/odoo" ] || [ ! -d "$SOURCE_DIR/enterprise" ]; then
    echo "Error: Could not find 'odoo' and/or 'enterprise' directories."
    echo "Please ensure the 'odoo-debugger-agent' folder is located alongside the 'odoo' and 'enterprise' repositories."
    exit 1
fi

echo "'odoo' and 'enterprise' repositories found."

# Security for private repos - extendable
if [ ! -d "$SOURCE_DIR/internal" ]; then
    mkdir -p "$SOURCE_DIR/internal"
fi

# Ensure the dedicated config directory exists on the host so the sandbox's
# login + MCP registration persist across runs (without touching the user's
# real ~/.claude config).
mkdir -p "$HOME/.odoo-debugger-claude"

cd "$SCRIPT_DIR"

# Optional: ./start.sh <db_name> — ensure that database is present and
# scrubbed on the ai-dev cluster (the only Postgres the container can reach)
# before launching. If it's missing, copy it over from main and scrub it
# here on the host, since the container itself can never reach main.
DB_NAME="${1:-}"
AI_DEV_HOST=/var/run/postgresql
AI_DEV_PORT=5433
AI_DEV_USER=odoo

if [ -n "$DB_NAME" ]; then
    STATUS=$(psql -h "$AI_DEV_HOST" -p "$AI_DEV_PORT" -U "$AI_DEV_USER" -d "$DB_NAME" -tAc \
        "SELECT value FROM ir_config_parameter WHERE key = 'scrubdb.run_completed'" 2>/dev/null || true)

    if [ "$STATUS" = "true" ]; then
        echo "Database '$DB_NAME' already scrubbed on ai-dev — skipping copy."
    elif [ -z "$STATUS" ] && psql -h "$AI_DEV_HOST" -p "$AI_DEV_PORT" -U "$AI_DEV_USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1; then
        echo "Error: '$DB_NAME' exists on ai-dev but isn't marked as scrubbed."
        echo "Inspect/drop it manually before retrying (won't auto-overwrite)."
        exit 1
    else
        echo "Database '$DB_NAME' not found on ai-dev — copying + scrubbing from main..."
        "$SCRIPT_DIR/scripts/copy_and_scrub_db.sh" "$DB_NAME"
    fi
    export DB_NAME
fi

echo "Starting Odoo Debugger Sandbox..."
docker compose run --rm sandbox
