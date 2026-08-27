# Created by Odoo — Technical Support tooling (scrubdb).
"""Version-independent schema discovery via ``information_schema``.

Deliberately avoids ``pg_catalog`` and any version branching so it behaves the
same on every PostgreSQL version. Enumerates *every* column, including custom
development fields and columns orphaned by version upgrades — deciding which of
them hold PII is classify.py's job, not this module's.
"""
from __future__ import annotations

from dataclasses import dataclass

# ``data_type`` values (information_schema) that can carry text-shaped PII.
TEXTUAL_TYPES = {
    "character varying",
    "varchar",
    "character",
    "char",
    "text",
    "citext",
    "name",
    "json",
    "jsonb",
    "xml",
}


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    data_type: str
    is_nullable: bool

    @property
    def is_textual(self) -> bool:
        return self.data_type in TEXTUAL_TYPES


def fetch_columns(cur, schema: str = "public") -> list[Column]:
    # Restrict to BASE TABLE: information_schema.columns also lists VIEW columns
    # (Odoo has SQL views such as hr_employee_public, sale_report, ...), and
    # those are not updatable — masking must only touch real tables.
    cur.execute(
        """
        SELECT c.table_name, c.column_name, c.data_type, c.is_nullable
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema
         AND t.table_name = c.table_name
        WHERE c.table_schema = %s
          AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
        """,
        (schema,),
    )
    return [
        Column(table=t, name=c, data_type=d, is_nullable=(n == "YES"))
        for (t, c, d, n) in cur.fetchall()
    ]


def fetch_tables(cur, schema: str = "public") -> list[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_foreign_key_columns(cur, schema: str = "public") -> set[tuple[str, str]]:
    """Return every ``(table, column)`` involved in a foreign key.

    Includes both the *referencing* columns and the *referenced* columns, so
    blanket masking can keep both sides untouched and referential integrity
    survives. Version-independent (``information_schema`` only).
    """
    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name,
               ccu.table_name AS ref_table, ccu.column_name AS ref_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
         AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %s
        """,
        (schema,),
    )
    cols: set[tuple[str, str]] = set()
    for table, col, ref_table, ref_col in cur.fetchall():
        cols.add((table, col))
        cols.add((ref_table, ref_col))
    return cols


def fetch_unique_columns(cur, schema: str = "public"):
    """Return ``(single, composite)`` sets of ``(table, column)``.

    * ``single`` — columns that are the *sole* column of a UNIQUE/PRIMARY KEY
      constraint. A per-value masked replacement can preserve distinctness here.
    * ``composite`` — columns that participate in a *multi-column* unique
      constraint. Masking any one participant can violate the constraint, so
      these are kept.

    Constraint-based only — unique *indexes* created outside a constraint are
    not reported (that needs pg_catalog, which we deliberately avoid).
    """
    cur.execute(
        """
        SELECT tc.constraint_name, tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY')
          AND tc.table_schema = %s
        """,
        (schema,),
    )
    by_constraint: dict[tuple, list[tuple[str, str]]] = {}
    for cname, table, col in cur.fetchall():
        by_constraint.setdefault((table, cname), []).append((table, col))
    single: set[tuple[str, str]] = set()
    composite: set[tuple[str, str]] = set()
    for cols in by_constraint.values():
        if len(cols) == 1:
            single.add(cols[0])
        else:
            composite.update(cols)
    return single, composite


def fetch_transient_tables(cur, schema: str = "public") -> set[str]:
    """Return DB table names of Odoo TransientModels (wizards, res.config.settings).

    Transient models hold ephemeral wizard data (auto-vacuumed) that mirrors
    configuration — masking them is pointless and can corrupt config. Odoo-specific:
    reads ``ir_model.transient``; returns an empty set on a non-Odoo database
    (checked first via information_schema so a missing table can't abort the
    transaction). Model names are mapped to tables by ``.`` → ``_``.
    """
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = 'ir_model' LIMIT 1
        """,
        (schema,),
    )
    if not cur.fetchall():
        return set()
    cur.execute("SELECT model FROM ir_model WHERE transient IS TRUE")
    return {row[0].replace(".", "_") for row in cur.fetchall()}


def fetch_transient_relation_tables(cur, transient_tables: set[str], schema: str = "public") -> set[str]:
    """Odoo many2many relation tables a CASCADE FK empties along with a wizard.

    A many2many relation table has no ``ir_model`` entry of its own (a pure
    link table, unlike ``fetch_transient_tables``'s targets) and isn't itself
    a TransientModel — so nothing would otherwise tell the executor to expect
    it empty. But Odoo's own ``_auto_init`` gives every such table exactly two
    NOT NULL foreign-key columns and nothing else, both ``ON DELETE CASCADE``.
    Deleting every row of a TransientModel wizard on one side of that FK
    therefore cascades and empties this table completely too — the same
    end state as if it had been truncated directly (confirmed live: 8 wizard
    relation tables, e.g. ``account_move_validate_account_move_rel``, went to
    0 rows once their wizard side was cleared, and
    ``verify_scrub_diff.py``'s cardinality check flagged the "unexpected" row
    loss since nothing had told it to expect it). Folding these into the same
    set the caller unions into ``transient_tables`` lets the executor
    truncate them explicitly (a no-op given the cascade already emptied them)
    and the diff check accept the result as expected rather than a regression.
    """
    cur.execute(
        """
        SELECT c.table_name, c.column_name, c.is_nullable
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE'
        """,
        (schema,),
    )
    columns_by_table: dict[str, list[tuple[str, bool]]] = {}
    for table, col, nullable in cur.fetchall():
        columns_by_table.setdefault(table, []).append((col, nullable == "YES"))

    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name, ccu.table_name, rc.delete_rule
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        JOIN information_schema.referential_constraints rc
          ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
        """,
        (schema,),
    )
    fks_by_table: dict[str, list[tuple[str, str, str]]] = {}
    for table, col, ref_table, delete_rule in cur.fetchall():
        fks_by_table.setdefault(table, []).append((col, ref_table, delete_rule))

    result: set[str] = set()
    for table, cols in columns_by_table.items():
        if len(cols) != 2 or not all(is_nullable is False for _, is_nullable in cols):
            continue
        fks = fks_by_table.get(table, [])
        if len(fks) != 2 or {c for c, _, _ in fks} != {c for c, _ in cols}:
            continue
        if any(rule != "CASCADE" for _, _, rule in fks):
            continue
        if any(ref_table in transient_tables for _, ref_table, _ in fks):
            result.add(table)
    return result


def fetch_selection_columns(cur, schema: str = "public") -> set[tuple[str, str]]:
    """Return ``(table, column)`` for every Odoo Selection field.

    Selection fields are stored as varchar with a fixed set of values the ORM
    validates in Python (no DB CHECK), so masking one to an arbitrary string
    breaks the ORM (``Wrong value for ...``). They aren't a schema signal, but
    Odoo records them in ``ir_model_fields.ttype = 'selection'``; map model →
    table (``.`` → ``_``). Odoo-specific: returns empty set on a non-Odoo DB
    (ir_model_fields absent), checked first so it can't abort the transaction.
    """
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = 'ir_model_fields' LIMIT 1
        """,
        (schema,),
    )
    if not cur.fetchall():
        return set()
    cur.execute("SELECT model, name FROM ir_model_fields WHERE ttype = 'selection'")
    return {(model.replace(".", "_"), name) for model, name in cur.fetchall()}


def fetch_check_constraint_columns(cur, schema: str = "public") -> set[tuple[str, str]]:
    """Return ``(table, column)`` for every column under a real CHECK constraint.

    These often encode enum/format rules the app relies on, so blanket masking
    keeps them. NOT NULL constraints (which PostgreSQL also exposes as CHECKs
    named ``*_not_null`` with a ``... IS NOT NULL`` clause) are filtered out —
    otherwise every NOT NULL column would be excluded. ``information_schema``
    only, so version-independent.
    """
    cur.execute(
        """
        SELECT ccu.table_name, ccu.column_name
        FROM information_schema.check_constraints cc
        JOIN information_schema.constraint_column_usage ccu
          ON cc.constraint_name = ccu.constraint_name
         AND cc.constraint_schema = ccu.table_schema
        WHERE cc.constraint_schema = %s
          AND cc.check_clause NOT LIKE '%%IS NOT NULL%%'
        """,
        (schema,),
    )
    return {(t, c) for (t, c) in cur.fetchall()}


def fetch_primary_keys(cur, schema: str = "public") -> dict[str, list[str]]:
    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %s
        ORDER BY tc.table_name, kcu.ordinal_position
        """,
        (schema,),
    )
    pks: dict[str, list[str]] = {}
    for table, col in cur.fetchall():
        pks.setdefault(table, []).append(col)
    return pks
