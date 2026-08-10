# Created by Odoo — Technical Support tooling (scrubdb).
"""Apply a Plan to a live PostgreSQL connection.

Generates the per-run key here, transforms each *distinct* source value once
(so duplicates map identically and joins survive), and applies the change with a
standard, version-independent ``UPDATE ... FROM (VALUES ...)``. Everything runs
in one transaction and the run key is never persisted — only an opaque
fingerprint is kept for the audit log.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

from . import finalize
from .policy import Action, Plan
from .transforms import Context

_logger = logging.getLogger("scrubdb.executor")

# Types psycopg2 hands back as str and that need no cast on the way in/out.
_PLAIN_TEXT_TYPES = {"character varying", "varchar", "character", "char",
                     "text", "citext", "name"}
# json/jsonb come back as Python dict/list and also need JSON-aware masking.
_JSON_TYPES = {"json", "jsonb"}
_NUMERIC_TYPES = {"integer", "bigint", "smallint", "numeric", "decimal",
                  "real", "double precision", "money"}


def _empty_literal(data_type):
    """A non-null 'empty' SQL literal for a type, or None if there's no safe one.

    Used to scrub a ``null`` transform on a NOT NULL column (can't set NULL).
    """
    if data_type in _PLAIN_TEXT_TYPES:
        return "''"
    if data_type in _JSON_TYPES:
        return f"'null'::{data_type}"   # JSON null value (not SQL NULL)
    if data_type == "bytea":
        return "''::bytea"
    if data_type in _NUMERIC_TYPES:
        return "0"
    if data_type == "boolean":
        return "false"
    return None


def _unique_suffix(old, ctx) -> str:
    """Short deterministic token, distinct per distinct original value.

    Appended to a masked value on UNIQUE/PK columns so masked values stay
    distinct (no constraint violation on --apply). Stable across the run.
    """
    return "_" + ctx.digest("uniq", old).hex()[:8]


def _map_json_strings(obj, fn, ctx):
    """Apply ``fn`` to every string *leaf* of a decoded JSON value.

    Keeps the structure (dict keys, list shape, numbers, bools, nulls) intact so
    the re-serialized result is still valid JSON — e.g. an Odoo translation blob
    ``{"en_US": "John"}`` becomes ``{"en_US": "<fake>"}``.
    """
    if isinstance(obj, str):
        return fn(obj, ctx)
    if isinstance(obj, dict):
        return {k: _map_json_strings(v, fn, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_json_strings(v, fn, ctx) for v in obj]
    return obj


def _transform_json_text(text, fn, ctx):
    """Mask the string leaves of a JSON *text* value, returning new JSON text."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # Not decodable as JSON — treat the whole thing as a scalar string.
        return fn(text, ctx)
    return json.dumps(_map_json_strings(obj, fn, ctx), ensure_ascii=False)


def connect(**params):
    """Return ``(connection, driver_name)`` using psycopg2.

    Takes discrete libpq keyword params (``dbname``/``user``/``password``/
    ``host``/``port``/``sslmode``/``application_name``); ``dbname`` is the exact
    target database. Empty/``None`` params are dropped so libpq's own ``PG*``
    environment fallback still applies to anything omitted. A single connection
    is opened — scrubdb anonymizes the named database in place and never
    creates, copies, or connects to a replica.
    """
    try:
        import psycopg2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 is not installed; install 'psycopg2' (or 'psycopg2-binary')"
        ) from exc
    kw = {k: v for k, v in params.items() if v not in (None, "")}
    return psycopg2.connect(**kw), "psycopg2"


@dataclass
class RunReport:
    dry_run: bool
    key_fingerprint: str
    driver: str = ""
    columns_changed: int = 0
    rows_updated: int = 0
    distinct_values: int = 0
    rows_deleted: int = 0
    truncated_tables: list = field(default_factory=list)
    passwords_reset: int = 0
    crons_deactivated: int = 0
    logins_normalized: int = 0
    user_list: list = field(default_factory=list)
    per_column: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "key_fingerprint": self.key_fingerprint,
            "driver": self.driver,
            "columns_changed": self.columns_changed,
            "rows_updated": self.rows_updated,
            "distinct_values": self.distinct_values,
            "rows_deleted": self.rows_deleted,
            "truncated_tables": self.truncated_tables,
            "passwords_reset": self.passwords_reset,
            "crons_deactivated": self.crons_deactivated,
            "logins_normalized": self.logins_normalized,
            # user_list is printed, not dumped (logins/passwords), kept out of JSON
            "per_column": self.per_column,
        }


def _apply_column(cur, action: Action, ctx: Context, schema: str, batch_size: int) -> tuple[int, int]:
    """Mask one column. Returns ``(rows_updated, distinct_values)``.

    For ``json``/``jsonb`` columns the round-trip is done via the ``::text``
    representation: psycopg2 returns those as Python ``dict``/``list`` (which it
    then cannot bind back as a parameter), and a masked value must remain valid
    JSON, so we select ``col::text``, transform the JSON string leaves, match on
    ``col::text``, and cast the new value back with ``::<type>``.
    """
    ident = f'"{schema}"."{action.table}"."{action.column}"'
    src = f'"{schema}"."{action.table}"'
    col = f'"{action.column}"'
    # The root/superuser (res_users id=1) is used for sudo and must never be
    # modified — guard every UPDATE/SELECT on res_users with id <> 1.
    guard = " AND id <> 1" if action.table == "res_users" else ""
    t_guard = " AND t.id <> 1" if action.table == "res_users" else ""

    # Fast path for large text: deterministic one-way redaction done entirely
    # server-side (md5 of the value + a per-run secret), so the big values are
    # never pulled to Python or shipped back — one sequential UPDATE, no join.
    # The secret is derived from the run key and discarded with it.
    if action.transform_name == "hash_text":
        secret = ctx.digest("hashtext-secret").hex()
        cur.execute(
            f"UPDATE {src} SET {col} = md5({ident}::text || %s) "
            f"WHERE {ident} IS NOT NULL{guard}",
            (secret,),
        )
        return (cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0, 0)

    # Fast path for null: a single UPDATE, no value fetch. Avoids pulling large
    # payloads (e.g. ir_attachment.db_datas bytea) over the wire just to drop them.
    if action.transform_name == "null":
        if action.is_nullable:
            cur.execute(f"UPDATE {src} SET {col} = NULL WHERE {ident} IS NOT NULL{guard}")
        else:
            # NOT NULL column — scrub with an empty value instead of NULL.
            empty = _empty_literal(action.data_type)
            if empty is None:
                _logger.warning("keeping NOT NULL %s.%s: no empty value for type %s",
                                action.table, action.column, action.data_type)
                return (0, 0)
            cur.execute(f"UPDATE {src} SET {col} = {empty} "
                        f"WHERE {ident} IS DISTINCT FROM {empty}{guard}")
        return (cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0, 0)

    is_json = action.data_type in _JSON_TYPES
    is_text = action.data_type in _PLAIN_TEXT_TYPES
    # Anything that isn't a plain text type is round-tripped through ``::text``:
    # psycopg2 returns json/date/numeric/etc. as non-str Python objects it can't
    # bind straight back, and the masked value is cast back with ``::<type>``.
    needs_cast = not is_text
    select_expr = f"{ident}::text" if needs_cast else ident
    cur.execute(f"SELECT DISTINCT {select_expr} FROM {src} WHERE {ident} IS NOT NULL{guard}")
    originals = [r[0] for r in cur.fetchall()]
    if not originals:
        return (0, 0)

    cast = f"::{action.data_type}" if needs_cast else ""
    match_expr = f"t.{col}::text" if needs_cast else f"t.{col}"

    mapping = []
    for old in originals:
        if is_json:
            new = _transform_json_text(old, action.fn, ctx)
        else:
            new = action.fn(old, ctx)
            # Keep UNIQUE/PK text columns distinct after masking.
            if action.unique and is_text and isinstance(new, str):
                new = new + _unique_suffix(old, ctx)
        mapping.append((old, new))
    # Skip pairs where the value is unchanged (e.g. keep).
    mapping = [(o, n) for (o, n) in mapping if n != o]
    if not mapping:
        return (0, len(originals))

    if len(mapping) <= batch_size:
        # Fits in one statement: a single UPDATE evaluates its SET/WHERE
        # against a consistent pre-statement snapshot, so this is inherently
        # chain-safe regardless of how the (old, new) pairs relate to each
        # other.
        rows_updated = _update_via_values(cur, src, col, cast, match_expr, t_guard, mapping)
    else:
        rows_updated = _update_via_temp_table(cur, src, col, cast, match_expr, t_guard, mapping, batch_size)

    return (rows_updated, len(originals))


def _update_via_values(cur, src, col, cast, match_expr, t_guard, mapping) -> int:
    placeholders = ",".join(["(%s, %s)"] * len(mapping))
    params: list = []
    for old, new in mapping:
        params.extend([old, new])
    cur.execute(
        f"UPDATE {src} AS t SET {col} = v.new_val{cast} "
        f"FROM (VALUES {placeholders}) AS v(old_val, new_val) "
        f"WHERE {match_expr} = v.old_val{t_guard}",
        params,
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _update_via_temp_table(cur, src, col, cast, match_expr, t_guard, mapping, batch_size) -> int:
    """Apply a mapping too large for one VALUES(...) statement as ONE UPDATE.

    More than ``batch_size`` distinct values can't share a single parameter
    list (and PostgreSQL has a hard 65535-param limit besides), so this used
    to run several sequential ``UPDATE`` statements instead. That's unsafe:
    each one sees the previous one's committed effect within the same
    transaction, so if distinct value A maps to B and B is still a pending
    *original* value in a later statement, that statement's
    ``WHERE col = B`` also catches the rows just rewritten from A to B and
    masks them a second time — occasionally cycling a row back to its own
    original value (confirmed on oes_psus's x_account_move, ~20k distinct
    values). Staging the full mapping in a temp table sidesteps this: the
    INSERTs can be chunked freely (their order has no correctness impact),
    but the actual mutation of the real table happens in exactly one
    UPDATE, so no row can ever be matched by more than one (old, new) pair.
    """
    # No PRIMARY KEY/index on old_val: a btree index entry is capped at ~1/3
    # of a page (~2.7KB), and a masked column's distinct values (e.g. a big
    # HTML/text body) can easily exceed that. Correctness doesn't need one —
    # the join can fall back to a hash join / seq scan — only avoiding
    # multiple sequential UPDATEs against the real table does.
    cur.execute("DROP TABLE IF EXISTS _scrubdb_map")
    cur.execute("CREATE TEMP TABLE _scrubdb_map (old_val text, new_val text)")
    for start in range(0, len(mapping), batch_size):
        chunk = mapping[start:start + batch_size]
        placeholders = ",".join(["(%s, %s)"] * len(chunk))
        params: list = []
        for old, new in chunk:
            params.extend([old, None if new is None else str(new)])
        cur.execute(f"INSERT INTO _scrubdb_map (old_val, new_val) VALUES {placeholders}", params)
    cur.execute(
        f"UPDATE {src} AS t SET {col} = m.new_val{cast} "
        f"FROM _scrubdb_map AS m "
        f"WHERE {match_expr} = m.old_val{t_guard}"
    )
    rows_updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    cur.execute("DROP TABLE _scrubdb_map")
    return rows_updated


def apply_plan(conn, plan: Plan, schema: str = "public",
               batch_size: int = 5000, dry_run: bool = False,
               reset_password=None, deactivate_crons: bool = False,
               normalize_logins: bool = False, list_users=None) -> RunReport:
    run_key = os.urandom(32)
    ctx = Context(run_key)
    fingerprint = hashlib.sha256(b"scrubdb-fpr\x00" + run_key).hexdigest()[:16]

    report = RunReport(dry_run=dry_run, key_fingerprint=fingerprint)
    cur = conn.cursor()
    total = len(plan.actions)
    try:
        # Empty transient (wizard / res.config.settings) tables wholesale.
        for table in getattr(plan, "truncate_tables", None) or []:
            _logger.info("truncating transient table %s", table)
            cur.execute(f'DELETE FROM "{schema}"."{table}"')
            report.rows_deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            report.truncated_tables.append(table)
        for i, action in enumerate(plan.actions, 1):
            _logger.info("[%d/%d] %s.%s → %s", i, total,
                         action.table, action.column, action.transform_name)
            rows, distinct = _apply_column(cur, action, ctx, schema, batch_size)
            _logger.debug("    %s.%s: %d rows, %d distinct values",
                          action.table, action.column, rows, distinct)
            report.columns_changed += 1
            report.rows_updated += rows
            report.distinct_values += distinct
            report.per_column.append({
                "table": action.table,
                "column": action.column,
                "transform": action.transform_name,
                "rows_updated": rows,
                "distinct_values": distinct,
            })
        # Post-mask finalization (same transaction; reads see masked values).
        if normalize_logins:
            report.logins_normalized = finalize.normalize_logins(cur, schema)
        if reset_password is not None:
            report.passwords_reset = finalize.reset_passwords(cur, schema, reset_password)
        if deactivate_crons:
            report.crons_deactivated = finalize.deactivate_crons(cur, schema)
        if list_users:
            report.user_list = finalize.list_users(cur, schema, list_users)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        # The key falls out of scope here; it is never written anywhere.
        del run_key, ctx
    return report
