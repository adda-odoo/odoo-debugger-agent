"""Tests for Phase 7 — manifest, dry-run, review.yaml merge, exceptions.yaml."""
from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from odoo_obfuscate.manifest import RunManifest


# ─── RunManifest unit tests ─────────────────────────────────────────────────

def _capture_summary(**kwargs) -> str:
    m = RunManifest(
        dbname=kwargs.get("dbname", "testdb"),
        pg_ver=kwargs.get("pg_ver", "15.4"),
        odoo_ver=kwargs.get("odoo_ver", "17.0"),
        dry_run=kwargs.get("dry_run", False),
    )
    for table, count in kwargs.get("tables", {}).items():
        m.record_table(table, count)
    for col in kwargs.get("absent_columns", []):
        t, c = col.split(".", 1)
        m.record_absent_column(t, c)
    for key in kwargs.get("undecided", []):
        m.record_undecided(key)
    for key in kwargs.get("low_conf", []):
        m.record_low_confidence(key)
    for key, action in kwargs.get("exceptions", []):
        m.record_exception(key, action)
    m.complete()

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        m.print_summary()
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def test_attestation_line_present():
    out = _capture_summary(tables={"res_partner": 5})
    assert "ATTESTATION:" in out
    assert "No personal data was transmitted" in out


def test_attestation_says_was_anonymized_on_live_run():
    out = _capture_summary()
    assert "was anonymized" in out
    assert "would be anonymized" not in out


def test_attestation_says_would_be_anonymized_on_dry_run():
    out = _capture_summary(dry_run=True)
    assert "would be anonymized" in out


def test_dry_run_mode_label_in_header():
    out = _capture_summary(dry_run=True)
    assert "DRY RUN" in out


def test_table_counts_shown():
    out = _capture_summary(tables={"res_partner": 14, "res_users": 4})
    assert "res_partner" in out
    assert "14" in out
    assert "res_users" in out
    assert "4" in out


def test_zero_row_tables_not_shown_in_per_table():
    out = _capture_summary(tables={"res_partner": 0, "res_users": 4})
    lines = out.splitlines()
    per_table_lines = [l for l in lines if "res_partner" in l and "rows" in l]
    assert not per_table_lines  # res_partner has 0 rows, should not appear


def test_absent_columns_shown():
    out = _capture_summary(absent_columns=["account_move.invoice_pdf_report_file"])
    assert "account_move.invoice_pdf_report_file" in out
    assert "Absent columns" in out


def test_undecided_review_warning_shown():
    out = _capture_summary(undecided=["account_analytic_account.name"])
    assert "WARNING" in out
    assert "account_analytic_account.name" in out
    assert "no decision" in out


def test_low_confidence_section_shown():
    out = _capture_summary(low_conf=["res_bank.name"])
    assert "Low-confidence" in out
    assert "res_bank.name" in out


def test_exceptions_section_shown():
    out = _capture_summary(exceptions=[("x_custom.x_notes", "anonymize (clear)")])
    assert "exceptions.yaml" in out
    assert "x_custom.x_notes" in out


def test_database_name_in_output():
    out = _capture_summary(dbname="my_prod_db")
    assert "my_prod_db" in out


def test_odoo_version_in_output():
    out = _capture_summary(odoo_ver="18.0")
    assert "18.0" in out


# ─── _build_fields_section integration ─────────────────────────────────────

