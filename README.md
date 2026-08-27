# Odoo Debugger Agent Sandbox

## 👋 Introduction

This contains a fully self-contained Odoo debugging environment. It packages a specialized **Subagent**, **Skills**, and an **MCP Server** into a secure Docker sandbox, driven by the **Claude Code** CLI (`claude`).

### Features
- **Isolated Environment:** The agent runs in a Docker container with restricted access to your Odoo workspace.
- **Expert Subagent:** The `odoo-debugger` agent is pre-configured with Odoo-specific diagnostic workflows, focused on root-cause analysis through data, logs, profiling, and browser reproduction rather than a live step-through debugger.
- **Performance Tools:** Integrated `py-spy` for flamegraphs and `memray` for memory profiling.
- **Browser Reproduction:** Headless Chromium (Playwright) driving the Odoo web client in-container, so view-layer bugs — Owl crashes, failed RPCs, broken asset bundles — can be reproduced and read directly instead of inferred from SQL.
- **Database Utilities:** Tools for scrubbing PII and running SQL/ORM queries.

### Structure
- `Dockerfile`: Builds the sandbox image with all dependencies.
- `agents/`: Contains the `odoo-debugger` subagent definition.
- `skills/`: Contains the agent's skills — `odoo-debugger` (best practices & documentation) and `odoo-ui-repro` (reproducing bugs through the web client). Every directory here is deployed automatically.
- `mcp_servers/`: Contains the `debug_server.py` MCP implementation.
- `scrubdb-poc/tools/scrubdb/`: The scrubdb anonymizer — gates all database access (see its own README for details).

## 🛠️ Setup

The container never touches your host's main PostgreSQL cluster directly. Instead it reaches out to a dedicated **`ai-dev`** cluster on the host, and Docker Compose wires up everything else (mounts, credentials, env vars) around that connection. Set up both pieces once before your first run.

### Cluster Setup
`start.sh` and the container both expect `ai-dev` to already exist. `ai-dev` is a second, standalone PostgreSQL cluster on the host, kept separate from your regular `main` cluster so the sandbox never shares a login path with it. The install and role-creation steps below follow the same pattern as [Odoo's own source-install docs](https://www.odoo.com/documentation/19.0/administration/on_premise/source.html#postgresql), adapted for a dedicated cluster that's reachable only from the Docker bridge network rather than the local Unix socket.

1. Install PostgreSQL 18 (Ubuntu's own repos only carry older majors, so add the [PGDG apt repo](https://www.postgresql.org/download/linux/ubuntu/) first):
   ```bash
   sudo apt install postgresql-common
   sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
   sudo apt install postgresql-18
   ```
2. Create the `ai-dev` cluster on its own port, alongside `main`:
   ```bash
   sudo pg_createcluster 18 ai-dev --port=5433
   ```
3. Let it accept TCP connections (the default only allows the local Unix socket), and scope password auth to the Docker bridge subnet only:
   ```bash
   sudo sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" /etc/postgresql/18/ai-dev/postgresql.conf
   echo "host    all             all             172.17.0.0/16           scram-sha-256" | sudo tee -a /etc/postgresql/18/ai-dev/pg_hba.conf
   sudo pg_ctlcluster 18 ai-dev restart
   ```
4. Create the `odoo` role. Odoo's docs forbid connecting as the default `postgres` superuser; here it also needs a password (not a Unix-peer match), since the container connects over TCP as a distinct, non-superuser role:
   ```bash
   sudo -u postgres psql -p 5433 -c "CREATE ROLE odoo WITH LOGIN CREATEDB PASSWORD 'odoo';"
   ```
5. Open the firewall to that same subnet only (`172.17.0.0/16` is Docker's default bridge network — matches `network_mode: bridge` in `docker-compose.yml`):
   ```bash
   sudo ufw allow from 172.17.0.0/16 to any port 5433 proto tcp comment 'ai-dev postgres cluster: docker bridge only'
   ```

Verify with `pg_lsclusters` — you should see both `main` (port 5432) and `ai-dev` (port 5433) listed as `online`.

The sandbox never touches your host's main PostgreSQL cluster. The container reaches the host's `ai-dev` cluster via `host.docker.internal`, and `ufw`/`pg_hba.conf` on the host restrict that path to the Docker bridge network only — no other host cluster is reachable this way. Any database on `ai-dev` stays inaccessible to every database-touching tool until it's confirmed anonymized (see Security Notes below); the in-container `scrub_database` MCP tool is a fallback for a database that reached `ai-dev` without going through the host-side pipeline described below.

### Docker Compose
The sandbox uses **Docker Compose** to manage its environment and mounts — bind-mounting your source directory, persisting credentials, and building the image on first run. `start.sh` is the driver for all of it; see **Starting Up the Container** below for actually invoking it.

**Database copy/scrub prerequisites (host-side, one-time):** the database copy step (`scripts/copy_and_scrub_db.sh`, invoked by `start.sh`) runs on the **host**, since the container can't reach the `main` cluster. It needs `scrubdb` installed on the host once (`pipx` — plain `pip install` is blocked on newer Debian/Ubuntu), plus `python3-psycopg2` for the structural clean/dirty check (`scripts/verify_scrub_diff.py`) that compares the `main` original against the `ai-dev` copy directly, rather than a pattern-based PII scan:
```bash
sudo apt install pipx python3-psycopg2   # if not already installed
pipx install "./odoo-debugger-agent/scrubdb-poc/tools/scrubdb[yaml]"
```

**DB connection settings:** the `ai-dev` connection details are fixed, not user-configurable: host `host.docker.internal`, port `5433`, role `odoo` / `odoo`. The isolation guarantee comes from the host's `ufw`/`pg_hba` scoping to that exact port, not from hiding any of these values.

**Authentication:** no environment variable is required. On your **first** `./start.sh` run, Claude Code will prompt you to log in interactively (Claude Pro/Max subscription or Console). Your login is then persisted on the host and reused automatically on every subsequent run — you do **not** log in again each time. Persistence uses a **dedicated** config directory (`~/.odoo-debugger-claude` on the host, exposed inside the container via `CLAUDE_CONFIG_DIR`). This deliberately keeps the sandbox separate from your **real** `~/.claude` config, so the container's MCP registration never pollutes your host Claude setup. `start.sh` creates the directory if needed. The `odoo-debugger` subagent, its skills, and the `odoo-debug` MCP server are auto-installed (and auto-trusted, with the MCP tools pre-allowed) on every launch — no manual setup.

**Lifecycle:** `start.sh` uses `docker compose run --rm sandbox`, which tears down the one-off `sandbox` container on exit. Databases copied into `ai-dev` persist across runs on the host — drop them manually via `psql`/`dropdb` if you want to force a fresh copy+scrub next time.

**Fields to leave unscrubbed:** `scripts/scrub_allowlist.txt` lists columns that `copy_and_scrub_db.sh` should leave untouched — passed straight to `scrubdb` as `--exclude-file` (one glob per line, `#` comments allowed, matches either a bare column name across all tables or an exact `table.column`). Since the structural clean/dirty check (`verify_scrub_diff.py`) only requires columns `scrubdb` actually touched to differ from `main`, anything listed here is automatically exempt from that check too — no changes needed there when you add an entry.

**Custom scrub rules per database:** `copy_and_scrub_db.sh` always passes the shared `scrubdb-poc/tools/scrubdb/rules/odoo_common.yml` ruleset to `scrubdb` via `--ruleset`, overriding the transform it would otherwise pick for specific columns (see that file's own comments). If `scripts/scrub_rules/<db_name>.yml` exists, it's merged on top of the shared ruleset — table/column by table/column — before scrubbing `<db_name>`, so a database with its own custom fields or unusual constraints (e.g. a partial unique index the generic PK/FK/unique-constraint introspection can't see) can get a targeted fix without editing the shared file. See `scripts/scrub_rules/README.md` for the format. `copy_and_scrub_db.sh` only scrubs a database once on first copy, so `dropdb` it from `ai-dev` before retrying `start.sh <db_name>` after adding or changing rules.

## ▶️ Starting Up the Container

Run these commands **from your source directory** (the directory containing the `odoo` and `enterprise` repos):

```bash
chmod +x ./odoo-debugger-agent/start.sh   # make executable (only needed for the first time)
./odoo-debugger-agent/start.sh             # launch with whatever's already on ai-dev
./odoo-debugger-agent/start.sh my_database # launch, ensuring my_database is present + scrubbed first
```

Pass a database name to have it ready inside the container automatically: if
that database isn't already present (and scrubbed) on `ai-dev`, `start.sh`
copies it over from your host's main cluster and runs `scrubdb` on it first —
see **Setup** above. Omit the argument to just launch with whatever's already
on `ai-dev`.

Either way, the database itself only ends up *present* on `ai-dev` — Odoo isn't
running yet. Once inside the sandbox, ask the agent to start it against that
database, e.g. *"start Odoo on my_database"* (it calls the `start_odoo` MCP
tool, which takes the database name as an argument).

`start.sh` also validates that it's being run alongside the `odoo` and `enterprise` directories, and safely manages ignored subfolders (like `internal`). It builds the image on first run if it doesn't exist yet. On first launch, the container automatically registers the Odoo MCP tools and deploys the agent/skill into the mounted config, so they persist across runs.

### First-run checklist (verify it works)
1. `./odoo-debugger-agent/start.sh` → complete the interactive login when prompted.
2. If asked to trust the `/source` workspace folder, accept it once.
3. Type `/agents` — you should see `odoo-debugger` listed; `/mcp` should show `odoo-debug` connected, with the `browser_*` tools among its tools.
4. Exit, then run `./odoo-debugger-agent/start.sh` again — it should **not** ask you to log in again.
5. Give it a task (e.g. *"use the odoo-debugger agent to inspect res_partner in db X"*) and confirm it calls the `mcp__odoo-debug__*` tools.

## 📖 Usage

### Invoking the Agent
Once inside the sandbox, hand the specialized debugger a high-level diagnostic task. Just describe the task; Claude delegates to the `odoo-debugger` subagent automatically. You can also nudge it explicitly, e.g. *"use the odoo-debugger agent to find out why SO1029 has no invoices."*

### Key Workflows
The agent is designed to act autonomously based on your symptoms or questions. You do not need to micromanage its tools. Here are examples of how to direct it:

- **Investigating Data & Workflows:**
  - *"Investigate why the hr.contract workflow is stuck in 'draft' for employee John Doe."*
  - *"Look at database 'my_db' and tell me what fields are currently set on product.template ID 45."*

- **Diagnosing Performance Bottlenecks:**
  - *"The server is hanging when I confirm a large order. Please generate a CPU flamegraph for 30 seconds so we can see the hot paths."*
  - *"We have a memory leak. Attach memray, let it run for 60 seconds, and generate an HTML report."*

- **Reproducing in the Browser:**
  - *"Open SO1029 in the web client and tell me what happens when you click Confirm — I get a spinner that never resolves."*
  - *"The Contacts kanban is blank for this database. Load it in the browser and show me the console and network errors."*

## ⚠️ Security Notes
- **Cluster isolation:** The only PostgreSQL reachable from inside the container is the host's `ai-dev` cluster — enforced by `ufw` (allowing the Docker bridge subnet through on `ai-dev`'s port only) and `pg_hba.conf` (scoping password auth on that port to the same subnet). The container's `network_mode: bridge` pins it to that exact subnet. The host's main Postgres cluster has no matching firewall rule and stays unreachable from inside the container.
- **Anonymization:** The agent is trained to prioritize database anonymization. Databases normally arrive on `ai-dev` already scrubbed by the host-side pipeline before the container starts; `scrub_database` is the in-container fallback for one that hasn't been. Every database-touching tool checks for a marker that's only written after a structural clean/dirty diff confirms every column scrubdb touched actually changed — a database that scrubdb applied to but didn't verify clean stays locked out.
- **Network:** The sandbox has internet access to reach Odoo documentation. The headless browser inherits that access — `browser_goto` accepts absolute URLs and is not restricted to the local Odoo server, so a page rendering scrubbed customer data can make outbound requests. Chromium also runs with `--no-sandbox` (the container has no `CAP_SYS_ADMIN`), so its isolation is the container's, not Chromium's.
- **Artifacts:** Flamegraphs, memray logs and browser screenshots are written to `/source/.debug-artifacts`, which is bind-mounted from the host directory above this repo — they survive the container exiting. Override with `DEBUG_ARTIFACTS_DIR`.
