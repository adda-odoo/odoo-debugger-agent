# Use a recent Ubuntu LTS as a base image — 24.04 ships Python 3.12 as the
# system default, which debug_server.py requires; the apt-installed Odoo
# deps below (via debinstall.sh, at container start) are pulled in for
# whatever python3 this base image ships, so they must move together.
FROM ubuntu:24.04

# Set non-interactive frontend to avoid prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies for Node.js, Python, and other tools
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    libpq-dev \
    gcc \
    python3-dev \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04's python3-pip marks the system interpreter "externally
# managed" (PEP 668) — pip refuses plain installs into it. This image's
# python3 is dedicated to the MCP server and debugging tools (nothing else
# depends on apt's own Python packages staying untouched), so overriding
# that guard is safe; every pip3 install below needs the same flag.
# --ignore-installed: apt's pip/setuptools/wheel have no RECORD file for
# pip's own uninstall step to read, so a plain --upgrade fails trying to
# remove them first ("Cannot uninstall pip 24.0 ... installed by debian").
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed --upgrade pip setuptools wheel

# Add NodeSource repository for Node.js 22
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list

# Install Node.js and npm
RUN apt-get update && apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Add the PGDG (PostgreSQL Global Development Group) apt repository — Ubuntu's
# default repos only ship an old postgresql-client, and it must match
# the dedicated PostgreSQL 18.4 cluster this sandbox connects to.
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" | tee /etc/apt/sources.list.d/pgdg.list

# Install PostgreSQL 18 client tools (pg_dump, createdb, pg_restore, dropdb) —
# required by debug_server.py's scrub_database, which snapshots a database to
# a same-cluster temp copy before scrubbing so it can verify the result (see
# verify_scrub_diff.py). psql ships in the same package for interactive use;
# neither is needed for psycopg2's own connections, which talk the wire
# protocol directly.
RUN apt-get update && apt-get install -y postgresql-client-18 && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies for the Odoo MCP server and debugging.
# mcp is pinned below 2.0: debug_server.py uses the FastMCP high-level API
# (mcp.server.fastmcp.FastMCP), which mcp 2.0.0 restructured/removed.
RUN pip3 install --no-cache-dir --break-system-packages \
    "mcp<2" \
    requests \
    psycopg2-binary \
    py-spy \
    memray \
    "playwright>=1.49"

# Headless Chromium for the browser_* tools (debug_server.py), which drive the
# Odoo web client at localhost:8069 to reproduce view-layer bugs. 1.49 is the
# floor for Locator.aria_snapshot(), the tools' primary output format.
#
# PLAYWRIGHT_BROWSERS_PATH puts the browser in a shared, world-readable path
# instead of the default ~/.cache/ms-playwright: `playwright install` runs as
# root here, the session runs as `debugger`, and the container is started with
# --rm — a per-user cache would be unreadable at runtime AND re-downloaded
# every single session. --with-deps apt-installs Chromium's shared libraries
# and needs root, which is why this sits above the useradd below.
#
# The symlink is for Odoo's own JS tours: HttpCase.browser_js probes PATH for
# 'google-chrome', 'chromium', 'chromium-browser' and 'google-chrome-stable'
# (odoo/tests/common.py), and skips the test when it finds none. Reusing the
# browser Playwright already downloaded makes `odoo-bin --test-enable` tours
# runnable in this container for no extra bytes.
#
# Cost: this layer is ~1GB (656MB of browser payloads — Chrome for Testing,
# the headless shell Playwright picks for headless launches, and ffmpeg —
# plus the shared libraries --with-deps pulls in), taking the image from
# ~1.9GB to ~3.6GB. Cached after the first build.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python3 -m playwright install --with-deps chromium && \
    chmod -R a+rX /opt/ms-playwright && \
    ln -sf "$(find /opt/ms-playwright -maxdepth 3 -type f \( -name chrome -o -name headless_shell \) | sort | head -n1)" /usr/local/bin/chromium && \
    chromium --version && \
    rm -rf /var/lib/apt/lists/*

# gdb is how `memray attach` (memray_attach tool) injects the tracker into the
# running Odoo process; without it memray fails with "Cannot find a supported
# lldb or gdb executable" (this image's Python 3.12 has no sys.remote_exec
# fallback — that landed in 3.14). Its own late layer keeps the browser layer
# above cached when this is added.
RUN apt-get update && apt-get install -y gdb && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security.
#
# Its UID/GID must MATCH the host user who owns the bind-mounted repos, or the
# container can only read /source, never write it: those files keep their host
# ownership (entrypoint.sh deliberately doesn't chown the whole tree), and the
# repos are mode 775, so a non-matching UID lands in "other" and gets r-x.
# start.sh passes the invoking user's real ids via docker-compose.yml; the
# 1000 defaults only apply to a bare `docker build`.
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN set -eu; \
    # ubuntu:24.04 ships a default 'ubuntu' user at UID 1000 — evict whoever
    # holds the target id so it can be reused (no-op when the id is free).
    existing="$(getent passwd "$HOST_UID" | cut -d: -f1 || true)"; \
    if [ -n "$existing" ]; then userdel -r "$existing" 2>/dev/null || true; fi; \
    # Reuse a group that already owns the GID rather than failing on a clash.
    if ! getent group "$HOST_GID" >/dev/null; then groupadd -g "$HOST_GID" debugger; fi; \
    useradd --create-home --shell /bin/bash -u "$HOST_UID" -g "$HOST_GID" debugger

# Set up staging directories for assets
RUN mkdir -p /opt/odoo-debug/skills

# Copy assets to staging area (to avoid shadowing by volume mounts)
COPY agents/odoo-debugger/odoo-debugger.md /opt/odoo-debug/agents/odoo-debugger/odoo-debugger.md
# Whole directory, not one named skill: entrypoint.sh deploys every skill it
# finds here, so adding a skill needs no Dockerfile change.
COPY skills /opt/odoo-debug/skills
COPY mcp_servers/debug_server.py /opt/odoo-debug/
# scrub_database's clean/dirty gate (same structural diff copy_and_scrub_db.sh
# uses on the host, generalized with --dst-db for the same-cluster case here).
COPY scripts/verify_scrub_diff.py /opt/odoo-debug/
COPY scrubdb-poc/tools/scrubdb /opt/odoo-debug/scrubdb

# Install scrubdb — the anonymizer that gates all database access below
RUN pip3 install --no-cache-dir --break-system-packages "/opt/odoo-debug/scrubdb[yaml]"

# Add entrypoint script
COPY entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# NOTE: switch_odoo_branch used to shell out to a root-privileged helper
# (switch-odoo-branch.sh) via a sudoers rule, on the assumption that root
# "never touches ownership itself, so the host user's own access is never at
# risk". That assumption was wrong: `git checkout` recreates every file that
# differs between branches, and files created by root are owned by root. On
# this bind mount (no userns remapping, so container root == host root) one
# branch switch left ~30k paths in odoo and ~27k in enterprise owned by
# root:root and locked the host user out of their own repos.
#
# The helper and its sudoers rule are therefore deliberately NOT installed.
# `debugger` is built with the host user's UID above, so it can check out
# branches directly as itself — and if it ever can't, git fails loudly
# instead of silently rewriting ownership.

# Fix permissions for staging area and user home
RUN chown -R debugger:debugger /home/debugger /opt/odoo-debug

# Define the default internal source directory where the user will mount their repos
ENV ODOO_SRC_DIR=/source
RUN mkdir -p /source && chown debugger:debugger /source

# Stay as root here: Docker auto-creates a missing bind-mount source as
# root:root, so ~/.odoo-debugger-claude can arrive unwritable by `debugger`
# regardless of the id matching above. entrypoint.sh chowns the specific
# mount points it needs at container start, then drops to `debugger` via
# setpriv before running anything else. Never run the actual Claude Code
# session as root.
WORKDIR /source

# Set the entrypoint to our setup script
ENTRYPOINT ["entrypoint.sh"]

# Default command starts the interactive Claude Code CLI
CMD ["claude"]
