"""Filestore scrubbing — ir_attachment + on-disk file replacement (Phase 5).

PLAN.md Step 8. Two sides:
  - SQL: nullify binary columns, redirect store_fname to dummy paths,
         replace type='url' attachments with a placeholder URL.
  - Disk: pre-create dummy files, then delete every real file in the
          filestore that isn't a dummy and isn't linked to a UI-asset.

Run order matters: dummies must exist on disk BEFORE we redirect store_fname
to them, otherwise Odoo serves 404s.
"""
from __future__ import annotations

import configparser
import logging
import os
import pathlib
from typing import Optional

from db import column_info, table_exists
from infra import _safe_op, _update

log = logging.getLogger(__name__)

# Models whose attachments are UI assets — never touch.
_UI_ASSET_MODELS = ("ir.ui.view", "website", "res.company", "website.page")

# Non-local storage backend markers in ir_config_parameter.
_NON_LOCAL_KEYS = (
    "ir_attachment.location",
    "cloud_storage_provider", "backup_storage_type",
    "backup_s3_bucket", "backup_azure_container", "backup_gcs_project",
)
_NON_LOCAL_VALUE_PREFIXES = ("s3://", "azure://", "azure", "gs://", "https://")

# Common odoo.conf search locations.
_ODOO_CONF_PATHS = (
    "/etc/odoo/odoo.conf",
    "/etc/odoo.conf",
    "~/.odoorc",
    "~/odoo.conf",
)

# Minimal valid binary blobs for dummy files.
_BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f\n"
    b"0000000009 00000 n\n0000000053 00000 n\n0000000102 00000 n\n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF\n"
)
# 1x1 transparent PNG, 67 bytes — used for all image MIME types. Hand-crafting
# a minimal valid JPEG is fragile and the browser sniffs content anyway, so
# we route image/jpeg → blank.png too.
_BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c626001000000050001a5f645400000000049454e44"
    "ae426082"
)

# MIME → dummy filename mapping. Falls back to PDF for anything not listed.
_MIME_TO_DUMMY = [
    # MIME pattern (LIKE), dummy filename
    ("application/pdf",              "blank.pdf"),
    ("application/%word%",           "blank.pdf"),
    ("application/%excel%",          "blank.pdf"),
    ("application/%spreadsheet%",    "blank.pdf"),
    ("application/%presentation%",   "blank.pdf"),
    ("application/octet-stream",     "blank.pdf"),
    ("image/png",                    "blank.png"),
    ("image/gif",                    "blank.png"),
    ("image/svg%",                   "blank.png"),
    ("image/webp",                   "blank.png"),
    ("image/jpeg",                   "blank.png"),
    ("image/jpg",                    "blank.png"),
]


# ─── Filestore path detection ───────────────────────────────────────────────

def _read_odoo_conf_data_dir() -> Optional[pathlib.Path]:
    """Return data_dir from the first found odoo.conf, or None."""
    for raw_path in _ODOO_CONF_PATHS:
        p = pathlib.Path(raw_path).expanduser()
        if not p.is_file():
            continue
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(p)
        except configparser.Error:
            continue
        for section in cp.sections():
            if cp.has_option(section, "data_dir"):
                return pathlib.Path(cp.get(section, "data_dir")).expanduser()
    return None


def detect_filestore_path(args, dbname: str) -> Optional[pathlib.Path]:
    """Resolve filestore path. Priority:
      1. --filestore CLI flag
      2. odoo.conf data_dir
      3. /var/lib/odoo/filestore/<dbname>/
      4. ~/.local/share/Odoo/filestore/<dbname>/
    Returns None if nothing exists — caller decides whether to skip or abort.
    """
    explicit = getattr(args, "filestore", None)
    if explicit:
        p = pathlib.Path(explicit).expanduser()
        if p.is_dir():
            return p
        log.warning("--filestore %s does not exist", explicit)

    data_dir = _read_odoo_conf_data_dir()
    if data_dir:
        p = data_dir / "filestore" / dbname
        if p.is_dir():
            return p

    for base in ("/var/lib/odoo/filestore", "~/.local/share/Odoo/filestore"):
        p = pathlib.Path(base).expanduser() / dbname
        if p.is_dir():
            return p

    return None


# ─── Non-local storage detection ────────────────────────────────────────────

class NonLocalFilestoreError(RuntimeError):
    """Raised when the DB is configured to use a non-local storage backend."""


def check_non_local_storage(conn) -> None:
    """Abort with a clear error if the DB references S3/Azure/GCS storage."""
    if not table_exists(conn, "ir_config_parameter"):
        return
    placeholders = ", ".join(["%s"] * len(_NON_LOCAL_KEYS))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT key, value FROM ir_config_parameter WHERE key IN ({placeholders})",
            _NON_LOCAL_KEYS,
        )
        rows = cur.fetchall()
    for key, value in rows:
        if not value:
            continue
        v = value.lower()
        if any(v.startswith(p) for p in _NON_LOCAL_VALUE_PREFIXES):
            raise NonLocalFilestoreError(
                f"Non-local filestore detected: {key}={value!r}. "
                f"Aborting — only local filesystems are supported."
            )


# ─── Dummy file management ──────────────────────────────────────────────────

def ensure_dummy_files(filestore_path: pathlib.Path) -> pathlib.Path:
    """Create <filestore>/dummy/ and write blank.{pdf,png}. Returns the
    dummy dir."""
    dummy_dir = filestore_path / "dummy"
    dummy_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("blank.pdf", _BLANK_PDF),
        ("blank.png", _BLANK_PNG),
    ):
        target = dummy_dir / name
        if not target.exists() or target.stat().st_size != len(content):
            target.write_bytes(content)
    log.info("Dummy files ready at %s", dummy_dir)
    return dummy_dir


# ─── SQL scrub helpers ──────────────────────────────────────────────────────

def _ui_asset_filter(prefix: str = "AND") -> str:
    """Build the standard preserve-UI-assets WHERE clause."""
    placeholders = ", ".join(["%s"] * len(_UI_ASSET_MODELS))
    return (
        f"{prefix} (res_model IS NULL OR res_model NOT IN ({placeholders}))"
    )


def nullify_attachment_binaries(conn) -> None:
    """db_datas = NULL, thumbnail = NULL (if present), for non-UI-asset rows."""
    if not table_exists(conn, "ir_attachment"):
        return

    cols = ["db_datas"]
    if column_info(conn, "ir_attachment", "thumbnail"):
        cols.append("thumbnail")
    set_sql = ", ".join(f"{c} = NULL" for c in cols)
    where = _ui_asset_filter(prefix="WHERE")
    _safe_op(
        conn,
        f"ir_attachment NULL {','.join(cols)} (non-UI-asset)",
        lambda: _update(
            conn,
            f"UPDATE ir_attachment SET {set_sql} {where}",
            _UI_ASSET_MODELS,
        ),
    )


def redirect_url_attachments(conn) -> None:
    """type='url' attachments get a generic placeholder so QWeb renders cleanly."""
    if not table_exists(conn, "ir_attachment"):
        return
    where = _ui_asset_filter(prefix="AND")
    _safe_op(
        conn,
        "ir_attachment url placeholder (type='url')",
        lambda: _update(
            conn,
            f"UPDATE ir_attachment SET url = %s "
            f"WHERE type = 'url' AND url IS NOT NULL {where}",
            ("/web/static/src/img/placeholder.png", *_UI_ASSET_MODELS),
        ),
    )


def redirect_store_fname(conn) -> None:
    """Bulk-redirect store_fname per MIME group → dummy/<filename>. Anything
    not matched by the MIME map falls back to dummy/blank.pdf."""
    if not table_exists(conn, "ir_attachment"):
        return

    matched_dummies: set[str] = set()
    for mime_pat, dummy in _MIME_TO_DUMMY:
        where = _ui_asset_filter(prefix="AND")
        _safe_op(
            conn,
            f"ir_attachment store_fname → dummy/{dummy} ({mime_pat})",
            lambda d=dummy, m=mime_pat, w=where: _update(
                conn,
                f"UPDATE ir_attachment SET store_fname = %s "
                f"WHERE store_fname IS NOT NULL AND mimetype LIKE %s {w}",
                (f"dummy/{d}", m, *_UI_ASSET_MODELS),
            ),
        )
        matched_dummies.add(dummy)

    # Catch-all: anything still pointing to a real file → blank.pdf
    where = _ui_asset_filter(prefix="AND")
    _safe_op(
        conn,
        "ir_attachment store_fname → dummy/blank.pdf (fallback)",
        lambda: _update(
            conn,
            f"UPDATE ir_attachment SET store_fname = %s "
            f"WHERE store_fname IS NOT NULL "
            f"AND store_fname NOT LIKE 'dummy/%%' {where}",
            ("dummy/blank.pdf", *_UI_ASSET_MODELS),
        ),
    )


# ─── Disk delete ────────────────────────────────────────────────────────────

def _collect_preserved_paths(conn, filestore_path: pathlib.Path) -> set[pathlib.Path]:
    """Absolute paths the UI-asset rows still point to — must NOT be deleted."""
    if not table_exists(conn, "ir_attachment"):
        return set()
    placeholders = ", ".join(["%s"] * len(_UI_ASSET_MODELS))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT store_fname FROM ir_attachment "
            f"WHERE store_fname IS NOT NULL AND res_model IN ({placeholders})",
            _UI_ASSET_MODELS,
        )
        rows = cur.fetchall()
    result: set[pathlib.Path] = set()
    for (store_fname,) in rows:
        try:
            result.add((filestore_path / store_fname).resolve())
        except (OSError, ValueError):
            continue
    return result


def delete_orphan_files(conn, filestore_path: pathlib.Path) -> int:
    """Walk the filestore and delete every file that is not (a) a dummy and
    (b) referenced by a UI-asset row. Returns count of deleted files."""
    fs_root = filestore_path.resolve()
    dummy_root = (fs_root / "dummy").resolve()
    preserved = _collect_preserved_paths(conn, filestore_path)

    deleted = 0
    for root, _dirs, files in os.walk(fs_root):
        root_path = pathlib.Path(root).resolve()
        # Always skip the dummy/ subtree
        try:
            root_path.relative_to(dummy_root)
            continue
        except ValueError:
            pass
        for fname in files:
            f = (root_path / fname).resolve()
            if f in preserved:
                continue
            try:
                f.unlink()
                deleted += 1
            except OSError as exc:
                log.warning("Could not delete %s: %s", f, exc)
    log.info("%-50s %6d files", "Filestore disk delete", deleted)
    return deleted


# ─── Orchestrator ───────────────────────────────────────────────────────────

def run_filestore_scrub(conn, args, dbname: str) -> None:
    """Step 8 — full filestore scrub. Safe to call with no filestore path
    (disk operations are skipped, DB-side scrubs still happen)."""
    try:
        check_non_local_storage(conn)
    except NonLocalFilestoreError as exc:
        log.error("%s", exc)
        return

    filestore_path = detect_filestore_path(args, dbname)

    if filestore_path is not None:
        log.info("Filestore detected at %s", filestore_path)
        try:
            ensure_dummy_files(filestore_path)
        except OSError as exc:
            log.warning("Could not create dummy files: %s — skipping disk ops", exc)
            filestore_path = None

    redirect_url_attachments(conn)
    redirect_store_fname(conn)
    nullify_attachment_binaries(conn)

    if filestore_path is not None:
        delete_orphan_files(conn, filestore_path)
    else:
        log.warning("Filestore path not found — DB scrub done, disk untouched.")
