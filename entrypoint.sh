#!/bin/bash
# entrypoint.sh - Automated setup for Odoo Debugger Agent (Claude Code backend)

echo "--- Odoo Debugger Environment Setup ---"

# Ensure we have a valid HOME
export HOME=/home/debugger
STAGING_DIR="/opt/odoo-debug"

# The container starts as root because Docker auto-creates a missing
# bind-mount source as root:root, which would otherwise leave it unwritable
# by `debugger` forever. (The image builds `debugger` with the host user's
# own UID/GID — see the Dockerfile's HOST_UID arg — so the repos under
# /source are already writable; these chowns cover the config/.claude mount
# points, which may not exist on the host yet.) Chown just those two, then
# drop to `debugger` for everything else. The actual Claude Code session
# must never run as root.
if [ "$(id -u)" = "0" ]; then
    CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude-config}"
    mkdir -p "$CONFIG_DIR"
    chown -R debugger:debugger "$CONFIG_DIR"
    mkdir -p /source/.claude
    chown -R debugger:debugger /source/.claude

    # odoo-bin's own shebang (`#!/usr/bin/env python3`) resolves to this
    # image's system python3, which starts out with only the MCP server's
    # own deps installed, not Odoo's (werkzeug, babel, lxml, gevent, ...) —
    # start_odoo crashes immediately, before Odoo's own logging even
    # initializes, without this. debian/control (Odoo's own
    # dependency manifest, read by setup/debinstall.sh below) only exists
    # once /source is mounted at container start, not at image build time,
    # so this can't be baked into the Dockerfile — it has to run here, every
    # session. Using Odoo's own debinstall.sh (apt packages) rather than
    # `pip install -r requirements.txt` matters: the pip-pinned equivalent of
    # some of these (gevent in particular) has no prebuilt wheel for this
    # Python/platform and fails compiling from source, where the apt package
    # is prebuilt for this exact Ubuntu release — this is the same install
    # path Odoo's own .deb packaging uses.
    if [ -f /source/odoo/setup/debinstall.sh ]; then
        sh /source/odoo/setup/debinstall.sh -q
    else
        echo "  [WARN] /source/odoo/setup/debinstall.sh not found — skipping Odoo dependency install."
    fi

    # Carry CAP_SYS_PTRACE into the debugger user's ambient set so it survives
    # the drop from root and is inherited by the MCP server and its profiling
    # children (py-spy, gdb/memray). docker-compose's cap_add only puts the
    # capability in the bounding set, which a non-root process can't use on its
    # own — without this, py-spy/memray attach to the sibling odoo-bin fail with
    # "Permission Denied" under the host's Yama ptrace_scope.
    #
    # Only when the capability is actually present (CapPrm bit 19 = 0x80000),
    # so running the image without cap_add degrades to "profiling tools fail"
    # rather than "setpriv errors and the container never starts".
    PTRACE_CAPS=""
    cap_prm="$(awk '/^CapPrm:/ {print $2}' /proc/self/status)"
    if [ -n "$cap_prm" ] && [ "$(( 0x$cap_prm & 0x80000 ))" -ne 0 ]; then
        PTRACE_CAPS="--inh-caps +sys_ptrace --ambient-caps +sys_ptrace"
    else
        echo "  [WARN] CAP_SYS_PTRACE not held — py-spy/memray profiling will be unavailable."
    fi
    exec setpriv --reuid=debugger --regid=debugger --init-groups $PTRACE_CAPS "$0" "$@"
fi

# The odoo/enterprise checkouts under /source are bind-mounted from the host
# and keep the host user's ownership (unlike the CLAUDE_CONFIG_DIR/.claude
# mounts above, chowning these on every start would be slow and would flip
# ownership away from the host user for no benefit). Git's ownership check
# then flags them as "dubious" for the debugger user — trust everything
# under /source instead, since that's the whole point of this sandbox.
git config --global --add safe.directory '*'

# The handoff skill writes its documents here. /source is the bind-mounted
# working directory, so these files land on the host and survive the --rm
# container. Created as debugger (not in the root block above) so it stays
# writable: root-owned paths on this bind mount lock the host user out.
mkdir -p /source/handoff

# PostgreSQL connection (standard libpq env vars). debug_server.py, the
# oe-support restore pipeline (createdb/dropdb/pg_restore), and the scrubdb
# CLI all resolve their Postgres target from these — set once, here, so
# every code path stays uniformly pointed at the dedicated cluster. Fixed,
# not user-configurable: the isolation guarantee comes from the host's
# ufw/pg_hba scoping to this exact port, not from hiding it.
export PGHOST="host.docker.internal"
export PGPORT="5433"
export PGUSER="odoo"
export PGPASSWORD="odoo"

deploy_claude() {
    # Claude Code reads project config from the working directory (/source).
    CLAUDE_PROJECT_DIR="/source/.claude"
    CLAUDE_AGENTS_DIR="$CLAUDE_PROJECT_DIR/agents"
    CLAUDE_SKILLS_DIR="$CLAUDE_PROJECT_DIR/skills"

    # 1. Deploy the subagent (flat markdown file).
    echo "Deploying odoo-debugger subagent..."
    if [ -f "$STAGING_DIR/agents/odoo-debugger/odoo-debugger.md" ]; then
        if mkdir -p "$CLAUDE_AGENTS_DIR" && cp "$STAGING_DIR/agents/odoo-debugger/odoo-debugger.md" "$CLAUDE_AGENTS_DIR/odoo-debugger.md"; then
            echo "  [OK] Subagent deployed to $CLAUDE_AGENTS_DIR"
        else
            echo "  [WARN] Failed to deploy subagent to $CLAUDE_AGENTS_DIR (check mount permissions)"
        fi
    fi

    # 2. Deploy Skills (idempotent overwrite so they are always current).
    #    Every directory staged under skills/ is deployed, so adding a skill to
    #    the repo needs no change here or in the Dockerfile.
    echo "Deploying Odoo debugger skills..."
    mkdir -p "$CLAUDE_SKILLS_DIR"
    for skill_dir in "$STAGING_DIR"/skills/*/; do
        [ -d "$skill_dir" ] || continue
        skill_name="$(basename "$skill_dir")"
        if cp -r "$skill_dir" "$CLAUDE_SKILLS_DIR/"; then
            echo "  [OK] Skill '$skill_name' deployed to $CLAUDE_SKILLS_DIR"
        else
            echo "  [WARN] Failed to deploy skill '$skill_name' (check mount permissions)"
        fi
    done

    # 3. Pre-allow the odoo-debug MCP tools in workspace settings so the subagent
    #    doesn't prompt on every tool call. Merge-safe: preserve any existing
    #    settings.local.json.
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
    if [ $? -eq 0 ]; then
        echo "  [OK] Workspace permissions updated."
    else
        echo "  [WARN] Failed to update workspace permissions (check mount permissions)"
    fi

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

deploy_claude

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
