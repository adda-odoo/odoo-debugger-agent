"""Phase 0 checkpoint: DB connect, version detection, info_schema helpers."""
import pytest

from odoo_obfuscate.db import (
    column_exists,
    column_info,
    connect,
    odoo_version,
    pg_version,
    table_exists,
)


def test_connect_and_pg_version(conn_str):
    conn = connect(db_url=conn_str)
    version = pg_version(conn)
    conn.close()
    # Version is a dotted string like '15.4'
    assert "." in version
    major = int(version.split(".")[0])
    assert major >= 12


def test_odoo_version_from_fixture(conn_str):
    conn = connect(db_url=conn_str)
    version = odoo_version(conn)
    conn.close()
    assert version == "17.0"


def test_table_exists(db_conn):
    assert table_exists(db_conn, "res_partner") is True
    assert table_exists(db_conn, "nonexistent_table_xyz") is False


def test_column_exists(db_conn):
    assert column_exists(db_conn, "res_partner", "email") is True
    assert column_exists(db_conn, "res_partner", "no_such_column") is False


def test_column_info_returns_type(db_conn):
    info = column_info(db_conn, "res_partner", "email")
    assert info is not None
    assert info["data_type"] in ("character varying", "text", "varchar")


def test_column_info_missing_returns_none(db_conn):
    assert column_info(db_conn, "res_partner", "definitely_not_a_column") is None
