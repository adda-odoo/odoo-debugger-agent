#!/usr/bin/env python3
"""Per-database scrub configuration registry on the host's main cluster.

The registry is a small PostgreSQL database (`scrubdb_registry`, on the main
cluster) that holds, per database, the allowlist and scrub rules a user entered
and an append-only snapshot of every scrub run. It replaces editing the shared
`scrub_allowlist.txt` / `odoo_common.yml` in place or dropping loose
`scrub_rules/<db>.yml` files, so a database's config survives scrubbing other
databases and a re-scrub can start from — or edit — what was last applied.

It is driven by `copy_and_scrub_db.sh` / `start.sh`, which run on the host (the
only side that can reach the main cluster). Three subcommands:

  init          create the registry database (if missing) and its tables.
  collect       open $EDITOR prefilled with this database's last run — or the
                shared defaults for a first scrub — and write the resolved
                allowlist.txt + ruleset.yml into --out-dir for scrubdb to use.
  record-run    append an immutable snapshot of what a run applied, and update
                the per-database summary row.

Connection coordinates are passed explicitly (--host/--port/--user/--dbname)
rather than read from PG* env vars, since the caller re-exports those to point
at the ai-dev copy being scrubbed.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile

import psycopg2
from psycopg2.extras import Json

# Seed the editor on a database's first scrub, when there is no prior run yet.
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ALLOWLIST = os.path.join(
    _HERE, "..", "scrubdb-poc", "tools", "scrubdb", "rules", "scrub_allowlist.txt"
)
DEFAULT_RULESET = os.path.join(
    _HERE, "..", "scrubdb-poc", "tools", "scrubdb", "rules", "odoo_common.yml"
)

ALLOWLIST_MARKER = "# ===== ALLOWLIST (one glob per line) ====="
RULES_MARKER = "# ===== SCRUB RULES (YAML) ====="

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrub_database (
    db_name                   text PRIMARY KEY,
    first_scrubbed_at         timestamptz,
    last_run_id               bigint,
    last_status               text,
    last_source_odoo_version  text,
    run_count                 integer NOT NULL DEFAULT 0,
    updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scrub_run (
    run_id               bigserial PRIMARY KEY,
    db_name              text NOT NULL,
    started_at           timestamptz,
    finished_at          timestamptz NOT NULL DEFAULT now(),
    status               text NOT NULL CHECK (status IN ('success', 'failed')),
    source_odoo_version  text,
    scrubdb_report_path  text,
    diff_report_path     text,
    initiated_by         text
);
CREATE INDEX IF NOT EXISTS scrub_run_db_name_idx ON scrub_run (db_name);

CREATE TABLE IF NOT EXISTS scrub_run_allowlist (
    run_id   bigint NOT NULL REFERENCES scrub_run (run_id) ON DELETE CASCADE,
    seq      integer NOT NULL,
    pattern  text NOT NULL,
    comment  text,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS scrub_run_rule (
    run_id       bigint NOT NULL REFERENCES scrub_run (run_id) ON DELETE CASCADE,
    table_name   text NOT NULL,
    column_name  text NOT NULL,
    transform    jsonb,
    comment      text,
    PRIMARY KEY (run_id, table_name, column_name)
);
"""


def connect(args, dbname=None):
    """Open a connection to `dbname` (default args.dbname) on the registry cluster."""
    return psycopg2.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        dbname=dbname or args.dbname,
    )


# --- allowlist / ruleset file <-> rows -------------------------------------

def parse_allowlist(text):
    """Return the glob patterns in an allowlist file body (skip blanks/comments)."""
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def parse_ruleset(text):
    """Return the {table: {column: transform}} mapping from a ruleset YAML body."""
    import yaml

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict) or "rules" not in data:
        raise ValueError("ruleset must be a mapping with a top-level 'rules:' key")
    rules = data.get("rules") or {}
    if not isinstance(rules, dict):
        raise ValueError("'rules' must be a mapping of table -> {column: transform}")
    return rules


def read_default_allowlist():
    with open(DEFAULT_ALLOWLIST, "r", encoding="utf-8") as fh:
        return parse_allowlist(fh.read())


def read_default_ruleset():
    with open(DEFAULT_RULESET, "r", encoding="utf-8") as fh:
        return parse_ruleset(fh.read())


def dump_ruleset_yaml(rules):
    import yaml

    return yaml.safe_dump({"rules": rules}, sort_keys=False)


# --- init -------------------------------------------------------------------

def cmd_init(args):
    """Create the registry database (if missing) and its tables. Idempotent."""
    created = False
    # CREATE DATABASE cannot run inside a transaction block, so the maintenance
    # connection uses autocommit and is managed explicitly (psycopg2's `with`
    # context manager would wrap the statements in a transaction).
    conn = connect(args, dbname="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.dbname,))
            if cur.fetchone() is None:
                try:
                    cur.execute(f'CREATE DATABASE "{args.dbname}"')
                    created = True
                except psycopg2.Error as exc:
                    sys.stderr.write(
                        f"Could not create registry database '{args.dbname}': {exc}\n"
                        f"Create it once by hand, then re-run:\n"
                        f"  createdb -h {args.host} -p {args.port} -U {args.user} "
                        f"{args.dbname}\n"
                    )
                    return 1
    finally:
        conn.close()

    with connect(args) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()

    print(
        f"Registry ready: {args.dbname} on {args.host}:{args.port}"
        + (" (created)" if created else "")
    )
    return 0


# --- collect ----------------------------------------------------------------

def latest_run_config(conn, db_name):
    """Return (patterns, rules) from this database's most recent run, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id FROM scrub_run WHERE db_name = %s "
            "ORDER BY run_id DESC LIMIT 1",
            (db_name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        run_id = row[0]

        cur.execute(
            "SELECT pattern FROM scrub_run_allowlist WHERE run_id = %s ORDER BY seq",
            (run_id,),
        )
        patterns = [r[0] for r in cur.fetchall()]

        cur.execute(
            "SELECT table_name, column_name, transform FROM scrub_run_rule "
            "WHERE run_id = %s ORDER BY table_name, column_name",
            (run_id,),
        )
        rules = {}
        for table, column, transform in cur.fetchall():
            rules.setdefault(table, {})[column] = transform
    return patterns, rules


def build_template(patterns, rules):
    lines = [
        ALLOWLIST_MARKER,
        "# Columns scrubdb should leave untouched. One glob per line;",
        "# matches a bare column name or an exact table.column.",
        "",
    ]
    lines.extend(patterns)
    lines.extend(
        [
            "",
            RULES_MARKER,
            "# Per-column transforms applied to this database. Seeded from",
            "# odoo_common.yml on a first scrub; edit freely. A column left out",
            "# still gets scrubdb's automatic type-based masking.",
            "",
            dump_ruleset_yaml(rules).rstrip("\n"),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def split_template(text):
    """Split an edited template back into (allowlist_body, rules_body)."""
    if RULES_MARKER not in text:
        raise ValueError(f"missing section marker: {RULES_MARKER!r}")
    allow_part, rules_part = text.split(RULES_MARKER, 1)
    allow_part = allow_part.replace(ALLOWLIST_MARKER, "", 1)
    return allow_part, rules_part


def open_in_editor(initial_text):
    """Write `initial_text` to a temp file, open $EDITOR on it, return the result."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        for candidate in ("sensible-editor", "nano", "vi"):
            if _which(candidate):
                editor = candidate
                break
    if not editor:
        raise RuntimeError("no editor found; set $EDITOR or install nano/vi")

    # $EDITOR/$VISUAL may carry arguments (e.g. "code --wait"), so split it the
    # way a shell would rather than treat the whole string as one executable.
    cmd = shlex.split(editor)

    fd, path = tempfile.mkstemp(prefix="scrub-config-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(initial_text)
        try:
            rc = subprocess.call(cmd + [path])
        except OSError as exc:
            raise RuntimeError(f"could not launch editor {editor!r}: {exc}") from exc
        if rc != 0:
            raise RuntimeError(f"editor exited with status {rc}; aborting")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    finally:
        os.unlink(path)


def _which(name):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def cmd_collect(args):
    """Prompt for this database's allowlist + rules and write them for scrubdb."""
    with connect(args) as conn:
        seed = latest_run_config(conn, args.db)

    if seed is not None:
        patterns, rules = seed
        sys.stderr.write(f"Seeding editor from the last recorded run of '{args.db}'.\n")
    else:
        patterns, rules = read_default_allowlist(), read_default_ruleset()
        sys.stderr.write(
            f"No prior run for '{args.db}'; seeding editor from the shared defaults.\n"
        )

    edited = open_in_editor(build_template(patterns, rules))
    allow_body, rules_body = split_template(edited)

    patterns = parse_allowlist(allow_body)
    try:
        rules = parse_ruleset(rules_body)
    except ValueError as exc:
        sys.stderr.write(f"Invalid rules: {exc}\n")
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    allowlist_path = os.path.join(args.out_dir, "allowlist.txt")
    ruleset_path = os.path.join(args.out_dir, "ruleset.yml")

    with open(allowlist_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(patterns) + ("\n" if patterns else ""))
    with open(ruleset_path, "w", encoding="utf-8") as fh:
        fh.write(dump_ruleset_yaml(rules))

    # Printed for logging only; the caller reads these by their fixed paths.
    print(allowlist_path)
    print(ruleset_path)
    return 0


# --- record-run -------------------------------------------------------------

def cmd_record_run(args):
    """Append an immutable snapshot of one run and refresh the summary row."""
    with open(args.allowlist, "r", encoding="utf-8") as fh:
        patterns = parse_allowlist(fh.read())
    with open(args.ruleset, "r", encoding="utf-8") as fh:
        rules = parse_ruleset(fh.read())

    initiated_by = args.initiated_by or os.environ.get("USER")

    with connect(args) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scrub_run (db_name, started_at, status, "
                "source_odoo_version, scrubdb_report_path, diff_report_path, "
                "initiated_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING run_id",
                (
                    args.db,
                    args.started_at or None,
                    args.status,
                    args.odoo_version or None,
                    args.report or None,
                    args.diff_report or None,
                    initiated_by,
                ),
            )
            run_id = cur.fetchone()[0]

            for seq, pattern in enumerate(patterns):
                cur.execute(
                    "INSERT INTO scrub_run_allowlist (run_id, seq, pattern) "
                    "VALUES (%s, %s, %s)",
                    (run_id, seq, pattern),
                )

            for table, cols in rules.items():
                for column, transform in (cols or {}).items():
                    cur.execute(
                        "INSERT INTO scrub_run_rule (run_id, table_name, "
                        "column_name, transform) VALUES (%s, %s, %s, %s)",
                        (run_id, table, column, Json(transform)),
                    )

            cur.execute(
                "INSERT INTO scrub_database (db_name, first_scrubbed_at, "
                "last_run_id, last_status, last_source_odoo_version, "
                "run_count, updated_at) "
                "VALUES (%s, now(), %s, %s, %s, 1, now()) "
                "ON CONFLICT (db_name) DO UPDATE SET "
                "last_run_id = EXCLUDED.last_run_id, "
                "last_status = EXCLUDED.last_status, "
                "last_source_odoo_version = EXCLUDED.last_source_odoo_version, "
                "run_count = scrub_database.run_count + 1, "
                "updated_at = now()",
                (args.db, run_id, args.status, args.odoo_version or None),
            )
        conn.commit()

    print(f"Recorded run {run_id} for '{args.db}' (status={args.status}).")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_conn(p):
        p.add_argument("--host", default="/var/run/postgresql")
        p.add_argument("--port", default="5432")
        p.add_argument("--user", default="odoo")
        p.add_argument("--dbname", default="scrubdb_registry")

    p_init = sub.add_parser("init", help="create the registry database and tables")
    add_conn(p_init)
    p_init.set_defaults(func=cmd_init)

    p_collect = sub.add_parser(
        "collect", help="edit a database's allowlist + rules and write them out"
    )
    add_conn(p_collect)
    p_collect.add_argument("db", help="database name being scrubbed")
    p_collect.add_argument(
        "--out-dir", required=True, help="directory for allowlist.txt + ruleset.yml"
    )
    p_collect.set_defaults(func=cmd_collect)

    p_record = sub.add_parser("record-run", help="append a run snapshot")
    add_conn(p_record)
    p_record.add_argument("db")
    p_record.add_argument("--status", required=True, choices=["success", "failed"])
    p_record.add_argument("--allowlist", required=True)
    p_record.add_argument("--ruleset", required=True)
    p_record.add_argument("--odoo-version", dest="odoo_version")
    p_record.add_argument("--report")
    p_record.add_argument("--diff-report")
    p_record.add_argument("--started-at", dest="started_at")
    p_record.add_argument("--initiated-by", dest="initiated_by")
    p_record.set_defaults(func=cmd_record_run)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as exc:
        sys.stderr.write(f"scrub_registry: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("scrub_registry: aborted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
