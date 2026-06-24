#!/bin/bash
set -e

# Usage: ./start.sh [antigravity|claude]
# Selects which agent backend to launch. Defaults to antigravity for backward-compat.
BACKEND="${1:-antigravity}"

case "$BACKEND" in
    antigravity|claude)
        ;;
    *)
        echo "Error: unknown backend '$BACKEND'. Use 'antigravity' or 'claude'."
        exit 1
        ;;
esac

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

# For the Claude backend, ensure the dedicated config directory exists on the host so
# the sandbox's login + MCP registration persist across runs (without touching the
# user's real ~/.claude config).
if [ "$BACKEND" = "claude" ]; then
    mkdir -p "$HOME/.odoo-debugger-claude"
fi

cd "$SCRIPT_DIR"

echo "Starting Odoo Debugger Sandbox ($BACKEND backend)..."
# Run the selected backend service via its compose profile
docker compose --profile "$BACKEND" run --rm "sandbox-$BACKEND"
