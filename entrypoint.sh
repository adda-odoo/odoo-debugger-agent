#!/bin/bash
# entrypoint.sh - Automated setup for Odoo Debugger Agent

echo "--- Odoo Debugger Environment Setup ---"

# Ensure we have a valid HOME
export HOME=/home/gemini
USER_HOME="/home/gemini"
STAGING_DIR="/opt/odoo-debug"
GEMINI_CONFIG_DIR="$USER_HOME/.gemini"
AGENTS_DIR="$GEMINI_CONFIG_DIR/agents"
GLOBAL_SKILLS_DIR="$USER_HOME/.agents/skills"

# 1. Create necessary directories
mkdir -p "$AGENTS_DIR"
mkdir -p "$GLOBAL_SKILLS_DIR"

# 2. Deploy the Agent definition
echo "Deploying @odoo-debugger agent..."
if [ -f "$STAGING_DIR/odoo-debugger.md" ]; then
    cp "$STAGING_DIR/odoo-debugger.md" "$AGENTS_DIR/"
    echo "  [OK] Agent definition deployed."
fi

# 3. Deploy Skills
# We deploy to the standard global location. 
# If ~/.gemini is mounted, this stays persistent.
echo "Deploying Odoo debugger skills..."
if [ -d "$STAGING_DIR/skills/odoo-debugger" ]; then
    # Use rsync-like behavior to update without causing link loops
    cp -r "$STAGING_DIR/skills/odoo-debugger" "$GLOBAL_SKILLS_DIR/"
    echo "  [OK] Skills deployed to $GLOBAL_SKILLS_DIR"
fi

# 4. Register and Enable MCP Server
echo "Configuring odoo-debug MCP server..."
if gemini mcp add odoo-debug /usr/bin/python3 "$STAGING_DIR/debug_server.py" --scope project; then
    echo "  [OK] MCP server registered in project scope."
    gemini mcp enable odoo-debug --scope project 2>/dev/null || true # TODO needs better exception handling
else
    echo "  [ERROR] MCP server registration failed."
fi

# 5. Check for previous crash logs
if [ -f "/tmp/odoo_mcp_error.log" ]; then
    echo "--- !!! PREVIOUS MCP CRASH DETECTED !!! ---"
    cat "/tmp/odoo_mcp_error.log"
    echo "------------------------------------------"
fi

echo "--- Setup Diagnostics ---"
echo "Current user: $(whoami) (UID: $(id -u))"
echo "Config folder: $AGENTS_DIR"
echo "--------------------------"
echo ""

# Execute the passed command
exec "$@"
