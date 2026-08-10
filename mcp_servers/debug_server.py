import subprocess
import signal
import os
import sys
import json
import socket
import time
from datetime import datetime
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from mcp.server.fastmcp import FastMCP
import configparser

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

DEBUGPY_PORT = 5678
LOG_FILE = "/tmp/odoo_server.log"
ODOO_URL = "http://localhost:8069"
ODOO_LOGIN = "admin"
ODOO_PASSWORD = "admin"

# entrypoint.sh apt-installs Odoo's own deps (debian/control, via
# setup/debinstall.sh) into /usr/lib/python3/dist-packages. Python's default
# sys.path puts /usr/local/lib/python3.10/dist-packages (pip installs) ahead
# of that, so a pip package pulled in for the MCP server itself — cryptography
# is one, transitively — silently shadows the apt version. pyOpenSSL (also
# apt-installed, imported by odoo/addons/base) is built against the apt
# cryptography's internal layout, so under the shadowed version it breaks
# with an unrelated-looking AttributeError deep inside module loading.
# Prepending this to PYTHONPATH for the odoo-bin/debugpy child only restores
# apt's priority without touching what the MCP server process itself sees.
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
    "dap": None,                # DAPClient | None
    "memray_pid": None,         # int | None — PID currently being tracked by memray attach
    "log_file": LOG_FILE,       # str — path passed via the most recent start_odoo call
}


# ===========================================================================
# DAP client
# ===========================================================================

class DAPClient:
    """Minimal DAP (Debug Adapter Protocol) client for communicating with debugpy.
`
    Messages are framed as:
        Content-Length: <N>\\r\\n\\r\\n<json body>

    Responses and events arrive interleaved on the same socket, so all received
    messages are buffered and searched by predicate in _wait_for().
    """

    def __init__(self, host: str = "localhost", port: int = DEBUGPY_PORT):
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._seq = 0
        self._raw_buf = b""
        self._msg_queue: list = []

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self._host, self._port))
        self._sock.settimeout(0.5)
        self._request("initialize", {
            "clientID": "odoo-mcp",
            "adapterID": "python",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
        })
        self._wait_for(lambda m: m.get("type") == "event" and m.get("event") == "initialized")
        self._request("configurationDone", {})

    def disconnect(self):
        if self._sock:
            try:
                self._request("disconnect", {"restart": False})
            except Exception:
                pass
            self._sock.close()
            self._sock = None

    # ------------------------------------------------------------------
    # Protocol internals
    # ------------------------------------------------------------------

    def _pump(self):
        """Read available socket bytes and parse complete DAP messages into the queue."""
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                self._raw_buf += chunk
        except (socket.timeout, BlockingIOError):
            pass

        while b"\r\n\r\n" in self._raw_buf:
            header_end = self._raw_buf.index(b"\r\n\r\n")
            header = self._raw_buf[:header_end].decode()
            length = int(header.split("Content-Length: ")[1].split("\r\n")[0])
            frame_end = header_end + 4 + length
            if len(self._raw_buf) < frame_end:
                break
            body = self._raw_buf[header_end + 4:frame_end]
            self._raw_buf = self._raw_buf[frame_end:]
            self._msg_queue.append(json.loads(body))

    def _wait_for(self, predicate, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, msg in enumerate(self._msg_queue):
                if predicate(msg):
                    self._msg_queue.pop(i)
                    return msg
            self._pump()
            time.sleep(0.05)
        raise TimeoutError("DAP timeout waiting for expected message")

    def _request(self, command: str, arguments: dict, timeout: float = 30.0) -> dict:
        self._seq += 1
        seq = self._seq
        body = json.dumps({
            "type": "request", "seq": seq,
            "command": command, "arguments": arguments,
        }).encode()
        self._sock.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        resp = self._wait_for(
            lambda m: m.get("type") == "response" and m.get("request_seq") == seq,
            timeout=timeout,
        )
        if not resp.get("success"):
            raise RuntimeError(resp.get("message", f"DAP {command} failed"))
        return resp.get("body") or {}

    # ------------------------------------------------------------------
    # High-level helpers used by MCP tools
    # ------------------------------------------------------------------

    def get_threads(self) -> list:
        return self._request("threads", {}).get("threads", [])

    def get_stack_trace(self, thread_id: int) -> list:
        return self._request("stackTrace", {
            "threadId": thread_id,
            "format": {"parameters": True, "line": True},
        }).get("stackFrames", [])

    def get_scopes(self, frame_id: int) -> list:
        return self._request("scopes", {"frameId": frame_id}).get("scopes", [])

    def get_variables(self, variables_reference: int) -> list:
        return self._request("variables", {"variablesReference": variables_reference}).get("variables", [])

    def evaluate(self, expression: str, frame_id: int) -> dict:
        return self._request("evaluate", {
            "expression": expression,
            "frameId": frame_id,
            "context": "repl",
        })

    def set_breakpoints(self, source_path: str, lines: list[int]) -> list:
        return self._request("setBreakpoints", {
            "source": {"path": source_path},
            "breakpoints": [{"line": ln} for ln in lines],
        }).get("breakpoints", [])

    def resume(self, thread_id: int) -> dict:
        return self._request("continue", {"threadId": thread_id, "singleThread": False})

    def pause(self, thread_id: int) -> dict:
        return self._request("pause", {"threadId": thread_id})


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

    Call this (with the series from get_odoo_version) before start_odoo or
    attach_debugger to ensure the source code matches the target database.
    The branch must already exist locally (e.g. '17.0', '16.0').

    Args:
        branch: Git branch name to check out, typically the first two
                dot-separated components of the Odoo version (e.g. '17.0')
    """
    repos = [
        ("odoo", ODOO_REPO),
        ("enterprise", ENTERPRISE_REPO),
    ]
    lines = []
    for name, path in repos:
        result = subprocess.run(
            ["git", "-C", path, "checkout", branch],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            lines.append(f"{name}: FAILED — {result.stderr.strip()}")
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

    try:
        conn = _db_connect(database, readonly=False)
        with conn.cursor() as cur:
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
        f"Verification found no surviving unmasked values. Other tools are now unlocked."
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
        proc = subprocess.Popen(cmd, preexec_fn=os.setsid, env=_odoo_subprocess_env())
        _state["odoo_process"] = proc
        _state["log_file"] = log_path
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
def attach_debugger(database: str, extra_args: str = "") -> str:
    """Start odoo-bin under debugpy and wait for a DAP client to attach on port 5678.

    After this call, use connect_debugger to complete the DAP handshake and
    release the --wait-for-client hold so Odoo starts executing.

    Args:
        database: Target database name
        extra_args: Additional odoo-bin flags, e.g. '-u sale'
    """
    if not _is_scrubbed(database):
        return _ACCESS_DENIED.format(database)
    proc = _state["odoo_process"]
    if proc and proc.poll() is None:
        return f"Odoo is already running (pid {proc.pid}). Call stop_odoo first."

    cmd = [
        "python3", "-m", "debugpy",
        "--listen", f"0.0.0.0:{DEBUGPY_PORT}",
        "--wait-for-client",
        ODOO_BIN,
        f"--addons-path={ODOO_ADDONS}",
        "-d", database,
        "--workers=0",
    ]
    if extra_args:
        cmd.extend(extra_args.split())

    try:
        _state["odoo_process"] = subprocess.Popen(cmd, preexec_fn=os.setsid, env=_odoo_subprocess_env())
        return (
            f"Odoo started under debugpy (pid {_state['odoo_process'].pid}), "
            f"listening on port {DEBUGPY_PORT}. Call connect_debugger to begin."
        )
    except Exception as e:
        return f"Error starting debugpy: {e}"


@mcp.tool()
def stop_odoo() -> str:
    """Stop the odoo-bin process started by start_odoo or attach_debugger."""
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
# MCP tools — DAP / debugpy
# ===========================================================================

def _dap() -> DAPClient:
    """Return the active DAPClient or raise if not connected."""
    client = _state["dap"]
    if not client or not client._sock:
        raise RuntimeError("DAP client not connected. Call connect_debugger first.")
    return client


@mcp.tool()
def connect_debugger() -> str:
    """Connect the DAP client to the running debugpy process.

    Call this after attach_debugger. Completes the DAP handshake, which
    releases the --wait-for-client hold and allows Odoo to start executing.
    """
    if _state["dap"] and _state["dap"]._sock:
        return "DAP client is already connected."
    proc = _state["odoo_process"]
    if not proc or proc.poll() is not None:
        return "No debugpy process running. Call attach_debugger first."
    try:
        client = DAPClient()
        client.connect()
        _state["dap"] = client
        return f"DAP client connected on port {DEBUGPY_PORT}. Odoo is now executing."
    except Exception as e:
        _state["dap"] = None
        return f"Error connecting DAP client: {e}"


@mcp.tool()
def set_breakpoint(file_path: str, lines: str) -> str:
    """Set breakpoints in a source file.

    Args:
        file_path: Absolute path to the Python source file
        lines: Comma-separated line numbers, e.g. '42,55,100'
    """
    try:
        line_numbers = [int(ln.strip()) for ln in lines.split(",")]
        results = _dap().set_breakpoints(file_path, line_numbers)
        parts = [
            f"  line {bp.get('line', '?')} — {'verified' if bp.get('verified') else 'unverified'}"
            for bp in results
        ]
        return f"Breakpoints set in {file_path}:\n" + "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_call_stack() -> str:
    """Get the current call stack for all threads.

    Only meaningful when the process is paused at a breakpoint or after
    pause_execution. Returns frame IDs needed by get_variables and
    evaluate_expression.
    """
    try:
        threads = _dap().get_threads()
        if not threads:
            return "No threads found."
        lines = []
        for thread in threads:
            tid = thread["id"]
            lines.append(f"Thread {tid}: {thread['name']}")
            try:
                for frame in _dap().get_stack_trace(tid):
                    source = frame.get("source", {}).get("path", "<unknown>")
                    lines.append(
                        f"  frame {frame['id']:>5}  {frame['name']}  {source}:{frame.get('line', '?')}"
                    )
            except Exception as e:
                lines.append(f"  (could not read stack: {e})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_variables(frame_id: int, scope: str = "locals") -> str:
    """Get variables at a specific stack frame.

    Args:
        frame_id: Frame ID from get_call_stack
        scope: Which scope to inspect — 'locals', 'globals', or 'closure'
    """
    try:
        scopes = _dap().get_scopes(frame_id)
        target = next((s for s in scopes if s["name"].lower() == scope.lower()), None)
        if not target:
            available = ", ".join(s["name"] for s in scopes)
            return f"Scope '{scope}' not found. Available: {available}"
        variables = _dap().get_variables(target["variablesReference"])
        if not variables:
            return f"No variables in {scope} scope."
        lines = [f"{'Name':<30} {'Type':<20} Value", "-" * 80]
        for v in variables:
            lines.append(f"{v['name']:<30} {v.get('type', ''):<20} {v['value']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def evaluate_expression(expression: str, frame_id: int) -> str:
    """Evaluate a Python expression in the context of a paused stack frame.

    Args:
        expression: Python expression, e.g. 'self.env["res.partner"].browse(1).name'
        frame_id: Frame ID from get_call_stack
    """
    try:
        result = _dap().evaluate(expression, frame_id)
        return result.get("result", "(no result)")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def resume_execution() -> str:
    """Resume execution of all paused threads."""
    try:
        threads = _dap().get_threads()
        if not threads:
            return "No threads found."
        _dap().resume(threads[0]["id"])
        return "Execution resumed."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def pause_execution() -> str:
    """Pause all running threads.

    After pausing, use get_call_stack to find frame IDs, then
    get_variables or evaluate_expression to inspect state.
    """
    try:
        threads = _dap().get_threads()
        if not threads:
            return "No threads found."
        for thread in threads:
            _dap().pause(thread["id"])
        return f"Paused {len(threads)} thread(s). Call get_call_stack to inspect."
    except Exception as e:
        return f"Error: {e}"


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


def _default_output(prefix: str, ext: str) -> str:
    return f"/tmp/{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"


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


if __name__ == "__main__":
    mcp.run()
