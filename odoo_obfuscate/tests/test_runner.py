"""Integration tests for Phase 2+3: res_partner, res_users, hr_employee, crm_lead, res_company."""
from __future__ import annotations

import psycopg2
import pytest

from odoo_obfuscate.identity import build_partner_identities
from odoo_obfuscate.runner import _process_table

# ── Seed constants used for "nothing survived" assertions ─────────────────────

SEED_EMAILS = {
    "john.smith@customer.com",
    "jane.doe@example.org",
    "bob.johnson@realcompany.com",
    "contact@acmecorp.biz",
    "alice.martin@private.net",
    "carlos.rivera@email.com",
    "diana.prince@hero.org",
}
SEED_NAMES_EN = {
    "John Smith", "Jane Doe", "Bob Johnson", "Acme Corp",
    "Alice Martin", "Carlos Rivera", "Diana Prince",
}
SEED_LOGINS = {
    "admin",
    "jane.doe@example.org",
    "bob.johnson@realcompany.com",
    "orphan.user@internal.example",
}

# ── Fields dicts passed to _process_table ────────────────────────────────────

PARTNER_FIELDS: dict[str, dict] = {
    "name":         {"type": "full_name",   "jsonb": True, "coherent_with": "display_name"},
    "display_name": {"type": "full_name",   "jsonb": True, "coherent_with": "name"},
    "email":        {"type": "email"},
    "phone":        {"type": "phone"},
    "mobile":       {"type": "phone"},
    "street":       {"type": "street"},
    "street2":      {"type": "street2"},
    "zip":          {"type": "zip"},
    "city":         {"type": "city"},
    "vat":          {"type": "vat",         "unique_suffix": True},
    "website":      {"type": "website"},
    "image_1920":   {"type": "clear"},
    "image_128":    {"type": "clear"},
}

USERS_FIELDS: dict[str, dict] = {
    "login":     {"type": "from_partner"},
    "signature": {"type": "clear"},
}

HR_EMPLOYEE_FIELDS: dict[str, dict] = {
    "name":         {"type": "full_name"},
    "work_email":   {"type": "email"},
    "work_phone":   {"type": "phone"},
    "mobile_phone": {"type": "phone"},
    "job_title":    {"type": "clear"},
    "birthday":     {"type": "date_shift"},
    "private_email":{"type": "email"},
    "private_phone":{"type": "phone"},
    "ssnid":        {"type": "ssn"},
    "passport_id":  {"type": "passport", "unique_suffix": True},
    "image_1920":   {"type": "clear"},
}

CRM_LEAD_FIELDS: dict[str, dict] = {
    "name":         {"type": "alphanum", "length": 20},
    "contact_name": {"type": "full_name"},
    "email_from":   {"type": "email"},
    "phone":        {"type": "phone"},
    "partner_name": {"type": "company_name"},
    "description":  {"type": "clear"},
}

RES_COMPANY_FIELDS: dict[str, dict] = {
    "name":    {"type": "company_name"},
    "email":   {"type": "email"},
    "phone":   {"type": "phone"},
    "website": {"type": "website"},
    "vat":     {"type": "vat", "unique_suffix": True},
    "street":  {"type": "street"},
    "city":    {"type": "city"},
}


# ── Module-scoped fixture: anonymize once, all tests share the result ─────────

@pytest.fixture(scope="module")
def anonymized_db(conn_str):
    """Connect, anonymize all Phase 2+3 tables, yield connection without rollback."""
    conn = psycopg2.connect(conn_str)
    conn.autocommit = False
    identity_map = build_partner_identities(conn)
    _process_table(conn, "res_partner", PARTNER_FIELDS,    identity_map)
    _process_table(conn, "res_users",   USERS_FIELDS,      identity_map)
    _process_table(conn, "hr_employee", HR_EMPLOYEE_FIELDS, {})
    _process_table(conn, "crm_lead",    CRM_LEAD_FIELDS,    identity_map)
    _process_table(conn, "res_company", RES_COMPANY_FIELDS, {})
    yield conn
    conn.close()


# ── res_partner assertions ────────────────────────────────────────────────────

def test_partner_emails_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM res_partner WHERE email IS NOT NULL")
        emails = {row[0] for row in cur.fetchall()}
    survivors = emails & SEED_EMAILS
    assert not survivors, f"Seed emails survived: {survivors}"
    assert all(e.endswith("@anon.example.com") for e in emails)


def test_partner_emails_unique(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM res_partner WHERE email IS NOT NULL")
        emails = [row[0] for row in cur.fetchall()]
    assert len(emails) == len(set(emails))


def test_partner_names_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT name->>'en_US' FROM res_partner WHERE name IS NOT NULL")
        names = {row[0] for row in cur.fetchall()}
    survivors = names & SEED_NAMES_EN
    assert not survivors, f"Seed names survived: {survivors}"


def test_display_name_matches_name(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, display_name FROM res_partner WHERE name IS NOT NULL"
        )
        rows = cur.fetchall()
    mismatches = [(r[0], r[1]) for r in rows if r[0] != r[1]]
    assert not mismatches, f"name/display_name mismatch: {mismatches}"


def test_partner_images_cleared(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM res_partner "
            "WHERE image_1920 IS NOT NULL OR image_128 IS NOT NULL"
        )
        rows = cur.fetchall()
    assert not rows, f"Images not cleared for partners: {[r[0] for r in rows]}"


# ── res_users assertions ──────────────────────────────────────────────────────

def test_user_logins_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT login FROM res_users")
        logins = {row[0] for row in cur.fetchall()}
    survivors = logins & SEED_LOGINS
    assert not survivors, f"Seed logins survived: {survivors}"


def test_user_login_matches_partner_email(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.login, p.email "
            "FROM res_users u "
            "JOIN res_partner p ON p.id = u.partner_id"
        )
        rows = cur.fetchall()
    mismatches = [(login, email) for login, email in rows if login != email]
    assert not mismatches, f"login/email mismatch: {mismatches}"


def test_null_partner_user_fallback_login(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT id, login FROM res_users WHERE partner_id IS NULL")
        rows = cur.fetchall()
    assert rows, "Expected at least one user with partner_id IS NULL in fixture"
    for row_id, login in rows:
        expected = f"user.{row_id}@anon.example.com"
        assert login == expected, f"Expected {expected!r}, got {login!r}"


def test_user_signatures_cleared(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM res_users WHERE signature IS NOT NULL")
        rows = cur.fetchall()
    assert not rows, f"Signatures not cleared for users: {[r[0] for r in rows]}"


# ── Column-absence guard ──────────────────────────────────────────────────────

def test_column_absence_guard(conn_str):
    """Nonexistent column is skipped cleanly; _process_table returns 0."""
    conn = psycopg2.connect(conn_str)
    conn.autocommit = False
    try:
        result = _process_table(
            conn,
            "res_partner",
            {"nonexistent_column_xyz": {"type": "email"}},
            {},
        )
        assert result == 0
    finally:
        conn.rollback()
        conn.close()


# ── hr_employee assertions ────────────────────────────────────────────────────

SEED_EMPLOYEE_NAMES   = {"John Smith", "Jane Doe", "Bob Johnson", "Alice Martin", "Carlos Rivera"}
SEED_WORK_EMAILS      = {"john.smith@acme.com", "jane.doe@acme.com", "bob.johnson@acme.com",
                         "alice.martin@acme.com", "carlos.rivera@acme.com"}
SEED_BIRTHDAYS        = {"1985-03-15", "1990-07-22", "1988-11-30", "1992-04-08", "1995-09-19"}
SEED_SSNS             = {"123-45-6789", "234-56-7890", "345-67-8901", "456-78-9012", "567-89-0123"}


def test_employee_names_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM hr_employee WHERE name IS NOT NULL")
        names = {row[0] for row in cur.fetchall()}
    assert not (names & SEED_EMPLOYEE_NAMES), f"Seed employee names survived: {names & SEED_EMPLOYEE_NAMES}"


def test_employee_work_emails_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT work_email FROM hr_employee WHERE work_email IS NOT NULL")
        emails = {row[0] for row in cur.fetchall()}
    assert not (emails & SEED_WORK_EMAILS)
    assert all(e.endswith("@anon.example.com") for e in emails)


def test_employee_birthdays_shifted(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT birthday::text FROM hr_employee WHERE birthday IS NOT NULL")
        dates = {row[0] for row in cur.fetchall()}
    assert not (dates & SEED_BIRTHDAYS), f"Seed birthdays survived: {dates & SEED_BIRTHDAYS}"


def test_employee_ssns_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT ssnid FROM hr_employee WHERE ssnid IS NOT NULL")
        ssns = {row[0] for row in cur.fetchall()}
    assert not (ssns & SEED_SSNS), f"Seed SSNs survived: {ssns & SEED_SSNS}"


def test_employee_images_cleared(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM hr_employee WHERE image_1920 IS NOT NULL")
        rows = cur.fetchall()
    assert not rows, f"Employee images not cleared: {[r[0] for r in rows]}"


# ── crm_lead assertions ───────────────────────────────────────────────────────

SEED_LEAD_CONTACTS = {"John Smith", "Jane Doe", "Bob Johnson"}
SEED_LEAD_EMAILS   = {"john.smith@customer.com", "jane.doe@example.org",
                      "bob.johnson@realcompany.com"}


def test_crm_lead_contact_names_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT contact_name FROM crm_lead WHERE contact_name IS NOT NULL")
        names = {row[0] for row in cur.fetchall()}
    assert not (names & SEED_LEAD_CONTACTS)


def test_crm_lead_emails_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT email_from FROM crm_lead WHERE email_from IS NOT NULL")
        emails = {row[0] for row in cur.fetchall()}
    assert not (emails & SEED_LEAD_EMAILS)
    assert all(e.endswith("@anon.example.com") for e in emails)


def test_crm_lead_descriptions_cleared(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM crm_lead WHERE description IS NOT NULL")
        rows = cur.fetchall()
    assert not rows, f"Lead descriptions not cleared: {[r[0] for r in rows]}"


def test_crm_lead_name_is_alphanum(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM crm_lead WHERE name IS NOT NULL")
        names = [row[0] for row in cur.fetchall()]
    assert names, "Expected crm_lead rows"
    for name in names:
        assert name.isalnum(), f"Lead name not alphanum: {name!r}"


# ── res_company assertions ────────────────────────────────────────────────────

def test_company_name_replaced(anonymized_db):
    conn = anonymized_db
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM res_company")
        names = {row[0] for row in cur.fetchall()}
    assert "Acme Corporation" not in names, "Seed company name survived"


# ── Chunked mode ──────────────────────────────────────────────────────────────

def test_chunked_mode_returns_correct_count(anonymized_db):
    """batch_size=2 on crm_lead (3 rows) → 2 batches → 3 total rows returned."""
    count = _process_table(
        anonymized_db,
        "crm_lead",
        {"email_from": {"type": "email"}},
        {},
        batch_size=2,
    )
    assert count == 3
