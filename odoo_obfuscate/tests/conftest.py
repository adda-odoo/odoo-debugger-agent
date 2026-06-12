"""Session-scoped pytest fixture: drops, recreates, and seeds anon_fixture DB."""
from __future__ import annotations

import subprocess
from pathlib import Path

import psycopg2
import pytest

FIXTURE_DB = "anon_fixture"
CONN_STR = f"postgresql:///{FIXTURE_DB}"
FIXTURE_DIR = Path(__file__).parent / "fixture"


def _run_psql(sql_file: Path, dbname: str) -> None:
    result = subprocess.run(
        ["psql", "-d", dbname, "-f", str(sql_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed for {sql_file}:\n{result.stderr}")


@pytest.fixture(scope="session", autouse=True)
def fixture_db():
    """Drop and recreate anon_fixture, then load schema + seed."""
    # Drop and recreate via template1 (no active connections to anon_fixture yet)
    admin = psycopg2.connect(dbname="template1")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {FIXTURE_DB}")
        cur.execute(f"CREATE DATABASE {FIXTURE_DB}")
    admin.close()

    _run_psql(FIXTURE_DIR / "schema.sql", FIXTURE_DB)
    _run_psql(FIXTURE_DIR / "seed.sql",   FIXTURE_DB)

    yield CONN_STR


@pytest.fixture(scope="session")
def conn_str(fixture_db):
    return fixture_db


@pytest.fixture
def db_conn(conn_str):
    """Per-test psycopg2 connection; rolled back after each test."""
    conn = psycopg2.connect(conn_str)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()
