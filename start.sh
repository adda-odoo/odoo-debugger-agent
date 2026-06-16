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

cd "$SCRIPT_DIR"

echo "Starting Odoo Debugger Sandbox..."
# Run the sandbox using docker compose
docker compose run --rm sandbox
