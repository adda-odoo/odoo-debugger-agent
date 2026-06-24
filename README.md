# Odoo Debugger Agent Sandbox

This folder contains a fully self-contained Odoo debugging environment. It packages a specialized **Subagent**, **Skills**, and an **MCP Server** into a secure Docker sandbox, and can be driven by either of two interchangeable agent backends — **Antigravity CLI** (`agy`) or **Claude Code** (`claude`).

## 🤖 Choosing a Backend

The same sandbox image ships both CLIs. Pick one at launch by passing it to `start.sh`:

```bash
./odoo-debugger-agent/start.sh antigravity   # Google Antigravity CLI (default)
./odoo-debugger-agent/start.sh claude         # Claude Code
```

The debugging engine (the `odoo-debug` MCP server, skills, and obfuscation tool) is identical for both; only the agent runtime and its auth differ. See the auth setup for your chosen backend below.

## 🚀 Features
- **Isolated Environment:** The agent runs in a Docker container with restricted access to your Odoo workspace.
- **Expert Subagent:** The `@odoo-debugger` agent is pre-configured with Odoo-specific diagnostic workflows.
- **Deep Inspection:** Built-in DAP (Debug Adapter Protocol) support for inspecting variables, call stacks, and evaluating expressions in a paused Odoo process. *(Note: DAP features are currently experimental, lack advanced stepping features, and may be buggy).*
- **Performance Tools:** Integrated `py-spy` for flamegraphs and `memray` for memory profiling.
- **Database Utilities:** Tools for fetching SaaS databases, obfuscating PII, and running SQL/ORM queries.

## 📂 Structure
- `Dockerfile`: Builds the sandbox image with all dependencies.
- `agents/`: Contains the `@odoo-debugger` subagent definition.
- `skills/`: Contains the `odoo-debugger` skill (best practices & documentation).
- `mcp_servers/`: Contains the `debug_server.py` MCP implementation.
- `odoo_obfuscate/`: Contains the database obfuscation tool

## ☁️ Antigravity Backend: Google Cloud Project Configuration

*(Skip this section if you are using the Claude backend.)*

Before running the sandbox with the **antigravity** backend, you must configure your Google Cloud Project to grant the CLI access and make the Project ID available in your local terminal environment:

1. **Create a Project:** Visit the [Google Cloud Console](https://console.cloud.google.com/) and create a new project. Make a note of your **Project ID** (not the project name).
2. **Locate Admin Settings:** Use the top search bar in the Google Cloud Console to search for and select **Admin for Gemini** (now Antigravity).
3. **Enable the Service:** Click to enable **Gemini Cloud Assist** (Antigravity) for your project.
4. **Configure Settings:** Navigate to the settings page and follow the on-screen prompts to enable Gemini/Antigravity for Google Cloud (this activates the necessary backend APIs).
5. **Enable Preview Models:** Within the settings page, locate the release channel options and switch the channel to **Preview**. Save your changes.

### 6. Set the Environment Variable (Required)
Make your project configuration persistent by adding your Project ID to your host machine's shell profile. Add the following line to your `~/.bashrc` or `~/.zshrc` file:

```bash
export GOOGLE_CLOUD_PROJECT="your_project_id"
```
*(Note: Run `source ~/.bashrc` or `source ~/.zshrc` or restart your terminal to apply the changes).*

## 🔑 Claude Backend: Authentication

*(Skip this section if you are using the Antigravity backend.)*

No environment variable is required. On your **first** `./start.sh claude` run, Claude Code will prompt you to log in interactively (Claude Pro/Max subscription or Console). Your login is then persisted on the host and reused automatically on every subsequent run — you do **not** log in again each time.

Persistence uses a **dedicated** config directory (`~/.odoo-debugger-claude` on the host, exposed inside the container via `CLAUDE_CONFIG_DIR`). This deliberately keeps the sandbox separate from your **real** `~/.claude` config, so the container's MCP registration never pollutes your host Claude setup. `start.sh` creates the directory if needed.

The `odoo-debugger` subagent, its skill, and the `odoo-debug` MCP server are auto-installed (and auto-trusted, with the MCP tools pre-allowed) on every launch — no manual setup.

#### First-run checklist (verify it works)
1. `./odoo-debugger-agent/start.sh claude` → complete the interactive login when prompted.
2. If asked to trust the `/source` workspace folder, accept it once.
3. Type `/agents` — you should see `odoo-debugger` listed; `/mcp` should show `odoo-debug` connected.
4. Exit, then run `./odoo-debugger-agent/start.sh claude` again — it should **not** ask you to log in again.
5. Give it a task (e.g. *"use the odoo-debugger agent to inspect res_partner in db X"*) and confirm it calls the `mcp__odoo-debug__*` tools.

## 🛠️ Setup & Installation

The sandbox uses **Docker Compose** to manage its environment and mounts.

Use the provided `start.sh` script to start the interactive CLI inside the sandbox, passing your chosen backend (`antigravity` or `claude`; defaults to `antigravity`).

This script automatically:
- **Validates** that it is being run alongside the `odoo` and `enterprise` directories.
- **Prevents permission issues** by safely managing ignored subfolders (like `internal`).
- Passes the relevant config into the container (`GOOGLE_CLOUD_PROJECT` for antigravity).
- Mounts your local source directory to `/source`.
- **Persists credentials** by mounting a config directory (`~/.gemini` for antigravity; the dedicated `~/.odoo-debugger-claude` for claude).
- **Exposes PostgreSQL** via Unix socket (recommended for Linux).
- Builds the image if it does not exist.

Run these commands **from your source directory** (the directory containing the `odoo` and `enterprise` repos):

```bash
chmod +x ./odoo-debugger-agent/start.sh   # make executable (only needed for the first time)
./odoo-debugger-agent/start.sh claude      # or: ./odoo-debugger-agent/start.sh antigravity
```

*Note: On first launch, the container automatically registers the Odoo MCP tools and deploys the agent/skill into the mounted config, so they persist across runs.*

## 🐘 Database Configuration

### Custom DB Settings
If your database is not accessible via the default Unix socket or uses a different user, you can override settings using environment variables:
- `DB_HOST`: Defaults to `/var/run/postgresql` (set to `localhost` with `network_mode: "host"` in `docker-compose.yml` for TCP)
- `DB_USER`: Defaults to `odoo`
- `DB_PORT`: Defaults to `5432`
- `DB_PASSWORD`: Defaults to empty string

Example for TCP connection:
```bash
DB_HOST=localhost ./odoo-debugger-agent/start.sh
```

## 📖 Usage

### Invoking the Agent
Once inside the sandbox, hand the specialized debugger a high-level diagnostic task.

- **Antigravity:** call the agent explicitly with the `@` syntax:
  ```
  @odoo-debugger tell me why the sales order SO1029 has no invoices attached to it
  ```
- **Claude:** just describe the task; Claude delegates to the `odoo-debugger` subagent automatically. You can also nudge it explicitly, e.g. *"use the odoo-debugger agent to find out why SO1029 has no invoices."*

### Key Workflows
The agent is designed to act autonomously based on your symptoms or questions. You do not need to micromanage its tools. Here are examples of how to direct it:

- **Investigating Data & Workflows:**
  - *"@odoo-debugger investigate why the hr.contract workflow is stuck in 'draft' for employee John Doe."*
  - *"@odoo-debugger look at database 'my_db' and tell me what fields are currently set on product.template ID 45."*

- **Triggering Deep Debugging (DAP):**
  - *"@odoo-debugger attach to 'my_db' and set a breakpoint where the invoice gets confirmed. Run the server and wait for me to trigger it."*
  - *(Once paused)* *"@odoo-debugger inspect the local variables to see why the total is calculating as zero."*

- **Diagnosing Performance Bottlenecks:**
  - *"@odoo-debugger the server is hanging when I confirm a large order. Please generate a CPU flamegraph for 30 seconds so we can see the hot paths."*
  - *"@odoo-debugger we have a memory leak. Attach memray, let it run for 60 seconds, and generate an HTML report."*

## ⚠️ Security Notes
- **Obfuscation:** The agent is trained to prioritize database obfuscation. Always call `obfuscate_database` before allowing the agent to query or inspect sensitive data.
- **Network:** The sandbox has internet access to reach Odoo documentation and SaaS fetch endpoints.
