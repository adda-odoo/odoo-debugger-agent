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

# Add NodeSource repository for Node.js 22
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list

# Install Node.js and npm
RUN apt-get update && apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Gemini CLI globally
RUN npm install -g @google/gemini-cli

# Install Python dependencies for the Odoo MCP server and debugging
RUN pip3 install --no-cache-dir \
    mcp \
    requests \
    psycopg2-binary \
    debugpy \
    py-spy \
    memray

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash gemini

# Set up directories for agents and skills in the user's home
RUN mkdir -p /home/gemini/.gemini/agents \
    && mkdir -p /home/gemini/.agents/skills \
    && mkdir -p /opt/odoo-debug

# Copy the agent definition, skills, and MCP server into the image
# (Paths are relative to the odoo-debugger-agent build context)
COPY agents/odoo-debugger.md /home/gemini/.gemini/agents/
COPY skills/odoo-debugger /home/gemini/.agents/skills/odoo-debugger
COPY mcp_servers/debug_server.py /opt/odoo-debug/

# Create a global MCP config that points to the internal path
RUN echo '{\n\
  "mcpServers": {\n\
    "odoo-debug": {\n\
      "type": "stdio",\n\
      "command": "python3",\n\
      "args": ["/opt/odoo-debug/debug_server.py"]\n\
    }\n\
  }\n\
}' > /home/gemini/.mcp.json

# Fix permissions
RUN chown -R gemini:gemini /home/gemini /opt/odoo-debug

# Define the default internal source directory where the user will mount their repos
ENV ODOO_SRC_DIR=/source
RUN mkdir -p /source && chown gemini:gemini /source

# Switch to the non-root user
USER gemini

# Set the working directory
WORKDIR /source

# Set the entrypoint to be gemini cli.
ENTRYPOINT ["gemini"]

# Default command to start the interactive chat
CMD []
