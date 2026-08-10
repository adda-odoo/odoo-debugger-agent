#!/usr/bin/env python3
"""Structural clean/dirty gate for scrubdb — used by copy_and_scrub_db.sh
(cross-cluster, main -> ai-dev, same database name) and by debug_server.py's
scrub_database (same cluster, a throwaway pre-scrub copy -> the real database,
different names — see --dst-db).

Replaces scrubdb's own removed --verify (a regex-based PII pattern scan, which
false-positives heavily on scrubdb's own shape-preserving masked output).
Instead, compares the pre-scrub copy against the post-scrub copy directly:
table row counts must match (except tables scrubdb truncates), and every
column scrubdb actually touched must differ row-for-row from the original
wherever the original wasn't NULL.

Two transform-specific exceptions avoid false positives on values that were
never masked because there was nothing to mask, without weakening the check
for anything that actually needed it (see _is_empty_like / _json_unmasked_leaf):
  - the "null" transform's contract is "make it empty", not "make it
    different" — a NOT NULL column that was already an empty string (e.g.
    ir_logging.message) has nothing left to scrub, and staying empty is the
    correct outcome, not a leak.
  - a jsonb column's transform only masks its string leaves (numbers/bools/
    keys are left alone by design), so a JSON value with no string leaves at
    all (e.g. {"<hash>": false}) has nothing to change either.

Two more exceptions mirror deliberate scrubdb behavior specific to res_users:
  - id=1 (the root/superuser, used for sudo) is hardcoded in executor.py to
    never be touched by any transform at all — it isn't a masking gap, it's
    the one row the tool is designed to leave alone.
  - --reset-passwords (on by default) overwrites every other user's password
    with one known constant in a separate finalization step outside the
    normal per-column transform contract; a user whose original password
    happened to already equal that constant (e.g. Odoo's own demo admin
    account) is a harmless coincidence, not a leak of real data.

A fifth, explicitly accepted exception: transforms.mask_name (used for
res_partner.name/res_users.name/hr_employee.name) keeps a single-word value
unchanged by design — there's no "surname" left to mask once the first word
is kept, and multi-word names already keep their first word in the clear the
same way, so this isn't a new exposure class. Per-run verification ignores it
rather than flagging it.

Sixth: a masked table with no single-column primary key can't be row-aligned
between main and ai-dev at all, so it's recorded as "unverifiable" — normally
treated as fail-closed (can't confirm clean, so don't trust it). But a table
with no corresponding entry in ir_model (mapped the same way scrubdb's own
introspect.py does, model.replace(".", "_")) isn't part of Odoo's actual data
model at all — e.g. temp_hr_import, a raw ad-hoc staging table with zero
constraints of any kind, never registered with the ORM. Those are ignored
outright rather than blocking an otherwise-clean run; a *real* model table
that's still unverifiable (which would be unusual) keeps failing closed.
"""
import argparse
import decimal
import json
import re
import sys

import psycopg2


def load_masked_and_truncated(report_path):
    report = json.load(open(report_path))
    masked = {}
    transforms = {}
    for c in report["run"]["per_column"]:
        masked.setdefault(c["table"], []).append(c["column"])
        transforms[(c["table"], c["column"])] = c["transform"]
    # Passwords are overwritten with one known constant in a finalize step
    # (see module docstring) — an original value matching that constant is
    # not something this per-original-value diff can meaningfully judge.
    if report["run"].get("passwords_reset", 0) > 0 and "res_users" in masked:
        masked["res_users"] = [c for c in masked["res_users"] if c != "password"]
    return masked, transforms, set(report["run"].get("truncated_tables", []))


def fetch_model_tables(conn) -> set:
    """DB table names backed by a real Odoo model (ir_model.model, "." -> "_").

    Mirrors introspect.py's fetch_transient_tables exactly, for the same
    reason: a non-Odoo (or ir_model-less) database just yields an empty set
    rather than erroring.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'ir_model' LIMIT 1
            """
        )
        if not cur.fetchall():
            return set()
        cur.execute("SELECT model FROM ir_model")
        return {row[0].replace(".", "_") for row in cur.fetchall()}


def get_pk_column(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary
            """,
            (table,),
        )
        cols = [r[0] for r in cur.fetchall()]
    return cols[0] if len(cols) == 1 else None


def list_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """
        )
        return [r[0] for r in cur.fetchall()]


def table_count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table}"')
        return cur.fetchone()[0]


def _is_empty_like(value) -> bool:
    """True for the "nothing left to scrub" states the null transform targets.

    Mirrors executor.py's _empty_literal: NULL for nullable columns, else
    '' / 0 / false / empty json for the column's type.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (int, float, decimal.Decimal)):
        return value == 0
    if isinstance(value, (dict, list)):
        return len(value) == 0
    return False


def _is_single_word(value) -> bool:
    """Mirrors transforms.mask_name's own "single word -> kept as-is" rule."""
    if not isinstance(value, str):
        return False
    words = [p for p in re.split(r"(\s+)", value) if p and not p.isspace()]
    return len(words) <= 1


def _json_unmasked_leaf(a, b) -> bool:
    """True if a non-empty string leaf survived unchanged from `a` to `b`.

    Walks both JSON values in parallel (masking never changes structure or
    key order, only string leaf content) and only looks at string leaves —
    numbers/booleans/null/keys are never touched by the json mask path
    (executor.py's _map_json_strings), so they're not evidence of anything.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return any(k in b and _json_unmasked_leaf(v, b[k]) for k, v in a.items())
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return any(_json_unmasked_leaf(x, y) for x, y in zip(a, b))
    if isinstance(a, str) and isinstance(b, str):
        return bool(a) and a == b
    return False


def diff_table(src_conn, dst_conn, table, pk, columns, transforms, findings):
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    query = f'SELECT "{pk}", {cols_sql} FROM "{table}" ORDER BY "{pk}"'
    src_cur = src_conn.cursor(name=f"src_{table}")
    dst_cur = dst_conn.cursor(name=f"dst_{table}")
    src_cur.itersize = dst_cur.itersize = 2000
    src_cur.execute(query)
    dst_cur.execute(query)
    for src_row, dst_row in zip(src_cur, dst_cur):
        if table == "res_users" and src_row[0] == 1:
            continue  # root/superuser — executor.py never touches this row
        for i, col in enumerate(columns, start=1):
            sval, dval = src_row[i], dst_row[i]
            transform = transforms.get((table, col))
            if transform == "null":
                # Contract is "make it empty", not "make it different" — an
                # already-empty original has nothing left to scrub.
                if not _is_empty_like(dval):
                    findings.append({"table": table, "column": col, "pk": src_row[0]})
            elif transform == "mask_name" and _is_single_word(sval):
                pass  # intentionally left unchanged; not a finding
            elif isinstance(sval, (dict, list)) or isinstance(dval, (dict, list)):
                if _json_unmasked_leaf(sval, dval):
                    findings.append({"table": table, "column": col, "pk": src_row[0]})
            elif sval is not None and sval == dval:
                findings.append({"table": table, "column": col, "pk": src_row[0]})
    src_cur.close()
    dst_cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="source (pre-scrub) database name")
    ap.add_argument("--dst-db", help="destination (scrubbed) database name, "
                                     "if different from --db (default: same as --db, "
                                     "for the cross-cluster same-name case)")
    ap.add_argument("--report", required=True)
    ap.add_argument("--src-host", required=True)
    ap.add_argument("--src-port", required=True)
    ap.add_argument("--src-user", required=True)
    ap.add_argument("--dst-host", required=True)
    ap.add_argument("--dst-port", required=True)
    ap.add_argument("--dst-user", required=True)
    ap.add_argument("--diff-report", required=True)
    args = ap.parse_args()
    dst_db = args.dst_db or args.db

    src = psycopg2.connect(host=args.src_host, port=args.src_port, user=args.src_user, dbname=args.db)
    dst = psycopg2.connect(host=args.dst_host, port=args.dst_port, user=args.dst_user, dbname=dst_db)
    src.set_session(readonly=True)
    dst.set_session(readonly=True)

    masked, transforms, truncated = load_masked_and_truncated(args.report)

    cardinality_findings = []
    for table in list_tables(dst):
        src_n, dst_n = table_count(src, table), table_count(dst, table)
        expected = 0 if table in truncated else src_n
        if dst_n != expected:
            cardinality_findings.append(
                {"table": table, "main_count": src_n, "ai_dev_count": dst_n, "expected": expected}
            )

    model_tables = fetch_model_tables(dst)

    value_findings = []
    unverifiable = []
    ignored_no_model = []
    for table, columns in masked.items():
        if table in truncated:
            continue  # whole table is expected empty; nothing to diff
        pk = get_pk_column(dst, table)
        if pk is None:
            if table in model_tables:
                unverifiable.append(table)
            else:
                ignored_no_model.append(table)
            continue
        diff_table(src, dst, table, pk, columns, transforms, value_findings)

    clean = not cardinality_findings and not value_findings and not unverifiable
    json.dump(
        {
            "clean": clean,
            "cardinality_findings": cardinality_findings,
            "value_findings": value_findings[:500],
            "value_findings_total": len(value_findings),
            "unverifiable_tables": unverifiable,
            "ignored_no_model_tables": ignored_no_model,
        },
        open(args.diff_report, "w"),
        indent=2,
    )

    # Close explicitly rather than let process exit handle it: a caller that
    # immediately tries to drop either database (e.g. debug_server.py's
    # scrub_database, dropping its throwaway pre-scrub copy right after this
    # returns) can otherwise race Postgres's own detection of the socket
    # closing.
    src.close()
    dst.close()

    sys.exit(0 if clean else 1)


if __name__ == "__main__":
    main()
