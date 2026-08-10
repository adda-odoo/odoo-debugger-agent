# Use a recent Ubuntu LTS as a base image
FROM ubuntu:22.04

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
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel

# Add NodeSource repository for Node.js 22
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list

# Install Node.js and npm
RUN apt-get update && apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Add the PGDG (PostgreSQL Global Development Group) apt repository — Ubuntu
# 22.04's default repos only ship an old postgresql-client, and it must match
# the dedicated PostgreSQL 18.4 cluster this sandbox connects to.
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" | tee /etc/apt/sources.list.d/pgdg.list

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
RUN pip3 install --no-cache-dir \
    "mcp<2" \
    requests \
    psycopg2-binary \
    debugpy \
    py-spy \
    memray

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash debugger

# Set up staging directories for assets
RUN mkdir -p /opt/odoo-debug/skills

# Copy assets to staging area (to avoid shadowing by volume mounts)
COPY agents/odoo-debugger/odoo-debugger.md /opt/odoo-debug/agents/odoo-debugger/odoo-debugger.md
COPY skills/odoo-debugger /opt/odoo-debug/skills/odoo-debugger
COPY mcp_servers/debug_server.py /opt/odoo-debug/
# scrub_database's clean/dirty gate (same structural diff copy_and_scrub_db.sh
# uses on the host, generalized with --dst-db for the same-cluster case here).
COPY scripts/verify_scrub_diff.py /opt/odoo-debug/
COPY scrubdb-poc/tools/scrubdb /opt/odoo-debug/scrubdb

# Install scrubdb — the anonymizer that gates all database access below
RUN pip3 install --no-cache-dir "/opt/odoo-debug/scrubdb[yaml]"

# Add entrypoint script
COPY entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

# Fix permissions for staging area and user home
RUN chown -R debugger:debugger /home/debugger /opt/odoo-debug

# Define the default internal source directory where the user will mount their repos
ENV ODOO_SRC_DIR=/source
RUN mkdir -p /source && chown debugger:debugger /source

# Stay as root here: the host's bind-mounted volumes (~/.odoo-debugger-claude,
# ../:/source) are usually owned by whatever UID the host user has, which
# rarely matches this image's fixed `debugger` UID (1000). entrypoint.sh
# chowns the specific mount points it needs at container start, then drops
# to `debugger` via setpriv before running anything else. Never run the
# actual Claude Code session as root.
WORKDIR /source

# Set the entrypoint to our setup script
ENTRYPOINT ["entrypoint.sh"]

# Default command starts the interactive Claude Code CLI
CMD ["claude"]
