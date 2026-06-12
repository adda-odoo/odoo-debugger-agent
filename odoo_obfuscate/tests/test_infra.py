"""Integration tests for Phase 4 — infrastructure scrubbing."""
from __future__ import annotations

import psycopg2
import pytest

from odoo_obfuscate import infra


@pytest.fixture(scope="module")
def scrubbed_db(conn_str):
    """Connect, run pre_neutralize + post_scrub once, yield the connection."""
    conn = psycopg2.connect(conn_str)
    conn.autocommit = False
    infra.pre_neutralize(conn)
    infra.post_scrub(conn)
    yield conn
    conn.close()


# ─── Step 1b — pre_neutralize ───────────────────────────────────────────────

def test_ir_mail_server_deactivated(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT bool_or(active) FROM ir_mail_server")
        assert cur.fetchone()[0] is False


def test_fetchmail_server_deactivated(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT bool_or(active) FROM fetchmail_server")
        assert cur.fetchone()[0] is False


def test_ir_cron_all_disabled(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT bool_or(active) FROM ir_cron")
        assert cur.fetchone()[0] is False


def test_payment_provider_disabled(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT DISTINCT state FROM payment_provider")
        states = {row[0] for row in cur.fetchall()}
    assert states == {"disabled"}


# ─── Step 4 — ir_config_parameter ───────────────────────────────────────────

def test_named_config_updates(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM ir_config_parameter "
            "WHERE key IN ('web.base.url', 'mail.catchall.domain', 'mail.default.from')"
        )
        rows = dict(cur.fetchall())
    assert rows["web.base.url"]         == "http://localhost:8069"
    assert rows["mail.catchall.domain"] == "anonymized.local"
    assert rows["mail.default.from"]    == "anonymized.local"


def test_credential_keys_scrubbed(scrubbed_db):
    """Keys matching the credential pattern (password/secret/token/key/smtp/
    api/oauth/credential/uuid) → '<redacted>'."""
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM ir_config_parameter "
            "WHERE key IN ('smtp.server.password', 'auth_oauth.authorization_data', 'database.uuid')"
        )
        rows = dict(cur.fetchall())
    assert len(rows) == 3, f"Expected 3 credential keys, got {rows}"
    for k, v in rows.items():
        assert v == "<redacted>", f"{k} not scrubbed: {v!r}"


def test_non_credential_keys_preserved(scrubbed_db):
    """Keys that don't match the credential pattern must keep their values."""
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT value FROM ir_config_parameter WHERE key = 'database.expiration_date'"
        )
        assert cur.fetchone()[0] == "2099-01-01"


# ─── Step 4b/4c — mail server credentials ───────────────────────────────────

def test_fetchmail_credentials_cleared(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute('SELECT "user", password FROM fetchmail_server')
        for user, pwd in cur.fetchall():
            assert user == "anon@example.com"
            assert pwd  == ""


def test_ir_mail_server_credentials_cleared(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT smtp_user, smtp_pass, smtp_ssl_certificate, smtp_ssl_private_key "
            "FROM ir_mail_server"
        )
        for user, pwd, cert, key in cur.fetchall():
            assert user == "anon@example.com"
            assert pwd  == ""
            assert cert is None
            assert key  is None


# ─── Step 4d — account hashing ──────────────────────────────────────────────

def test_account_journal_hash_disabled(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT bool_or(restrict_mode_hash_table) FROM account_journal")
        assert cur.fetchone()[0] is False


def test_account_move_hashes_nullified(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT secure_sequence_number, inalterable_hash FROM account_move"
        )
        for seq, hash_ in cur.fetchall():
            assert seq is None
            assert hash_ is None


# ─── Step 5 — user passwords ────────────────────────────────────────────────

def test_user_passwords_reset(scrubbed_db):
    """All active users except id=1 get the admin hash."""
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT id, password FROM res_users WHERE active = true")
        rows = cur.fetchall()
    from passlib.hash import pbkdf2_sha512
    for user_id, pwd in rows:
        if user_id == 1:
            # __system__ user keeps original hash — may not be pbkdf2_sha512 in fixture
            try:
                assert not (pwd and pbkdf2_sha512.verify("admin", pwd))
            except ValueError:
                pass  # non-pbkdf2 hash in fixture — acceptable
        else:
            assert pwd and pbkdf2_sha512.verify("admin", pwd)


# ─── Step 5b — discuss_channel ──────────────────────────────────────────────

def test_chat_channels_anonymized(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT name FROM discuss_channel "
            "WHERE channel_type IN ('chat', 'livechat')"
        )
        names = {row[0] for row in cur.fetchall()}
    assert names == {"Anonymized"}


def test_system_channels_preserved(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT name FROM discuss_channel WHERE channel_type = 'channel'"
        )
        names = {row[0] for row in cur.fetchall()}
    # general, announce, sales-team — none were touched
    assert "general" in names
    assert "announce" in names
    assert "sales-team" in names


# ─── Step 6 — mail_alias ────────────────────────────────────────────────────

def test_mail_aliases_nullified(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute(
            "SELECT id, alias_name FROM mail_alias ORDER BY id"
        )
        rows = cur.fetchall()
    by_id = dict(rows)
    # catchall and bounce preserved
    assert by_id[1] == "catchall"
    assert by_id[2] == "bounce"
    # everything else nullified
    assert by_id[3] is None
    assert by_id[4] is None
    assert by_id[5] is None


# ─── Step 7 — ir_logging ────────────────────────────────────────────────────

def test_ir_logging_truncated(scrubbed_db):
    with scrubbed_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ir_logging")
        assert cur.fetchone()[0] == 0


# NOTE: "table absent" path is covered by real-DB runs against test_restore
# where Odoo-version-specific tables (google_gmail_mixin, portal_mixin,
# mail_thread_blacklist, etc.) are routinely missing. A unit test that drops
# a fixture table inside a transaction deadlocks against the module-scoped
# scrubbed_db fixture's open SELECT transactions — not worth the complexity.
