"""Database connection and helpers."""
from __future__ import annotations

import psycopg2
import psycopg2.extras


def connect(dbname: str | None = None, db_url: str | None = None) -> psycopg2.extensions.connection:
    """Return a psycopg2 connection using peer auth (dbname) or a full URL."""
    if db_url:
        conn = psycopg2.connect(db_url)
    elif dbname:
        conn = psycopg2.connect(dbname=dbname)
    else:
        raise ValueError("Either dbname or db_url must be provided")
    conn.autocommit = False
    return conn


def pg_version(conn: psycopg2.extensions.connection) -> str:
    """Return PostgreSQL server version as a dotted string, e.g. '15.4'."""
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        raw: str = cur.fetchone()[0]
    # 'PostgreSQL 15.4 on ...' → '15.4'
    parts = raw.split()
    return parts[1] if len(parts) >= 2 else raw


def odoo_version(conn: psycopg2.extensions.connection) -> str | None:
    """Return Odoo series as a string (e.g. '17.0') or None if not detectable."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT latest_version FROM ir_module_module "
                "WHERE name = 'base' AND state = 'installed' LIMIT 1"
            )
            row = cur.fetchone()
        except psycopg2.Error:
            conn.rollback()
            return None
    if not row or not row[0]:
        return None
    # latest_version is e.g. '17.0.1.3' — return the major.minor part
    parts = str(row[0]).split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return str(row[0])


def column_info(
    conn: psycopg2.extensions.connection,
    table: str,
    column: str,
) -> dict | None:
    """Return information_schema metadata for a column, or None if absent."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT data_type, is_nullable, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
            """,
            (table, column),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def column_exists(
    conn: psycopg2.extensions.connection,
    table: str,
    column: str,
) -> bool:
    return column_info(conn, table, column) is not None


def table_exists(conn: psycopg2.extensions.connection, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return cur.fetchone() is not None


def jsonb_keys(
    conn: psycopg2.extensions.connection,
    table: str,
    column: str,
) -> list[str]:
    """Return distinct top-level keys observed across all non-null values in a JSONB column."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT jsonb_object_keys({column}) FROM {table} WHERE {column} IS NOT NULL"
        )
        return [row[0] for row in cur.fetchall()]
