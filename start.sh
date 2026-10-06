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
# scrubbed on the aidev cluster (the only Postgres the container can reach)
# before launching. If it's missing, copy it over from main and scrub it
# here on the host, since the container itself can never reach main.
DB_NAME="${1:-}"
AI_DEV_HOST=/var/run/postgresql
AI_DEV_PORT=5433
AI_DEV_USER=odoo

# Scrub-config registry, on the main cluster. Holds each database's allowlist +
# scrub rules and an append-only snapshot of every run, so config survives
# scrubbing other databases and a re-scrub can start from what was last applied.
export REGISTRY_HOST=/var/run/postgresql
export REGISTRY_PORT=5432
export REGISTRY_USER=odoo
export REGISTRY_DB=scrubdb_registry

if [ -n "$DB_NAME" ]; then
    STATUS=$(psql -h "$AI_DEV_HOST" -p "$AI_DEV_PORT" -U "$AI_DEV_USER" -d "$DB_NAME" -tAc \
        "SELECT value FROM ir_config_parameter WHERE key = 'scrubdb.run_completed'" 2>/dev/null || true)

    if [ "$STATUS" = "true" ]; then
        echo "Database '$DB_NAME' already scrubbed on aidev — skipping copy."
    elif [ -z "$STATUS" ] && psql -h "$AI_DEV_HOST" -p "$AI_DEV_PORT" -U "$AI_DEV_USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1; then
        echo "Error: '$DB_NAME' exists on aidev but isn't marked as scrubbed."
        echo "Inspect/drop it manually before retrying (won't auto-overwrite)."
        exit 1
    else
        echo "Database '$DB_NAME' not found on aidev — collecting scrub config + copying from main..."
        REGISTRY_ARGS=(--host "$REGISTRY_HOST" --port "$REGISTRY_PORT" \
            --user "$REGISTRY_USER" --dbname "$REGISTRY_DB")
        python3 "$SCRIPT_DIR/scripts/scrub_registry.py" init "${REGISTRY_ARGS[@]}"
        SCRUB_CFG_DIR="$(mktemp -d)"
        # Opens $EDITOR prefilled with this database's last run, or the shared
        # defaults on a first scrub. Writes allowlist.txt + ruleset.yml there.
        python3 "$SCRIPT_DIR/scripts/scrub_registry.py" collect "$DB_NAME" \
            "${REGISTRY_ARGS[@]}" --out-dir "$SCRUB_CFG_DIR"
        export SCRUB_ALLOWLIST_FILE="$SCRUB_CFG_DIR/allowlist.txt"
        export SCRUB_RULESET_FILE="$SCRUB_CFG_DIR/ruleset.yml"
        "$SCRIPT_DIR/scripts/copy_and_scrub_db.sh" "$DB_NAME"
    fi
    export DB_NAME
fi

echo "Starting Odoo Debugger Sandbox..."
# Build the sandbox's `debugger` user with THIS user's ids, so edits it makes
# to the bind-mounted repos under /source land as you on the host (and so it
# can write them at all — see the Dockerfile's HOST_UID comment).
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
export HOST_UID HOST_GID
# --build re-evaluates the image against the current Dockerfile/build context
# every run; Docker's own layer cache makes this a no-op (a few hundred ms)
# when nothing changed, but without it `docker compose run` silently keeps
# using whatever image was last built, even after Dockerfile/mcp_servers
# edits — that drift is exactly what let removed MCP tools (e.g.
# connect_debugger) keep running in a stale container.
docker compose run --build --rm sandbox
