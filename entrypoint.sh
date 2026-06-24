#!/bin/bash
# entrypoint.sh - Automated setup for Odoo Debugger Agent
#
# Supports two interchangeable backends, selected via the AGENT_BACKEND env var:
#   - antigravity : Google's Antigravity CLI (`agy`)   [default, for backward-compat]
#   - claude      : Claude Code CLI (`claude`)

echo "--- Odoo Debugger Environment Setup ---"

# Ensure we have a valid HOME
export HOME=/home/debugger
USER_HOME="/home/debugger"
STAGING_DIR="/opt/odoo-debug"
AGENT_BACKEND="${AGENT_BACKEND:-antigravity}"

echo "Selected backend: $AGENT_BACKEND"

deploy_antigravity() {
    ANTIGRAVITY_CONFIG_DIR="$USER_HOME/.gemini/antigravity-cli"
    AGENTS_DIR="$ANTIGRAVITY_CONFIG_DIR/agents"
    GLOBAL_SKILLS_DIR="$ANTIGRAVITY_CONFIG_DIR/skills"

    # 1. Create necessary directories
    mkdir -p "$AGENTS_DIR"
    mkdir -p "$GLOBAL_SKILLS_DIR"

    # 2. Deploy the Agent definition
    echo "Deploying @odoo-debugger agent..."
    if [ -f "$STAGING_DIR/agents/odoo-debugger/agent.json" ]; then
        mkdir -p "$AGENTS_DIR/odoo-debugger"
        cp "$STAGING_DIR/agents/odoo-debugger/agent.json" "$AGENTS_DIR/odoo-debugger/"
        echo "  [OK] Agent definition deployed."
    fi

    # 3. Deploy Skills
    # We deploy to the standard global location and workspace location.
    # If ~/.gemini is mounted, the global one stays persistent.
    echo "Deploying Odoo debugger skills..."
    if [ -d "$STAGING_DIR/skills/odoo-debugger" ]; then
        mkdir -p "$GLOBAL_SKILLS_DIR"
        mkdir -p "/source/.agents/skills"
        cp -r "$STAGING_DIR/skills/odoo-debugger" "$GLOBAL_SKILLS_DIR/"
        cp -r "$STAGING_DIR/skills/odoo-debugger" "/source/.agents/skills/"
        echo "  [OK] Skills deployed to $GLOBAL_SKILLS_DIR and /source/.agents/skills"
    fi

    # 4. Register and Enable MCP Server
    echo "Configuring odoo-debug MCP server..."
    python3 -c "
import os, json
agents_dir = '/source/.agents'
os.makedirs(agents_dir, exist_ok=True)
config_path = os.path.join(agents_dir, 'mcp_config.json')
config = {'mcpServers': {}}
if os.path.exists(config_path):
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except Exception:
        pass
if 'mcpServers' not in config:
    config['mcpServers'] = {}
config['mcpServers']['odoo-debug'] = {
    'command': '/usr/bin/python3',
    'args': ['/opt/odoo-debug/debug_server.py']
}
with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
"
    echo "  [OK] MCP server registered in workspace scope."
    echo "Config folder: $AGENTS_DIR"
}

deploy_claude() {
    # Claude Code reads project config from the working directory (/source).
    CLAUDE_PROJECT_DIR="/source/.claude"
    CLAUDE_AGENTS_DIR="$CLAUDE_PROJECT_DIR/agents"
    CLAUDE_SKILLS_DIR="$CLAUDE_PROJECT_DIR/skills"

    # 1. Deploy the subagent (flat markdown file).
    echo "Deploying odoo-debugger subagent..."
    if [ -f "$STAGING_DIR/agents/odoo-debugger/odoo-debugger.md" ]; then
        mkdir -p "$CLAUDE_AGENTS_DIR"
        cp "$STAGING_DIR/agents/odoo-debugger/odoo-debugger.md" "$CLAUDE_AGENTS_DIR/odoo-debugger.md"
        echo "  [OK] Subagent deployed to $CLAUDE_AGENTS_DIR"
    fi

    # 2. Deploy Skills (idempotent overwrite so they are always current).
    echo "Deploying Odoo debugger skills..."
    if [ -d "$STAGING_DIR/skills/odoo-debugger" ]; then
        mkdir -p "$CLAUDE_SKILLS_DIR"
        cp -r "$STAGING_DIR/skills/odoo-debugger" "$CLAUDE_SKILLS_DIR/"
        echo "  [OK] Skills deployed to $CLAUDE_SKILLS_DIR"
    fi

    # 3. Pre-allow the odoo-debug MCP tools in workspace settings so the subagent
    #    doesn't prompt on every tool call (mirrors the frictionless Antigravity UX).
    #    Merge-safe: preserve any existing settings.local.json.
    echo "Pre-allowing odoo-debug tools in workspace settings..."
    python3 -c "
import os, json
settings_dir = '/source/.claude'
os.makedirs(settings_dir, exist_ok=True)
path = os.path.join(settings_dir, 'settings.local.json')
settings = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            settings = json.load(f)
    except Exception:
        settings = {}
perms = settings.setdefault('permissions', {})
allow = perms.setdefault('allow', [])
if 'mcp__odoo-debug__*' not in allow:
    allow.append('mcp__odoo-debug__*')
with open(path, 'w') as f:
    json.dump(settings, f, indent=2)
"
    echo "  [OK] Workspace permissions updated."

    # 4. Auto-register the MCP server at user scope (auto-trusted; persists in the
    #    dedicated CLAUDE_CONFIG_DIR so no per-launch trust prompt is needed). Idempotent.
    echo "Configuring odoo-debug MCP server..."
    claude mcp remove --scope user odoo-debug >/dev/null 2>&1 || true
    if claude mcp add --scope user odoo-debug -- /usr/bin/python3 "$STAGING_DIR/debug_server.py"; then
        echo "  [OK] MCP server registered at user scope."
    else
        echo "  [WARN] 'claude mcp add' failed; you may need to register odoo-debug manually."
    fi
}

case "$AGENT_BACKEND" in
    claude)
        deploy_claude
        ;;
    antigravity)
        deploy_antigravity
        ;;
    *)
        echo "  [WARN] Unknown AGENT_BACKEND '$AGENT_BACKEND'; defaulting to antigravity."
        deploy_antigravity
        ;;
esac

# Check for previous crash logs
if [ -f "/tmp/odoo_mcp_error.log" ]; then
    echo "--- !!! PREVIOUS MCP CRASH DETECTED !!! ---"
    cat "/tmp/odoo_mcp_error.log"
    echo "------------------------------------------"
fi

echo "--- Setup Diagnostics ---"
echo "Current user: $(whoami) (UID: $(id -u))"
echo "--------------------------"
echo ""

# Execute the passed command
exec "$@"
