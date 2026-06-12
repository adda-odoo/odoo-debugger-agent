"""Integration tests for Phase 5 — filestore scrubbing."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import psycopg2
import pytest

from odoo_obfuscate import filestore


# Filenames we pre-write to the tmp filestore. The DB rows in the seed already
# reference 'ab/abc...', 'cd/cde...', 'ef/efg...' — we materialize those plus
# extras for the UI-asset and orphan cases this test introduces.
_REAL_FILES = {
    "ab/abc123def456":           b"FAKE invoice PDF bytes for John",
    "cd/cde234efg567":           b"FAKE JPEG photo of John",
    "ef/efg345ghi678":           b"FAKE contract PDF for Acme",
    "11/ui_view_asset_keep":     b"FAKE compiled CSS bundle - must survive",
    "22/website_logo_keep":      b"FAKE company logo - must survive",
    "33/orphan_no_db_row":       b"FAKE orphan file - no ir_attachment row",
}


@pytest.fixture(scope="module")
def scrubbed_filestore(conn_str, tmp_path_factory):
    """Pre-write a fake filestore, insert UI-asset + url-type rows, run the
    filestore scrub, then yield (conn, filestore_root) for assertions."""
    fs_root: pathlib.Path = tmp_path_factory.mktemp("filestore")

    # Materialize files described by _REAL_FILES (creating nested dirs).
    for rel, content in _REAL_FILES.items():
        target = fs_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    conn = psycopg2.connect(conn_str)
    conn.autocommit = False

    # Add: a UI-asset attachment + a type='url' attachment + an orphan.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ir_attachment "
            "(id, name, store_fname, mimetype, res_model, res_id, type, url) VALUES "
            "(10, 'theme.css',    '11/ui_view_asset_keep', 'text/css',  'ir.ui.view',    1, 'binary', NULL), "
            "(11, 'logo.png',     '22/website_logo_keep',  'image/png', 'website',       1, 'binary', NULL), "
            "(12, 'external_doc', NULL, NULL, 'crm.lead', 1, 'url', 'https://customer.example.com/contract.pdf')"
        )
    conn.commit()

    args = SimpleNamespace(filestore=str(fs_root))
    filestore.run_filestore_scrub(conn, args, dbname="anon_fixture")

    yield conn, fs_root
    conn.close()


# ─── SQL outcomes ───────────────────────────────────────────────────────────

def test_db_datas_nulled_for_non_ui_assets(scrubbed_filestore):
    conn, _ = scrubbed_filestore
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM ir_attachment "
            "WHERE db_datas IS NOT NULL "
            "AND res_model NOT IN ('ir.ui.view','website','res.company','website.page')"
        )
        assert cur.fetchone()[0] == 0


def test_store_fname_redirected_to_dummy(scrubbed_filestore):
    conn, _ = scrubbed_filestore
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, store_fname FROM ir_attachment "
            "WHERE id IN (1, 2, 3) ORDER BY id"
        )
        rows = cur.fetchall()
    # id=1 is application/pdf → blank.pdf
    # id=2 is image/jpeg      → blank.png
    # id=3 is application/pdf → blank.pdf
    assert dict(rows)[1] == "dummy/blank.pdf"
    assert dict(rows)[2] == "dummy/blank.png"
    assert dict(rows)[3] == "dummy/blank.pdf"


def test_ui_asset_rows_preserved(scrubbed_filestore):
    """ir.ui.view and website rows must keep their original store_fname."""
    conn, _ = scrubbed_filestore
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, store_fname FROM ir_attachment WHERE id IN (10, 11) ORDER BY id"
        )
        rows = dict(cur.fetchall())
    assert rows[10] == "11/ui_view_asset_keep"
    assert rows[11] == "22/website_logo_keep"


def test_url_attachment_redirected(scrubbed_filestore):
    conn, _ = scrubbed_filestore
    with conn.cursor() as cur:
        cur.execute("SELECT url FROM ir_attachment WHERE id = 12")
        assert cur.fetchone()[0] == "/web/static/src/img/placeholder.png"


# ─── Disk outcomes ──────────────────────────────────────────────────────────

def test_dummy_files_created(scrubbed_filestore):
    _, fs_root = scrubbed_filestore
    assert (fs_root / "dummy" / "blank.pdf").exists()
    assert (fs_root / "dummy" / "blank.png").exists()
    # PDF and PNG signatures
    assert (fs_root / "dummy" / "blank.pdf").read_bytes().startswith(b"%PDF-")
    assert (fs_root / "dummy" / "blank.png").read_bytes().startswith(b"\x89PNG")


def test_ui_asset_files_preserved_on_disk(scrubbed_filestore):
    """Files referenced by UI-asset rows must survive disk delete."""
    _, fs_root = scrubbed_filestore
    assert (fs_root / "11" / "ui_view_asset_keep").exists()
    assert (fs_root / "22" / "website_logo_keep").exists()


def test_real_customer_files_deleted(scrubbed_filestore):
    """Original non-UI-asset files must be gone — even the orphan."""
    _, fs_root = scrubbed_filestore
    assert not (fs_root / "ab" / "abc123def456").exists()
    assert not (fs_root / "cd" / "cde234efg567").exists()
    assert not (fs_root / "ef" / "efg345ghi678").exists()
    assert not (fs_root / "33" / "orphan_no_db_row").exists()


# ─── Non-local storage detection ────────────────────────────────────────────

def test_s3_storage_aborts(conn_str):
    """If ir_config_parameter signals S3 backend, scrub aborts cleanly."""
    conn = psycopg2.connect(conn_str)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ir_config_parameter (key, value) VALUES "
                "('ir_attachment.location', 's3://bucket/path') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            )
        conn.commit()
        with pytest.raises(filestore.NonLocalFilestoreError):
            filestore.check_non_local_storage(conn)
    finally:
        # Cleanup so other tests aren't affected.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ir_config_parameter WHERE key = 'ir_attachment.location'"
            )
        conn.commit()
        conn.close()


# ─── Path detection ─────────────────────────────────────────────────────────

def test_detect_filestore_path_explicit(tmp_path):
    """--filestore CLI arg wins when the path exists."""
    args = SimpleNamespace(filestore=str(tmp_path))
    result = filestore.detect_filestore_path(args, dbname="anywhere")
    assert result == tmp_path


def test_detect_filestore_path_returns_none_when_missing(tmp_path):
    """Non-existent --filestore + no fallback paths → returns None."""
    bogus = tmp_path / "does_not_exist"
    args = SimpleNamespace(filestore=str(bogus))
    result = filestore.detect_filestore_path(args, dbname="nonexistent_db_xyz")
    assert result is None
