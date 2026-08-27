import asyncio
import collections
import subprocess
import signal
import os
import sys
import json
import time
import urllib.parse
from datetime import datetime
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from mcp.server.fastmcp import FastMCP
import configparser

# Playwright backs the browser_* tools at the bottom of this file. It is
# imported defensively: an image built before those tools existed has no
# playwright, and a hard ImportError here would take all sixteen of the other
# tools down with it. _require_playwright() turns the miss into the usual
# returned-string error instead.
try:
    from playwright.async_api import async_playwright
except ImportError as _playwright_import_error:
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = str(_playwright_import_error)
else:
    _PLAYWRIGHT_IMPORT_ERROR = ""

# 1. Read .oe-config and environment variables
config = configparser.ConfigParser()
oe_config_path = os.path.expanduser("~/.oe-config")

# Priority: Environment Variable > .oe-config > Default (/source)
src_dir = os.environ.get("ODOO_SRC_DIR", "/source")
custom_addons_list = []

if os.path.exists(oe_config_path):
    config.read(oe_config_path)
    if config.has_section("oe-support"):
        if config.has_option("oe-support", "src") and not os.environ.get("ODOO_SRC_DIR"):
            src_dir = os.path.expanduser(config.get("oe-support", "src"))
        if config.has_option("oe-support", "custom-addons"):
            addons_str = config.get("oe-support", "custom-addons")
            custom_addons_list = [a.strip() for a in addons_str.split(",") if a.strip()]

# 2. Dynamically build paths based on the source directory
ODOO_REPO = os.path.join(src_dir, "odoo")
ENTERPRISE_REPO = os.path.join(src_dir, "enterprise")
ODOO_BIN = os.path.join(ODOO_REPO, "odoo-bin")

# 3. Assemble the full addons path
addons_paths = [
    os.path.join(ODOO_REPO, "addons"),
    ENTERPRISE_REPO,
]
# Append any custom addons from .oe-config
for addon in custom_addons_list:
    addons_paths.append(os.path.join(src_dir, addon))

ODOO_ADDONS = ",".join(addons_paths)

# 4. Resolve internal script locations
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRUBDB_BIN = "scrubdb"
SCRUBDB_MARKER_KEY = "scrubdb.run_completed"
VERIFY_SCRUB_DIFF_SCRIPT = os.path.join("/opt/odoo-debug", "verify_scrub_diff.py")

LOG_FILE = "/tmp/odoo_server.log"
ODOO_URL = "http://localhost:8069"
# Where profiling output and browser screenshots are written — see _artifacts_dir.
ARTIFACTS_DIR = os.environ.get("DEBUG_ARTIFACTS_DIR", "/source/.debug-artifacts")
# Credentials the RPC tools authenticate with. These are the *post-scrub*
# values, not the ones the database shipped with: scrubdb's finalization
# rewrites every non-root login to user<id> (normalize_logins) and sets every
# non-root password to 'admin' (reset_passwords). Odoo's admin user is id 2,
# so it answers to 'user2' here — the original 'admin' login no longer exists
# and authenticating as it fails for every database this server can touch
# (access is gated on a confirmed scrub, so there is no unscrubbed case).
# Overridable for a database finalized with different options.
ODOO_LOGIN = os.environ.get("ODOO_LOGIN", "user2")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")

# entrypoint.sh apt-installs Odoo's own deps (debian/control, via
# setup/debinstall.sh) into /usr/lib/python3/dist-packages. Python's default
# sys.path puts /usr/local/lib/python3.10/dist-packages (pip installs) ahead
# of that, so a pip package pulled in for the MCP server itself — cryptography
# is one, transitively — silently shadows the apt version. pyOpenSSL (also
# apt-installed, imported by odoo/addons/base) is built against the apt
# cryptography's internal layout, so under the shadowed version it breaks
# with an unrelated-looking AttributeError deep inside module loading.
# Prepending this to PYTHONPATH for the odoo-bin child only restores apt's
# priority without touching what the MCP server process itself sees.
_ODOO_PYTHONPATH = "/usr/lib/python3/dist-packages"


def _odoo_subprocess_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = _ODOO_PYTHONPATH + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def _db_connect(database: str, readonly: bool = True):
    """Connect to `database` on the dedicated PostgreSQL cluster.

    Resolves the target purely from standard libpq env vars (PGHOST/PGPORT/
    PGUSER/PGPASSWORD), exported once in entrypoint.sh — nothing is hardcoded
    here, so this, the oe-support restore pipeline, and the scrubdb CLI all
    stay pointed at the same cluster automatically.
    """
    conn = psycopg2.connect(dbname=database)
    if readonly:
        conn.set_session(readonly=True)
    return conn


mcp = FastMCP("odoo-debug")

# All mutable runtime state lives here — no globals, no scattered declarations.
_state: dict = {
    "odoo_process": None,       # subprocess.Popen | None
    "http_sessions": {},        # dict[str, requests.Session] keyed by database
    "memray_pid": None,         # int | None — PID currently being tracked by memray attach
    "log_file": LOG_FILE,       # str — path passed via the most recent start_odoo call
    "browser": None,            # dict | None — the live Chromium session, see _launch_browser
    # Serializes the browser_* tools. They are async, so unlike every other
    # tool here they do NOT block the event loop, and FastMCP dispatches each
    # incoming request with tg.start_soon — two browser calls issued in one
    # turn would otherwise interleave on the same page. Constructing the Lock
    # at import is safe on 3.10+: it no longer binds to a loop until awaited.
    "browser_lock": asyncio.Lock(),
}


# ===========================================================================
# HTTP / JSON-2 helpers
# ===========================================================================

def _get_session(database: str) -> requests.Session:
    """Return an authenticated requests.Session for the given database.

    Authenticates via /web/session/authenticate on first call per database.
    The session_id cookie is retained by the Session object for all subsequent
    JSON-2 requests.
    """
    if database in _state["http_sessions"]:
        return _state["http_sessions"][database]

    session = requests.Session()
    resp = session.post(
        f"{ODOO_URL}/web/session/authenticate",
        json={
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"db": database, "login": ODOO_LOGIN, "password": ODOO_PASSWORD},
        },
    )
    resp.raise_for_status()
    result = resp.json().get("result", {})
    if not result.get("uid"):
        raise RuntimeError(
            f"Authentication failed for database '{database}'. "
            "Check ODOO_LOGIN / ODOO_PASSWORD and that the server is running."
        )
    _state["http_sessions"][database] = session
    return session


def _call_kw(database: str, model: str, method: str, args: list = None, kwargs: dict = None) -> object:
    """Call an Odoo ORM method via /web/dataset/call_kw (JSON-RPC)."""
    session = _get_session(database)
    resp = session.post(
        f"{ODOO_URL}/web/dataset/call_kw",
        json={
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "model": model,
                "method": method,
                "args": args or [],
                "kwargs": kwargs or {},
            },
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(
            data["error"].get("data", {}).get("message", str(data["error"]))
        )
    return data.get("result")


# ===========================================================================
# MCP tools — process management
# ===========================================================================

_ACCESS_DENIED = "Access denied: '{}' has not been confirmed anonymized. Call scrub_database first."


@mcp.tool()
def query_psql(database: str, sql: str, limit: int = 200) -> str:
    """Execute a read-only SQL query against a local Odoo database.

    Limited to 'limit' rows to make context usage more efficient

    Args:
        database: Database name (e.g. 'odoo', 'adi')
        sql: SQL query to execute (SELECT only)
        limit: limit the max number of rows to return (default max 200)
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT") and not sql_stripped.startswith("WITH"):
        return "Error: only SELECT queries are allowed."
    try:
        conn = _db_connect(database)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall() 
            if len(rows) > limit:
                rows = rows[:limit] # limit to 200 records to save context window TODO may need to implement limit offset + generator object 
        conn.close()
        if not rows:
            return "No rows returned."
        headers = list(rows[0].keys())
        lines = ["\t".join(headers)]
        for row in rows:
            lines.append("\t".join(str(v) for v in row.values()))
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_odoo_version(database: str) -> str:
    """Return the Odoo version installed in a database.

    Reads the 'base' module record from ir_module_module directly via SQL —
    works whether or not the Odoo server is running.

    Args:
        database: Database name
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    try:
        conn = _db_connect(database)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT latest_version FROM ir_module_module WHERE name = 'base'"
            )
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        return f"Error: {e}"

    if not row:
        return "Error: 'base' module not found — is this an Odoo database?"
    return f"Odoo version: {row[0]}"


@mcp.tool()
def switch_odoo_branch(branch: str) -> str:
    """Switch the odoo and enterprise git repositories to the given branch.

    Call this (with the series from get_odoo_version) before start_odoo to
    ensure the source code matches the target database. The branch must
    already exist locally (e.g. '17.0', '16.0').

    Args:
        branch: Git branch name to check out, typically the first two
                dot-separated components of the Odoo version (e.g. '17.0')
    """
    # A branch switch must NEVER run as root. git checkout deletes and
    # recreates every file that differs between the two branches, and a file
    # created by a root process is owned by root — on a bind mount with no
    # userns remapping that is host root. Doing this under sudo once left
    # ~30k paths in odoo and ~27k in enterprise owned by root:root (plus
    # .git/HEAD), which locked the host user out of `git checkout` entirely
    # and aborted mid-checkout, leaving one branch's files overlaid on
    # another. Root bypassing the permission check is exactly what makes it
    # dangerous here: it cannot fail safely.
    #
    # It also isn't needed. The image builds `debugger` with the host user's
    # own UID/GID (Dockerfile HOST_UID), so this process already is the owner
    # of the bind-mounted repos. If a path genuinely isn't writable, git
    # fails loudly and changes nothing, which is the outcome we want.
    if branch.startswith("-"):
        return f"Refusing branch argument that looks like a flag: {branch}"

    lines = []
    for name, path in (("odoo", ODOO_REPO), ("enterprise", ENTERPRISE_REPO)):
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", path, "checkout", branch],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            lines.append(f"{name}: FAILED — {(result.stderr or result.stdout).strip()}")
        else:
            lines.append(f"{name}: OK")
    return "\n".join(lines)


def _is_scrubbed(database: str) -> bool:
    """Return True if the database has a confirmed-clean scrub.

    The marker is written either by the host-side start.sh/
    copy_and_scrub_db.sh pipeline (the normal path — a database usually
    arrives here already scrubbed, before the container even starts) or by
    scrub_database below as an in-container fallback. Both write it only
    after the same structural clean/dirty diff check comes back clean, so
    this checks for a *confirmed* anonymization, not merely an attempted one.
    """
    try:
        conn = _db_connect(database)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM ir_config_parameter WHERE key = %s", (SCRUBDB_MARKER_KEY,)
            )
            found = cur.fetchone() is not None
        conn.close()
        return found
    except Exception:
        return False


def _run(cmd, timeout):
    """subprocess.run wrapper that turns a timeout into a normal failed result."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(e))


@mcp.tool()
def scrub_database(database: str) -> str:
    """Anonymize an Odoo database in place using scrubdb, then confirm it's clean.

    Must be called before any other tool accesses the database — it's the
    only way to satisfy the gate those tools enforce. This is the fallback
    for a database that reached this cluster without already being scrubbed
    by the host-side start.sh/copy_and_scrub_db.sh pipeline (which normally
    handles it before the container ever starts) — e.g. one restored here by
    some other means.

    scrubdb's masking is one-way (the run key is discarded, nothing is
    reversible), so before touching the real database this snapshots its
    current state into a throwaway same-cluster copy, scrubs the real
    database in place, then structurally diffs the scrubbed result against
    that snapshot — every column scrubdb touched must have actually changed
    (the same clean/dirty check copy_and_scrub_db.sh uses on the host). The
    "confirmed anonymized" marker other tools check for is only written if
    that comes back clean; if not, the database is left scrubbed but NOT
    marked confirmed, and this returns the findings instead. The throwaway
    snapshot is always dropped afterward — only the real database's fate is
    reported.

    Args:
        database: Database name (must already exist on this cluster —
                  scrubdb rewrites it in place)
    """
    if _is_scrubbed(database):
        return (
            f"'{database}' is already confirmed scrubbed (marker "
            f"'{SCRUBDB_MARKER_KEY}' present) — skipping. Other tools are "
            f"already unlocked for it."
        )

    ts = int(time.time())
    tmp_db = f"_scrubdb_verify_{database}_{ts}"
    dump_path = f"/tmp/scrubdb_predump_{database}_{ts}.dump"
    report_path = f"/tmp/scrubdb_report_{database}_{ts}.json"
    diff_report_path = f"/tmp/scrubdb_diff_{database}_{ts}.json"

    def _drop_tmp() -> bool:
        # A connection closed by a subprocess that just exited (verify_scrub_diff.py)
        # can take Postgres a moment to notice — retry briefly rather than
        # leaking the throwaway copy on a race.
        for attempt in range(5):
            if _run(["dropdb", "--if-exists", tmp_db], timeout=60).returncode == 0:
                return True
            time.sleep(1)
        return False

    # 1. Snapshot the pre-scrub state — the only reference left to verify
    #    against once the real database is scrubbed in place. Dump+restore
    #    rather than CREATE DATABASE ... TEMPLATE, since the latter requires
    #    zero other connections to the source at the moment of copy.
    dump = _run(["pg_dump", "-Fc", "-f", dump_path, database], timeout=1800)
    if dump.returncode != 0:
        return f"scrub_database failed to snapshot '{database}' for verification:\n{dump.stderr}"
    create = _run(["createdb", tmp_db, "--encoding", "unicode", "--template", "template0",
                   "--lc-collate", "C", "--lc-ctype", "C"], timeout=60)
    if create.returncode != 0:
        os.remove(dump_path)
        return f"scrub_database failed to create a verification copy of '{database}':\n{create.stderr}"
    restore = _run(["pg_restore", "-d", tmp_db, "--no-owner", "--no-privileges", "-j", "4", dump_path],
                    timeout=1800)
    os.remove(dump_path)
    if restore.returncode != 0:
        _drop_tmp()
        return f"scrub_database failed to restore the verification copy of '{database}':\n{restore.stderr}"

    # 2. Scrub the real database in place.
    result = _run(
        [SCRUBDB_BIN, database, "--apply", "--i-confirm-this-is-a-copy", "--report", report_path],
        timeout=7200,
    )
    output = (result.stdout + result.stderr).strip()
    try:
        with open(report_path) as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        _drop_tmp()
        return f"scrub_database failed to produce a report (exit {result.returncode}):\n{output}"

    run = report.get("run", {})
    summary = (
        f"columns changed: {run.get('columns_changed', '?')}, "
        f"rows updated: {run.get('rows_updated', '?')}"
    )
    if result.returncode != 0:
        _drop_tmp()
        return f"scrubdb exited {result.returncode} ({summary}):\n{output}"

    # 3. Verify: diff the pre-scrub snapshot against the now-scrubbed real
    #    database (same cluster, different names).
    pg_host = os.environ.get("PGHOST", "")
    pg_port = os.environ.get("PGPORT", "5432")
    pg_user = os.environ.get("PGUSER", "")
    diff_result = _run(
        [
            sys.executable, VERIFY_SCRUB_DIFF_SCRIPT,
            "--db", tmp_db, "--dst-db", database,
            "--report", report_path,
            "--src-host", pg_host, "--src-port", pg_port, "--src-user", pg_user,
            "--dst-host", pg_host, "--dst-port", pg_port, "--dst-user", pg_user,
            "--diff-report", diff_report_path,
        ],
        timeout=1800,
    )
    cleaned_up = _drop_tmp()
    cleanup_note = "" if cleaned_up else f" (also: failed to drop throwaway copy '{tmp_db}' — drop it manually)"

    try:
        with open(diff_report_path) as f:
            diff_report = json.load(f)
    except (OSError, json.JSONDecodeError):
        return (
            f"scrubdb applied ({summary}) but the verification check itself failed "
            f"to produce a report (exit {diff_result.returncode}):\n"
            f"{(diff_result.stdout + diff_result.stderr).strip()}\n"
            f"NOT marked as confirmed anonymized — other tools remain locked out "
            f"of '{database}'.{cleanup_note}"
        )

    if not diff_report.get("clean"):
        return (
            f"scrubdb applied ({summary}) but verification found it not fully clean: "
            f"{diff_report.get('value_findings_total', 0)} unchanged value(s), "
            f"{len(diff_report.get('cardinality_findings', []))} row-count mismatch(es), "
            f"{len(diff_report.get('unverifiable_tables', []))} unverifiable table(s). "
            f"NOT marked as confirmed anonymized — other tools remain locked out of "
            f"'{database}'. Review the diff report ({diff_report_path}) and scrubdb "
            f"report ({report_path}), add --exclude/--ruleset overrides as needed, "
            f"and re-run scrub_database.{cleanup_note}"
        )

    assets_dropped = 0
    try:
        conn = _db_connect(database, readonly=False)
        with conn.cursor() as cur:
            # Drop Odoo's generated CSS/JS asset bundles. Masking leaves them
            # with a nulled url/store_fname: dead rows that 404, and a cold
            # first page load whose bundle rebuild is easily misread as an
            # application performance bug. They are regenerable caches, so
            # deleting them just makes that rebuild happen up front.
            #
            # This runs *after* verification on purpose. verify_scrub_diff.py
            # requires every non-truncated table's row count to match the pre-scrub
            # snapshot, and its value diff walks both snapshots as two
            # PK-ordered cursors zipped together — deleting rows mid-scrub
            # would both trip the row-count check and slide those cursors out
            # of alignment, silently comparing mismatched records. Doing it
            # here keeps the anonymization gate's invariants intact.
            #
            # res_model/res_id are matched because both survive masking
            # (DEFAULT_EXCLUDE_PATTERNS), unlike name (masked) and url (nulled).
            cur.execute(
                "DELETE FROM ir_attachment "
                "WHERE res_model = 'ir.ui.view' AND COALESCE(res_id, 0) = 0"
            )
            assets_dropped = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.execute(
                "INSERT INTO ir_config_parameter (key, value) VALUES (%s, 'true') "
                "ON CONFLICT (key) DO UPDATE SET value = 'true'",
                (SCRUBDB_MARKER_KEY,),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        return (
            f"scrubdb applied and verified clean ({summary}) but marking the "
            f"database as confirmed failed: {e}"
        )

    return (
        f"'{database}' scrubbed and confirmed clean ({summary}). "
        f"Verification found no surviving unmasked values. Other tools are now unlocked. "
        f"Dropped {assets_dropped} generated asset attachment(s); Odoo rebuilds them "
        f"on the first request, so warm the bundles once before timing anything."
        f"{cleanup_note}"
    )


@mcp.tool()
def start_odoo(database: str, extra_args: str = "") -> str:
    """Start the Odoo server in the background. Logs are written to LOG_FILE via --logfile.

    Use get_odoo_logs to observe server output. Use stop_odoo to shut it down.

    Args:
        database: Target database name (e.g. 'odoo', 'adi')
        extra_args: Additional odoo-bin flags, e.g. '--dev=all'
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    proc = _state["odoo_process"]
    if proc and proc.poll() is None:
        return f"Odoo is already running (pid {proc.pid}). Call stop_odoo first."

    extra_args_list = extra_args.split() if extra_args else []
    log_path = next(
        (a.split("=", 1)[1] for a in extra_args_list if a.startswith("--logfile=")),
        LOG_FILE,
    )
    extra_args_list = [a for a in extra_args_list if not a.startswith("--logfile=")]
    cmd = [
        ODOO_BIN,
        f"--addons-path={ODOO_ADDONS}",
        "-d", database,
        "--workers=0",
        f"--logfile={log_path}",
    ]
    cmd.extend(extra_args_list)

    try:
        # A crash that happens before Odoo's own --logfile logger initializes
        # (e.g. the startup Python-version assertion) prints to stderr instead
        # and would otherwise vanish, since nothing else captures this
        # process's stdout/stderr. Route both into the same log file so
        # get_odoo_logs can still see it.
        log_fh = open(log_path, "a")
        proc = subprocess.Popen(
            cmd, preexec_fn=os.setsid, env=_odoo_subprocess_env(),
            stdout=log_fh, stderr=subprocess.STDOUT,
        )
        _state["odoo_process"] = proc
        _state["log_file"] = log_path

        # Popen succeeding only means fork/exec worked, not that the process
        # is actually running Odoo — many failures (missing deps, an unmet
        # Python version assertion) kill it within milliseconds. Give it a
        # moment and confirm before reporting success.
        time.sleep(1.5)
        if proc.poll() is not None:
            log_fh.close()
            tail = ""
            if os.path.exists(log_path):
                with open(log_path) as f:
                    tail = f.read()[-4000:]
            return (
                f"Odoo failed to start (exited immediately, code {proc.returncode}).\n"
                f"Output:\n{tail}"
            )

        return (
            f"Odoo server started (pid {proc.pid}). "
            f"Logs -> {log_path}. Call get_odoo_logs to observe output."
        )
    except Exception as e:
        return f"Error starting Odoo: {e}"


@mcp.tool()
def get_odoo_logs(lines: int = 100) -> str:
    """Return the most recent log lines from the running Odoo server.

    Args:
        lines: Number of lines to return from the end of the log (default 100)
    """
    log_file = _state["log_file"]
    if not os.path.exists(log_file):
        return "No log file found. Start the server first with start_odoo."
    try:
        result = subprocess.run(["tail", f"-{lines}", log_file], capture_output=True, text=True)
        return result.stdout or "(log file is empty)"
    except Exception as e:
        return f"Error reading logs: {e}"


@mcp.tool()
def stop_odoo() -> str:
    """Stop the odoo-bin process started by start_odoo."""
    proc = _state["odoo_process"]
    if not proc or proc.poll() is not None:
        return "No running Odoo process found."
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        _state["odoo_process"] = None
        return "Odoo process terminated."
    except Exception as e:
        return f"Error stopping process: {e}"


# ===========================================================================
# MCP tools — ORM (JSON-2 API)
# ===========================================================================

@mcp.tool()
def search_records(
    database: str,
    model: str,
    domain: str = "[]",
    fields: str = "[]",
    limit: int = 80,
    offset: int = 0,
) -> str:
    """Search and read Odoo records using the ORM via the JSON-2 API.

    Requires a running Odoo server (start_odoo or an external instance).

    Args:
        database: Database name (e.g. 'odoo', 'adi')
        model: Odoo model technical name (e.g. 'res.partner', 'sale.order')
        domain: Odoo domain filter as a JSON array (e.g. '[["customer_rank",">",0]]')
        fields: Field names to return as a JSON array (e.g. '["name","email"]').
                Pass '[]' to return all fields.
        limit: Maximum number of records to return (default 80)
        offset: Number of records to skip for pagination (default 0)
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    try:
        domain_parsed = json.loads(domain)
        fields_parsed = json.loads(fields)
    except json.JSONDecodeError as e:
        return f"Error parsing arguments: {e}. Use JSON array syntax."

    try:
        records = _call_kw(
            database, model, "search_read",
            args=[domain_parsed],
            kwargs={"fields": fields_parsed, "limit": limit, "offset": offset},
        )
    except Exception as e:
        return f"Error: {e}"

    if not records:
        return "No records found."

    headers = list(records[0].keys())
    lines = ["\t".join(headers)]
    for rec in records:
        lines.append("\t".join(str(v) for v in rec.values()))
    return "\n".join(lines)


@mcp.tool()
def get_model_fields(database: str, model: str, attributes: str = "[]") -> str:
    """Return field definitions for an Odoo model using fields_get.

    Useful for discovering available field names before calling search_records.

    Args:
        database: Database name (e.g. 'odoo', 'adi')
        model: Odoo model technical name (e.g. 'res.partner')
        attributes: Field attributes to include as a JSON array
                    (e.g. '["string","type","required"]'). Pass '[]' for all.
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    try:
        attributes_parsed = json.loads(attributes)
    except json.JSONDecodeError as e:
        return f"Error parsing attributes: {e}."

    try:
        kw = {"attributes": attributes_parsed} if attributes_parsed else {}
        fields = _call_kw(database, model, "fields_get", kwargs=kw)
    except Exception as e:
        return f"Error: {e}"

    lines = []
    for fname, fdef in sorted(fields.items()):
        parts = [fname]
        if "type" in fdef:
            parts.append(fdef["type"])
        if "string" in fdef:
            parts.append(f'"{fdef["string"]}"')
        if fdef.get("required"):
            parts.append("required")
        if fdef.get("readonly"):
            parts.append("readonly")
        lines.append("  ".join(parts))
    return "\n".join(lines)

@mcp.tool()
def call_method(
    database: str,
    model: str,
    method: str,
    args: str = "[]",
    kwargs: str = "{}"
) -> str:
    """Call an arbitrary method on an Odoo model using the ORM via the JSON-2 API.

    Requires a running Odoo server.

    Args:
        database: Database name (e.g. 'odoo', 'adi')
        model: Odoo model technical name (e.g. 'res.partner')
        method: The name of the method to call (e.g. 'action_confirm', 'read')
        args: Positional arguments as a JSON array (e.g. '[[1, 2, 3]]' if the first argument is a list of IDs)
        kwargs: Keyword arguments as a JSON object (e.g. '{"context": {"lang": "en_US"}}')
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    
    try:
        args_parsed = json.loads(args)
        kwargs_parsed = json.loads(kwargs)
    except json.JSONDecodeError as e:
        return f"Error parsing arguments: {e}. Use JSON syntax."

    try:
        result = _call_kw(database, model, method, args=args_parsed, kwargs=kwargs_parsed)
    except Exception as e:
        return f"Error: {e}"

    if result is None:
        return "Method executed successfully (returned None)."
    
    try:
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Method executed successfully, but result could not be JSON-serialized: {e}\nRaw result: {result}"


# ===========================================================================
# MCP tools — performance & profiling
# ===========================================================================

def _get_odoo_pid() -> int:
    """Return the PID of the running Odoo process — MCP-managed or external."""
    proc = _state["odoo_process"]
    if proc and proc.poll() is None:
        return proc.pid
    try:
        result = subprocess.run(
            ["pgrep", "-f", "odoo-bin"],
            capture_output=True, text=True, check=True,
        )
        pids = [int(p) for p in result.stdout.split() if p.strip()]
        if pids:
            return pids[0]
    except subprocess.CalledProcessError:
        pass
    raise RuntimeError("No Odoo process found. Start one with start_odoo first.")


def _artifacts_dir() -> str:
    """Return a writable directory for tool output — flamegraphs, screenshots.

    Defaults under /source, which docker-compose bind-mounts from the host, so
    output survives the container's `docker compose run --rm` teardown and can
    actually be opened afterwards. Everything used to land in /tmp, which meant
    every flamegraph this server produced died with the session. Falls back to
    /tmp if the mount isn't writable for some reason.
    """
    try:
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        if os.access(ARTIFACTS_DIR, os.W_OK):
            return ARTIFACTS_DIR
    except OSError:
        pass
    return "/tmp"


def _default_output(prefix: str, ext: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(_artifacts_dir(), f"{prefix}_{stamp}.{ext}")


@mcp.tool()
def profile_cpu(duration: int = 30, output_path: str = "") -> str:
    """Record a CPU flamegraph of the running Odoo process using py-spy.

    Samples Python stacks at 100 Hz for the given duration, then writes a
    flamegraph SVG. Use to find hot code paths during slow requests or
    background jobs.

    Args:
        duration: Sampling duration in seconds (default 30)
        output_path: Output SVG path. Defaults to /tmp/odoo_cpu_<ts>.svg
    """
    try:
        pid = _get_odoo_pid()
    except RuntimeError as e:
        return f"Error: {e}"

    output = output_path or _default_output("odoo_cpu", "svg")
    try:
        result = subprocess.run(
            ["py-spy", "record", "--pid", str(pid),
             "-d", str(duration), "-o", output, "--subprocesses"],
            capture_output=True, text=True, timeout=duration + 30,
        )
    except subprocess.TimeoutExpired:
        return f"Error: py-spy did not finish within {duration + 30}s."

    if result.returncode != 0:
        return f"py-spy failed: {result.stderr.strip() or result.stdout.strip()}"
    return f"Flamegraph written to {output}\n{result.stdout.strip()}"


@mcp.tool()
def dump_stacks() -> str:
    """Dump current Python stack traces for all threads of the Odoo process.

    Non-invasive snapshot useful for diagnosing hangs, deadlocks, or
    long-running requests without a recording window.
    """
    try:
        pid = _get_odoo_pid()
    except RuntimeError as e:
        return f"Error: {e}"

    result = subprocess.run(
        ["py-spy", "dump", "--pid", str(pid)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"py-spy failed: {result.stderr.strip() or result.stdout.strip()}"
    return result.stdout


@mcp.tool()
def memray_attach(output_path: str = "", duration: int = 0) -> str:
    """Begin tracking memory allocations in the running Odoo process via memray.

    If duration > 0, blocks for that many seconds then auto-detaches.
    If duration == 0, returns immediately and tracking continues until
    memray_detach is called. Generate a report with memray_flamegraph.

    Args:
        output_path: Where to write the .bin allocation log.
                     Defaults to /tmp/odoo_mem_<ts>.bin
        duration: Auto-detach after N seconds (0 = manual detach)
    """
    if _state["memray_pid"] is not None:
        return f"memray is already tracking pid {_state['memray_pid']}. Call memray_detach first."
    try:
        pid = _get_odoo_pid()
    except RuntimeError as e:
        return f"Error: {e}"

    output = output_path or _default_output("odoo_mem", "bin")
    cmd = ["memray", "attach", str(pid), "-o", output, "-f"]
    if duration > 0:
        cmd.extend(["--duration", str(duration)])

    if duration > 0:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 30)
        if result.returncode != 0:
            return f"memray failed: {result.stderr.strip() or result.stdout.strip()}"
        return f"Tracked {duration}s of allocations → {output}"

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return f"memray failed: {result.stderr.strip() or result.stdout.strip()}"
    _state["memray_pid"] = pid
    return f"Tracking started on pid {pid} → {output}. Call memray_detach when done."


@mcp.tool()
def memray_detach() -> str:
    """Stop a memray tracking session previously started by memray_attach."""
    pid = _state["memray_pid"]
    if pid is None:
        return "No active memray tracking session."
    result = subprocess.run(
        ["memray", "detach", str(pid)],
        capture_output=True, text=True,
    )
    _state["memray_pid"] = None
    if result.returncode != 0:
        return f"memray detach failed: {result.stderr.strip() or result.stdout.strip()}"
    return f"Detached from pid {pid}. {result.stdout.strip()}"


@mcp.tool()
def memray_flamegraph(input_path: str, output_path: str = "") -> str:
    """Generate an HTML flamegraph from a memray .bin allocation log.

    Args:
        input_path: Path to the .bin file produced by memray_attach
        output_path: Output HTML path. Defaults to <input_path>.html
    """
    if not os.path.exists(input_path):
        return f"Error: input file not found: {input_path}"
    output = output_path or input_path + ".html"
    result = subprocess.run(
        ["memray", "flamegraph", "-o", output, "-f", input_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"memray flamegraph failed: {result.stderr.strip() or result.stdout.strip()}"
    return f"Flamegraph written to {output}"


@mcp.tool()
def get_process_info() -> str:
    """Return resource usage of the running Odoo process — RSS, threads, fd count, CPU time.

    Reads /proc/<pid>/status and /proc/<pid>/stat directly (no psutil dependency).
    """
    try:
        pid = _get_odoo_pid()
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        with open(f"/proc/{pid}/status") as f:
            status = {
                line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                for line in f if ":" in line
            }
        fd_count = len(os.listdir(f"/proc/{pid}/fd"))
        with open(f"/proc/{pid}/cmdline") as f:
            cmdline = f.read().replace("\x00", " ").strip()
    except (OSError, IOError) as e:
        return f"Error reading /proc/{pid}: {e}"

    return (
        f"PID:         {pid}\n"
        f"Cmdline:     {cmdline}\n"
        f"State:       {status.get('State', '?')}\n"
        f"VmRSS:       {status.get('VmRSS', '?')}  (resident memory)\n"
        f"VmSize:      {status.get('VmSize', '?')}  (virtual memory)\n"
        f"VmPeak:      {status.get('VmPeak', '?')}  (peak virtual)\n"
        f"Threads:     {status.get('Threads', '?')}\n"
        f"Open FDs:    {fd_count}\n"
        f"Uid:         {status.get('Uid', '?').split()[0]}\n"
    )


@mcp.tool()
def explain_query(database: str, sql: str) -> str:
    """Run EXPLAIN (ANALYZE, BUFFERS) on a SELECT query against an Odoo database.

    Reveals the actual query plan with row counts, timings, and buffer hits —
    use to diagnose slow queries surfaced by query_psql or the Odoo logs.

    Args:
        database: Database name
        sql: A SELECT query (no DML allowed)
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT") and not sql_stripped.startswith("WITH"):
        return "Error: only SELECT/WITH queries are allowed."
    try:
        conn = _db_connect(database)
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}")
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"Error: {e}"
    return "\n".join(row[0] for row in rows)


# ===========================================================================
# Web client (headless Chromium via Playwright)
#
# These are the only async tools in this file, and that is forced rather than
# stylistic. playwright.sync_api refuses to start inside a running asyncio
# loop, and FastMCP calls sync tools *directly* on its loop thread
# (fastmcp/utilities/func_metadata.py: `return fn(**arguments_parsed_dict)`,
# with no thread offload anywhere in the tool path), so the sync API would hit
# that guard on every single call. Async tools are awaited on the one loop
# FastMCP creates for the life of the process, which is also what lets a Page
# opened by browser_open still be valid in the next tool call.
#
# The tradeoff is that async tools don't block the loop, so two of them can
# interleave — every tool body below takes _state["browser_lock"] first.
# Helpers never take it, so there is no reentrancy deadlock. Blocking calls
# (_is_scrubbed, _get_session) are made directly on the loop, consistent with
# the rest of this file.
# ===========================================================================

_BROWSER_ARGS = [
    # The container has no CAP_SYS_ADMIN (docker-compose.yml only adds
    # SYS_PTRACE) and Ubuntu 24.04 restricts unprivileged user namespaces, so
    # Chromium's zygote sandbox can't initialize — it dies with "Failed to
    # move to new namespace" before the first page ever loads.
    "--no-sandbox",
    # Docker gives /dev/shm 64MB by default and there is no shm_size override
    # on older compose files. Chromium puts the renderer's shared-memory
    # backing store there, and an Odoo list view of a few hundred rows
    # overruns it — the tab dies and every later call fails with an opaque
    # "Target page, context or browser has been closed".
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

_CALL_KW_PATH = "/web/dataset/call_kw"
_LOGOUT_PATH = "/web/session/logout"
# Ring-buffer size for the console/network recorders. One Odoo page load alone
# is a few hundred requests, and this state lives as long as the MCP server.
_BROWSER_BUFFER = 500
# Grace before _settle first samples the in-flight counter — see _settle.
_SETTLE_GRACE_MS = 250

# Installed on the *context*, so it runs before any page script on every
# navigation including redirects. It is what _settle() waits on.
#
# Patching XMLHttpRequest is not belt-and-braces: Odoo's RPC layer is XHR,
# not fetch (web/static/src/core/network/rpc.js). fetch is patched too for the
# paths that do use it. The bus connection is excluded because it never
# closes — counting it would mean the page is never idle, which is also
# exactly why Playwright's own wait_until="networkidle" is useless here.
_INFLIGHT_INIT_JS = r"""
(() => {
  if (window.__mcp_inflight !== undefined) return;
  window.__mcp_inflight = 0;
  const ignore = (u) => /\/websocket|\/longpolling\/|\/bus\//.test(String(u || ""));
  const XHR = window.XMLHttpRequest;
  if (XHR) {
    const open = XHR.prototype.open;
    const send = XHR.prototype.send;
    XHR.prototype.open = function (method, url) {
      this.__mcp_tracked = !ignore(url);
      return open.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      if (this.__mcp_tracked) {
        window.__mcp_inflight++;
        const done = () => {
          if (this.__mcp_tracked) {
            this.__mcp_tracked = false;
            window.__mcp_inflight--;
          }
        };
        this.addEventListener("loadend", done);
      }
      return send.apply(this, arguments);
    };
  }
  const nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input) {
      const url = typeof input === "string" ? input : (input && input.url);
      if (ignore(url)) return nativeFetch.apply(this, arguments);
      window.__mcp_inflight++;
      return nativeFetch.apply(this, arguments).finally(() => {
        window.__mcp_inflight--;
      });
    };
  }
})();
"""

_NO_BROWSER = "No browser is open. Call browser_open first."


def _require_playwright() -> str:
    """Return an error message if playwright is missing, else an empty string."""
    if async_playwright is None:
        return (
            "Error: playwright is not installed in this image. Rebuild the sandbox "
            f"with ./start.sh (it passes --build) to pick it up. ({_PLAYWRIGHT_IMPORT_ERROR})"
        )
    return ""


async def _require_browser():
    """Return (browser_record, "") if a browser is open and usable, else (None, message).

    Re-checks _is_scrubbed rather than trusting the check browser_open did: the
    browser outlives a single tool call, so the anonymization gate has to be
    re-asserted at every entry point, exactly like the database tools do. Also
    reaps a session whose Chromium has died — an OOM-killed or crashed renderer
    leaves a Browser handle that is no longer connected, and every operation on
    it would otherwise fail with an opaque "Target closed".
    """
    rec = _state["browser"]
    if rec is None:
        return None, _NO_BROWSER
    if not _is_scrubbed(rec["database"]):
        return None, _ACCESS_DENIED.format(rec["database"])
    try:
        connected = rec["browser"].is_connected()
    except Exception:
        connected = False
    if not connected:
        await _teardown(rec.get("pw"), rec.get("browser"), rec.get("context"))
        _state["browser"] = None
        return None, (
            "The browser is gone (Chromium is no longer connected) — the session was "
            "discarded. Call browser_open again."
        )
    return rec, ""


def _resolve_url(path: str) -> str:
    """Resolve a caller-supplied path or URL into something to navigate to.

    Relative paths resolve against the local Odoo server; absolute URLs pass
    through (this sandbox has outbound internet access by design — see the
    README's security notes). /web/session/logout is the one refusal: the
    browser deliberately shares its server-side session with the RPC tools, and
    logging out hard-rotates that session, breaking call_method and
    search_records for the rest of this server's life.
    """
    if path.lower().startswith(("http://", "https://")):
        target = path
    else:
        target = ODOO_URL + "/" + path.lstrip("/")
    if urllib.parse.urlsplit(target).path.rstrip("/") == _LOGOUT_PATH:
        raise ValueError(
            "refusing to navigate to /web/session/logout — it would invalidate the "
            "session the ORM tools share with the browser. Use browser_close instead."
        )
    return target


def _summarize_body(text: str, content_type: str) -> str:
    """Condense a response body into one line (plus traceback tail for Odoo errors)."""
    if "json" in content_type.lower():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            info = err.get("data") or {}
            name = info.get("name") or err.get("message") or "error"
            message = " ".join((info.get("message") or "").split())
            out = f"{name}: {message}" if message else str(name)
            frames = [ln.strip() for ln in (info.get("debug") or "").splitlines() if ln.strip()]
            if frames:
                out += "\n" + "\n".join(f"                {f}" for f in frames[-2:])
            return out
    collapsed = " ".join(text.split())
    return collapsed[:300] + ("…" if len(collapsed) > 300 else "")


def _attach_recorders(page, rec: dict) -> None:
    """Wire the page's console/error/network events into rec's ring buffers.

    Called once per page immediately after creation and *before* the first
    navigation, so the boot-time failures that matter most — an asset bundle
    that 404s, an Owl template that fails to compile — are captured.
    """

    def _stamp() -> float:
        return time.time() - rec["opened_at"]

    def on_console(msg):
        loc = msg.location or {}
        where = f"{loc.get('url', '')}:{loc.get('lineNumber', '')}".strip(":")
        rec["console"].append(
            {"t": _stamp(), "kind": msg.type, "text": msg.text, "where": where}
        )

    def on_pageerror(err):
        message = getattr(err, "message", None) or str(err)
        stack = (getattr(err, "stack", "") or "").splitlines()
        rec["console"].append({
            "t": _stamp(),
            "kind": "pageerror",
            "text": " ".join(str(message).split()),
            "where": stack[1].strip() if len(stack) > 1 else "",
        })

    def on_request(req):
        entry = {
            "t": _stamp(), "method": req.method, "url": req.url,
            "status": None, "call_kw": None, "detail": "", "failure": None,
        }
        if _CALL_KW_PATH in req.url:
            try:
                params = (req.post_data_json or {}).get("params", {})
                entry["call_kw"] = f"{params.get('model')}.{params.get('method')}"
            except Exception:
                entry["call_kw"] = "call_kw (payload unparsed)"
        # Keyed by the Request object itself: Playwright's bindings keep a
        # guid->object map, so the instance handed to this event is the same
        # one later reachable as response.request.
        rec["pending"][req] = entry
        rec["network"].append(entry)

    async def on_response(resp):
        # A coroutine handler is legal here — Playwright emits through pyee's
        # AsyncIOEventEmitter, which schedules coroutine listeners as tasks.
        entry = rec["pending"].pop(resp.request, None)
        if entry is None:
            return
        entry["status"] = resp.status
        # Odoo answers a server-side exception with HTTP 200 and an "error"
        # object in the body, so the status alone says nothing about a call_kw
        # — the body has to be read. Everything else is only read when the
        # status itself is bad, which keeps multi-megabyte asset bundles out
        # of memory.
        if entry["call_kw"] is None and 200 <= resp.status < 300:
            return
        try:
            body = await resp.text()
            entry["detail"] = _summarize_body(body, resp.headers.get("content-type", ""))
        except Exception as e:
            entry["detail"] = f"(body unavailable: {e})"

    def on_requestfailed(req):
        entry = rec["pending"].pop(req, None)
        if entry is None:
            return
        entry["failure"] = req.failure or "request failed"

    async def on_dialog(dialog):
        # Not optional: Odoo puts a native beforeunload handler on dirty form
        # views. Playwright auto-dismisses dialogs only when nothing is
        # listening, which would swallow it silently and turn "my navigation
        # didn't go where I asked" into a mystery.
        rec["console"].append({
            "t": _stamp(), "kind": "dialog",
            "text": f"{dialog.type}: {dialog.message} (dismissed)", "where": "",
        })
        try:
            await dialog.dismiss()
        except Exception:
            pass

    def on_crash(_page):
        rec["console"].append({
            "t": _stamp(), "kind": "crash",
            "text": "the page crashed (renderer died)", "where": "",
        })

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_requestfailed)
    page.on("dialog", on_dialog)
    page.on("crash", on_crash)


async def _settle(page, timeout: int = 15000) -> str:
    """Wait until the Odoo web client is idle. Returns "" or a warning to append.

    A short grace period, then:
      1. no non-bus RPC in flight (the counter installed by _INFLIGHT_INIT_JS)
      2. two animation frames — when Owl has flushed its render queue
      3. the counter again, since a component that just mounted can fire its
         own onWillStart RPC that didn't exist at step 1

    The grace is the one fixed wait here and it earns its place: a handler that
    defers its RPC — Odoo debounces several, the search input among them —
    hasn't issued the request yet at the moment the click returns, so checking
    the counter immediately finds zero in flight and reports a settled page
    that is about to become busy. Everything after the grace is event-driven.

    Deliberately not .o_loading_indicator: that component only sets show=true
    after a 250ms timer, so a fast RPC never renders it and "wait for it to be
    hidden" resolves instantly on a page that is busy. Deliberately not
    wait_until="networkidle": the bus connection never closes.

    Never raises — a page that is still busy is a finding to report, not a
    tool failure.
    """
    deadline = time.monotonic() + timeout / 1000.0
    try:
        await page.wait_for_timeout(_SETTLE_GRACE_MS)
        for phase in (1, 2):
            while True:
                inflight = await page.evaluate("() => window.__mcp_inflight || 0")
                if not inflight:
                    break
                if time.monotonic() > deadline:
                    return (
                        f"\n(warning: page still busy after {timeout}ms, {inflight} "
                        "request(s) in flight — output may be mid-render)"
                    )
                await page.wait_for_timeout(50)
            if phase == 1:
                await page.evaluate(
                    "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
                )
    except Exception:
        # Navigated away mid-check, page closed, execution context destroyed —
        # none of which is worth failing the caller's actual operation over.
        return ""
    return ""


async def _first_text(page, selector: str, limit: int = 1500) -> str:
    """Return the collapsed text of the first visible match, or ""."""
    try:
        loc = page.locator(selector)
        if await loc.count() and await loc.first.is_visible():
            return " ".join((await loc.first.inner_text()).split())[:limit]
    except Exception:
        pass
    return ""


async def _page_context(page) -> str:
    """Return a short header describing where the page is and what is shouting.

    Prepended to every action tool's result so a click that silently raised a
    ValidationError reads as a ValidationError rather than as nothing happening.
    """
    lines = [f"URL: {page.url}"]
    crumb = await _first_text(page, ".o_last_breadcrumb_item", 200)
    if crumb:
        lines.append(f"Breadcrumb: {crumb}")
    # .o_error_dialog is the class Odoo puts on every server-error, timeout and
    # session-expired dialog (web/static/src/core/errors/error_dialogs.xml).
    dialog = await _first_text(page, ".o_error_dialog")
    if dialog:
        lines.append(f"ERROR DIALOG: {dialog}")
    notification = await _first_text(page, ".o_notification", 500)
    if notification:
        lines.append(f"Notification: {notification}")
    return "\n".join(lines)


async def _snapshot_root(page, selector: str):
    """Pick what to snapshot: an explicit selector, else the topmost open dialog,
    else the action manager, else body.

    Defaulting to the dialog is almost always what the caller wants — if a modal
    is up, the list view behind it is noise.
    """
    if selector:
        return page.locator(selector).first, selector
    try:
        dialogs = page.locator(".o_dialog")
        if await dialogs.count():
            return dialogs.last, ".o_dialog (topmost open dialog)"
    except Exception:
        pass
    try:
        action = page.locator(".o_action_manager")
        if await action.count():
            return action.first, ".o_action_manager"
    except Exception:
        pass
    return page.locator("body"), "body"


async def _wait_webclient(page, timeout: int) -> None:
    """Wait for something client-side to actually mount.

    Not body.o_web_client: that class is set server-side in the template
    (web/views/webclient_templates.xml), so it is in the HTML the instant the
    response arrives, long before Owl mounts anything — waiting on it looks
    like it works and waits for nothing. .o_navbar and .o_action_manager are
    rendered by Owl components; .oe_login_form means we landed on the login
    page instead, which the caller checks for separately.
    """
    try:
        await page.wait_for_selector(
            ".o_navbar, .o_action_manager, .oe_login_form", timeout=timeout
        )
    except Exception:
        pass


async def _auth_rejected(page) -> bool:
    """Did Odoo refuse the session we injected?

    Two shapes, and checking only the first is a bug: a 303 redirect back to
    /web/login, or no redirect at all but an "Odoo Session Expired" dialog
    rendered over an otherwise-loaded client.
    """
    if "/web/login" in page.url:
        return True
    dialog = await _first_text(page, ".o_error_dialog")
    return "session expired" in dialog.lower()


async def _login_form(rec: dict, timeout: int) -> str:
    """Log in through the real form at /web/login. Fallback path for _authenticate."""
    page = rec["page"]
    await page.goto(ODOO_URL + "/web/login", wait_until="domcontentloaded", timeout=timeout)
    await page.fill('input[name="login"]', ODOO_LOGIN)
    await page.fill('input[name="password"]', ODOO_PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_url(lambda url: "/web/login" not in url, timeout=timeout)
    # Put the browser's new session_id back on the requests.Session. Without
    # this the RPC tools and the browser sit on two different server-side
    # sessions with independently mutable context (lang, timezone, active
    # company), and a repro that sets something up over RPC and verifies it in
    # the UI quietly stops making sense.
    for cookie in await rec["context"].cookies(ODOO_URL):
        if cookie["name"] == "session_id":
            _get_session(rec["database"]).cookies.set("session_id", cookie["value"])
    return "form login"


async def _authenticate(rec: dict) -> str:
    """Give the browser context an authenticated Odoo session. Returns a label.

    Lifts the session_id cookie straight off the authenticated requests.Session
    _get_session already maintains for this database. That skips the login form
    entirely and, more usefully, carries the server-side session's database
    selection with it, so the browser lands on the right database without a db=
    parameter. The form login is the fallback, applied by browser_open if the
    cookie turns out to be rejected.

    The cookie is read by iterating the jar, not via cookies.get(), which is
    domain-scoped and returns None on a domain mismatch.
    """
    session = _get_session(rec["database"])
    sid = next((c.value for c in session.cookies if c.name == "session_id"), None)
    if not sid:
        return ""
    await rec["context"].add_cookies(
        [{"name": "session_id", "value": sid, "url": ODOO_URL}]
    )
    return "session cookie"


async def _teardown(pw=None, browser=None, context=None) -> None:
    """Close a browser session's objects innermost-first, tolerating failures.

    Each close is independently guarded: a context that is already dead must
    not stop the Browser and the Playwright driver behind it from being shut
    down, which is precisely how a Chromium process gets orphaned.
    """
    for closer in (
        getattr(context, "close", None),
        getattr(browser, "close", None),
        getattr(pw, "stop", None),
    ):
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            pass


async def _launch_browser(database: str) -> dict:
    """Start Playwright, launch Chromium, build a context/page, authenticate.

    Unwinds completely through _teardown if any step fails, so a half-built
    session never orphans a Chromium process.
    """
    pw = await async_playwright().start()
    browser = context = None
    try:
        browser = await pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        context = await browser.new_context(
            # Below roughly 768px Odoo swaps to an entirely different mobile
            # DOM, so pin this rather than take Playwright's 1280x720 default.
            viewport={"width": 1600, "height": 1000},
            locale="en-US",
            # Odoo's JS formats datetimes in the *browser's* timezone, not the
            # user's Odoo timezone. Unpinned, every date bug is irreproducible
            # across machines.
            timezone_id="UTC",
            accept_downloads=True,
        )
        await context.add_init_script(_INFLIGHT_INIT_JS)
        rec = {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": None,
            "database": database,
            "console": collections.deque(maxlen=_BROWSER_BUFFER),
            "network": collections.deque(maxlen=_BROWSER_BUFFER),
            "pending": {},
            "opened_at": time.time(),
            "auth": "",
        }
        # Cookie before the first page, so the very first navigation is already
        # authenticated and there is no login round trip to unwind.
        rec["auth"] = await _authenticate(rec)
        page = await context.new_page()
        rec["page"] = page
        _attach_recorders(page, rec)
        return rec
    except Exception:
        await _teardown(pw, browser, context)
        raise


def _pw_error(e: Exception) -> str:
    """Condense a Playwright error to something worth spending context on.

    A TimeoutError carries the full retry call log — dozens of lines of
    "waiting for locator(...)", "element is not visible", "retrying". The first
    line plus the last few say everything; the rest is noise. A tail mentioning
    pointer events usually means a route change hung: webclient.js sets
    body.style.pointerEvents = "none" for the duration of one.
    """
    lines = [ln for ln in str(e).strip().splitlines() if ln.strip()]
    if len(lines) <= 6:
        return "\n".join(lines)
    return "\n".join(lines[:2] + ["  …"] + lines[-3:])


@mcp.tool()
async def browser_open(database: str, path: str = "/odoo", debug: bool = True, timeout: int = 120000) -> str:
    """Open a headless Chromium on the local Odoo web client and log in.

    Starts a browser that PERSISTS across tool calls — every other browser_*
    tool operates on the page this creates, until browser_close. Requires a
    running Odoo server (start_odoo).

    Authenticates by reusing the session cookie the ORM tools already hold for
    this database, so there is no login form to drive and no database selector
    to answer; falls back to a real form login if that cookie is rejected.
    Console output, page errors and network traffic are recorded from the
    moment the page exists — read them with browser_console and browser_network.

    The first load after a scrub is slow and that is not a bug: scrub_database
    deletes the generated CSS/JS asset attachments, so Odoo rebuilds the whole
    web.assets_web bundle on this request, single-threaded under --workers=0.
    That rebuild is warmed over plain HTTP before the browser navigates, and
    the elapsed time is reported separately so it isn't mistaken for a
    page-render problem.

    Args:
        database: Target database name (e.g. 'odoo', 'adi')
        path: Path to open (default '/odoo', the web client). Odoo 19 routes
              '/odoo/res.partner/42' straight to that record's form.
        debug: Open with ?debug=1, so Odoo puts the full server traceback in
               its error dialogs instead of a one-line message — usually the
               whole point of reproducing the bug. Not debug=assets, which
               additionally disables bundling and makes every load far slower.
        timeout: Milliseconds for the initial load (default 120000, sized for
                 a cold asset-bundle rebuild)
    """
    missing = _require_playwright()
    if missing:
        return missing
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)

    async with _state["browser_lock"]:
        if _state["browser"] is not None:
            return "A browser is already open. Call browser_close first."

        # Before anything is spawned: this both proves Odoo is up and produces
        # the cookie the browser will use, so failing here costs nothing.
        try:
            session = _get_session(database)
        except Exception as e:
            return (
                f"Error: could not authenticate against Odoo at {ODOO_URL} — is the "
                f"server running (start_odoo)? ({e})"
            )

        # Warm the asset bundles over HTTP rather than inside page.goto: a cold
        # rebuild there is a navigation timeout with an unhelpful message,
        # whereas here it is just a number in the result.
        warm = ""
        try:
            started = time.time()
            session.get(f"{ODOO_URL}/odoo", timeout=timeout / 1000.0)
            elapsed = time.time() - started
            if elapsed > 5:
                warm = (
                    f"Asset bundle warm-up: {elapsed:.1f}s (cold rebuild after scrub — "
                    "subsequent loads are fast).\n"
                )
        except Exception as e:
            warm = f"(asset warm-up request failed, continuing anyway: {e})\n"

        try:
            rec = await _launch_browser(database)
        except Exception as e:
            return f"Error launching Chromium: {_pw_error(e)}"

        page = rec["page"]
        try:
            target = _resolve_url(path)
            if debug:
                target += ("&" if "?" in target else "?") + "debug=1"
            load_timeout = min(timeout, 60000)
            await page.goto(target, wait_until="domcontentloaded", timeout=timeout)
            await _wait_webclient(page, timeout=load_timeout)
            if await _auth_rejected(page):
                rec["auth"] = await _login_form(rec, timeout=load_timeout)
                await page.goto(target, wait_until="domcontentloaded", timeout=timeout)
                await _wait_webclient(page, timeout=load_timeout)
            busy = await _settle(page)
        except ValueError as e:
            await _teardown(rec.get("pw"), rec.get("browser"), rec.get("context"))
            return f"Error: {e}"
        except Exception as e:
            await _teardown(rec.get("pw"), rec.get("browser"), rec.get("context"))
            return f"Error opening {path}: {_pw_error(e)}"

        _state["browser"] = rec
        context_lines = await _page_context(page)
        errors = sum(1 for c in rec["console"] if c["kind"] in ("error", "pageerror"))
        return (
            f"Browser open (database '{database}', logged in as {ODOO_LOGIN} via "
            f"{rec['auth'] or 'no session cookie — check the page'}).\n"
            f"{warm}{context_lines}\n"
            f"Recorded so far: {len(rec['network'])} request(s), "
            f"{len(rec['console'])} console message(s), {errors} error(s).\n"
            f"Call browser_snapshot to see what is on screen.{busy}"
        )


@mcp.tool()
async def browser_goto(path: str, timeout: int = 60000) -> str:
    """Navigate the open browser to another page.

    Odoo 19 routes the web client at /odoo and /odoo/<subpath>, so
    '/odoo/res.partner/42' opens that partner's form and '/odoo/sales' opens
    the Sales app. Legacy '/web#action=...' hashes still redirect. Absolute
    URLs are allowed too.

    Args:
        path: Path on the Odoo server, e.g. '/odoo/res.partner/42', or a full URL
        timeout: Milliseconds to allow for the navigation (default 60000)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        page = rec["page"]
        try:
            await page.goto(_resolve_url(path), wait_until="domcontentloaded", timeout=timeout)
            await _wait_webclient(page, timeout=timeout)
            busy = await _settle(page)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error navigating to {path}: {_pw_error(e)}"
        return f"{await _page_context(page)}{busy}"


@mcp.tool()
async def browser_snapshot(selector: str = "", max_chars: int = 6000) -> str:
    """Return an accessibility-tree snapshot of what is currently on screen.

    This is the primary way to see the page. It is a compact structural outline
    — roles, names, values, headings, rows, buttons — not markup and not an
    image, so it is readable and cheap. Use the names it shows to build
    selectors for browser_click and browser_fill.

    With no selector it snapshots the topmost open dialog if there is one,
    otherwise the action manager (the view area, without the navbar chrome).
    Pass a selector to narrow it further — an Odoo list view of a few hundred
    rows will otherwise be truncated.

    Any visible Odoo error dialog or notification is reported above the
    snapshot regardless of the selector.

    Args:
        selector: CSS selector to snapshot (default: dialog, else '.o_action_manager')
        max_chars: Truncate the snapshot beyond this many characters (default 6000)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        page = rec["page"]
        try:
            root, described = await _snapshot_root(page, selector)
            snapshot = await root.aria_snapshot()
        except Exception as e:
            return f"Error taking snapshot: {_pw_error(e)}"
        if len(snapshot) > max_chars:
            snapshot = (
                snapshot[:max_chars]
                + f"\n… (truncated at {max_chars} chars — pass a narrower selector)"
            )
        return f"{await _page_context(page)}\n\nSnapshot of {described}:\n{snapshot}"


@mcp.tool()
async def browser_click(selector: str, timeout: int = 15000) -> str:
    """Click an element, then wait for Odoo to finish reacting.

    Waits for the element to be visible, stable, enabled and actually clickable
    before clicking, so no sleep is needed beforehand. Afterwards it waits for
    every non-bus RPC the click triggered and for Owl to flush its render, then
    reports the resulting URL, breadcrumb, and any error dialog or notification
    that appeared.

    Prefer a role/name selector taken from browser_snapshot over a brittle class
    chain: Owl replaces DOM nodes on re-render, and Playwright's retry only
    helps if the selector still matches the new node.

    Args:
        selector: CSS or Playwright selector, e.g. "button:has-text('Confirm')",
                  ".o_list_view .o_data_row:first-child", "[name='partner_id'] input"
        timeout: Milliseconds to wait for the element (default 15000)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        page = rec["page"]
        before = len(rec["console"])
        try:
            await page.locator(selector).first.click(timeout=timeout)
        except Exception as e:
            return f"Error clicking {selector!r}: {_pw_error(e)}"
        busy = await _settle(page)
        new_errors = sum(
            1 for c in list(rec["console"])[before:] if c["kind"] in ("error", "pageerror")
        )
        tail = f"\n{new_errors} new console error(s) — call browser_console." if new_errors else ""
        return f"Clicked {selector}\n{await _page_context(page)}{tail}{busy}"


@mcp.tool()
async def browser_fill(selector: str, value: str, press_enter: bool = False, timeout: int = 15000) -> str:
    """Type a value into a field, clearing it first.

    For an Odoo many2one or tag field, set press_enter to accept the first
    autocomplete suggestion — without it the typed text is left dangling and
    the record will not save.

    Args:
        selector: Selector for the input, e.g. "[name='name'] input", "input#login"
        value: Text to enter
        press_enter: Press Enter afterwards (default False)
        timeout: Milliseconds to wait for the field (default 15000)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        page = rec["page"]
        try:
            field = page.locator(selector).first
            await field.fill(value, timeout=timeout)
            if press_enter:
                await field.press("Enter", timeout=timeout)
        except Exception as e:
            return f"Error filling {selector!r}: {_pw_error(e)}"
        busy = await _settle(page)
        return f"Filled {selector}\n{await _page_context(page)}{busy}"


@mcp.tool()
async def browser_press(key: str, selector: str = "", timeout: int = 15000) -> str:
    """Send a key to the page, or to a specific element.

    For Odoo interactions with no clickable target — Escape to dismiss a
    dropdown or dialog, Enter to validate an inline edit, 'Alt+s' to save a form.

    Args:
        key: Key name or chord, e.g. 'Enter', 'Escape', 'Tab', 'Alt+s'
        selector: Element to focus first; empty sends to the page
        timeout: Milliseconds to wait for the element (default 15000)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        page = rec["page"]
        try:
            if selector:
                await page.locator(selector).first.press(key, timeout=timeout)
            else:
                await page.keyboard.press(key)
        except Exception as e:
            return f"Error pressing {key!r}: {_pw_error(e)}"
        busy = await _settle(page)
        return f"Pressed {key}\n{await _page_context(page)}{busy}"


@mcp.tool()
async def browser_console(limit: int = 40, kinds: str = "error,warning,pageerror", clear: bool = False) -> str:
    """Return browser console output recorded since the page was created.

    Covers console messages, uncaught JavaScript exceptions (reported as
    'pageerror', with the top stack frame), native JS dialogs that were
    auto-dismissed, and renderer crashes. Recording starts before the first
    navigation, so boot-time failures — an asset bundle that 404s, an Owl
    template that fails to compile — are captured.

    Args:
        limit: Most recent N entries to return (default 40)
        kinds: Comma-separated types to include: log, info, warning, error,
               pageerror, dialog, crash. Pass 'all' for everything.
        clear: Empty the buffer after returning it, so the next call shows only
               what a subsequent action produced (default False)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        wanted = (
            None if kinds.strip().lower() == "all"
            else {k.strip().lower() for k in kinds.split(",") if k.strip()}
        )
        matching = [
            c for c in rec["console"] if wanted is None or c["kind"].lower() in wanted
        ]
        recorded = len(rec["console"])
        shown = matching[-limit:]
        if clear:
            rec["console"].clear()
        if not shown:
            return f"No console entries matching '{kinds}' ({recorded} recorded in total)."
        lines = [
            f"[{c['t']:6.1f}s] {c['kind']:<9} {c['text']}"
            + (f"  @ {c['where']}" if c["where"] else "")
            for c in shown
        ]
        return (
            f"{len(shown)} of {len(matching)} matching entries ({recorded} recorded):\n"
            + "\n".join(lines)
        )


@mcp.tool()
async def browser_network(limit: int = 25, only: str = "problems", clear: bool = False) -> str:
    """Return the page's network activity, with Odoo's JSON-RPC calls decoded.

    Each /web/dataset/call_kw request is shown as the model and method it
    invoked rather than an opaque URL, and its response body is parsed: Odoo
    answers a server-side exception with HTTP 200 and an "error" object, so the
    status code alone tells you nothing — the exception name, message and the
    tail of the server traceback are surfaced instead. Non-2xx responses get
    their status and a body excerpt; requests that never completed get their
    failure reason.

    Args:
        limit: Most recent N entries to return (default 25)
        only: 'problems' for failed requests and failed RPCs only (default),
              'rpc' for every call_kw, 'all' for every request including assets
        clear: Empty the buffer after returning it (default False)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        mode = only.strip().lower()

        def keep(entry: dict) -> bool:
            if mode == "all":
                return True
            if mode == "rpc":
                return entry["call_kw"] is not None
            if entry["failure"]:
                return True
            if entry["status"] is not None and not 200 <= entry["status"] < 300:
                return True
            return bool(entry["call_kw"] and entry["detail"])

        matching = [e for e in rec["network"] if keep(e)]
        recorded = len(rec["network"])
        shown = matching[-limit:]
        if clear:
            rec["network"].clear()
            rec["pending"].clear()
        if not shown:
            return (
                f"No network entries matching '{mode}' ({recorded} requests recorded). "
                "Pass only='all' to see everything."
            )
        lines = []
        for e in shown:
            status = e["status"] if e["status"] is not None else "---"
            label = f"call_kw {e['call_kw']}" if e["call_kw"] else e["url"]
            line = f"[{e['t']:6.1f}s] {e['method']:<5} {str(status):<4} {label}"
            if e["failure"]:
                line += f"  ->  {e['failure']}"
            elif e["detail"]:
                line += f"  ->  {e['detail']}"
            lines.append(line)
        return (
            f"{len(shown)} of {len(matching)} matching entries ({recorded} requests recorded):\n"
            + "\n".join(lines)
        )


@mcp.tool()
async def browser_screenshot(output_path: str = "", selector: str = "", full_page: bool = False) -> str:
    """Save a PNG of the open page and return its path.

    Secondary to browser_snapshot — reach for this when the bug is visual
    (layout, overlap, a wrong colour), since an accessibility snapshot says far
    more about a working page per token. Writes a file and returns its path
    rather than returning image data.

    Args:
        output_path: Where to write the PNG. Defaults to the artifacts
                     directory, which is bind-mounted to the host.
        selector: Screenshot just this element instead of the viewport
        full_page: Capture the full scrollable page rather than the viewport
                   (default False; an Odoo list view can be very tall)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        page = rec["page"]
        output = output_path or _default_output("odoo_browser", "png")
        try:
            if selector:
                await page.locator(selector).first.screenshot(path=output)
            else:
                await page.screenshot(path=output, full_page=full_page)
        except Exception as e:
            return f"Error taking screenshot: {_pw_error(e)}"
        return f"Screenshot written to {output}"


@mcp.tool()
async def browser_eval(expression: str, timeout: int = 15000) -> str:
    """Evaluate a JavaScript expression in the open page and return the result.

    The escape hatch for what the other tools can't reach: inspecting Owl
    component state, reading a service off the environment, checking what a
    registry contains. With browser_open(debug=True) the web client exposes
    odoo.__WOWL_DEBUG__.root, whose .env gets you the services.

    The expression is evaluated as-is; wrap it in an arrow function if you need
    statements. The result must be JSON-serializable — DOM nodes and component
    instances are not, so read properties off them instead of returning them.

    Args:
        expression: JavaScript to evaluate, e.g.
                    "odoo.__WOWL_DEBUG__.root.env.services.action.currentController.action.res_model"
        timeout: Milliseconds to allow (default 15000)
    """
    async with _state["browser_lock"]:
        rec, err = await _require_browser()
        if err:
            return err
        try:
            result = await asyncio.wait_for(
                rec["page"].evaluate(expression), timeout=timeout / 1000.0
            )
        except asyncio.TimeoutError:
            return f"Error: evaluation did not finish within {timeout}ms."
        except Exception as e:
            return f"Error evaluating expression: {_pw_error(e)}"
        try:
            rendered = json.dumps(result, indent=2, default=str)
        except (TypeError, ValueError):
            rendered = str(result)
        return rendered[:8000]


@mcp.tool()
async def browser_close() -> str:
    """Close the browser opened by browser_open and free its Chromium process.

    Also discards the recorded console and network buffers — read anything you
    still need with browser_console / browser_network first.
    """
    async with _state["browser_lock"]:
        rec = _state["browser"]
        if rec is None:
            return "No browser is open."
        await _teardown(rec.get("pw"), rec.get("browser"), rec.get("context"))
        _state["browser"] = None
        return "Browser closed."


if __name__ == "__main__":
    mcp.run()
