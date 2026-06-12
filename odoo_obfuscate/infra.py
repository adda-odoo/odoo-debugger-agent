"""Infrastructure scrubbing — credentials, mail, logging, alias, idempotency.

Implements PLAN.md Steps 1b and 4–7. Two entry points:
  - pre_neutralize(conn)   → runs BEFORE the per-table anonymization loop
  - post_scrub(conn)       → runs AFTER the per-table anonymization loop
"""
from __future__ import annotations

import logging
from typing import Callable

import psycopg2

from db import column_info, table_exists

log = logging.getLogger(__name__)

ERRORS = []

def _make_admin_hash() -> str:
    """Compute a fresh PBKDF2-SHA512 hash of 'admin' using passlib."""
    from passlib.hash import pbkdf2_sha512
    return pbkdf2_sha512.using(rounds=25000).hash("admin")

# Aliases that must be preserved — they handle system-level mail routing.
_PRESERVED_ALIASES = ("catchall", "bounce")

# UI / DM channels with these names exist on every Odoo install and must
# be preserved even when their channel_type matches the scrub filter.
_PRESERVED_CHANNEL_NAMES = ("general", "announce")

# ir_config_parameter keys that need a real value (not '<redacted>') so Odoo
# keeps functioning. Updated BEFORE the pattern scrub to avoid corruption.
_NAMED_CONFIG_UPDATES = [
    ("web.base.url",            "http://localhost:8069"),
    ("mail.catchall.domain",    "anonymized.local"),
    ("mail.default.from",       "anonymized.local"),
]
# Same keys as a SQL tuple literal — referenced by the pattern scrub's NOT IN.
_NAMED_CONFIG_KEYS = tuple(k for k, _ in _NAMED_CONFIG_UPDATES)


def _safe_op(conn, label: str, fn: Callable[[], int]) -> int:
    """Run one infrastructure op, commit on success, rollback+warn on error."""
    try:
        count = fn()
        conn.commit()
        log.info("%-50s %6d rows", label, count)
        return count
    except psycopg2.Error as exc:
        conn.rollback()
        log.warning("Skipping %s — %s: %s", label, type(exc).__name__, exc)
        ERRORS.append(label)
        return 0


def _update(conn, sql: str, params: tuple = ()) -> int:
    """Execute an UPDATE/TRUNCATE and return rowcount."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


# ─── Step 1b — Odoo-standard neutralization ─────────────────────────────────

def _deactivate(conn, table: str) -> None:
    """UPDATE <table> SET active = false — skip if table absent."""
    if not table_exists(conn, table):
        log.info("Table %s absent — skipping deactivation", table)
        return
    _safe_op(
        conn,
        f"{table}.active = false",
        lambda: _update(conn, f"UPDATE {table} SET active = false"),
    )


def _disable_payment_providers(conn) -> None:
    """Disable payment.provider (v18+) or payment.acquirer (v16/v17)."""
    for table in ("payment_provider", "payment_acquirer"):
        if not table_exists(conn, table):
            continue
        _safe_op(
            conn,
            f"{table}.state = 'disabled'",
            lambda t=table: _update(conn, f"UPDATE {t} SET state = 'disabled'"),
        )


def pre_neutralize(conn) -> None:
    """Step 1b — disable outgoing mail, cron, and payment providers.

    Runs BEFORE the per-table loop so triggers fired by those subsystems
    can't react to anonymization writes.
    """
    _deactivate(conn, "ir_mail_server")
    _deactivate(conn, "fetchmail_server")
    _deactivate(conn, "ir_cron")
    _disable_payment_providers(conn)


# ─── Step 4 — ir_config_parameter ───────────────────────────────────────────

def scrub_ir_config_parameter(conn) -> None:
    """Named updates first, then pattern scrub. Order is critical — see
    CLAUDE.md: 'Named ir_config_parameter updates must run before the
    pattern scrub.'
    """
    if not table_exists(conn, "ir_config_parameter"):
        return

    def named() -> int:
        total = 0
        with conn.cursor() as cur:
            for key, value in _NAMED_CONFIG_UPDATES:
                cur.execute(
                    "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
                    (value, key),
                )
                total += cur.rowcount
        return total

    _safe_op(conn, "ir_config_parameter named updates", named)

    placeholders = ", ".join(["%s"] * len(_NAMED_CONFIG_KEYS))
    # Pattern extended beyond PLAN.md's original (password|secret|token|key|smtp|api):
    # also covers oauth state/tokens, generic credentials, and database UUIDs
    # — all of which can identify the customer to an LLM.
    _safe_op(
        conn,
        "ir_config_parameter credential pattern scrub",
        lambda: _update(
            conn,
            f"UPDATE ir_config_parameter SET value = '<redacted>' "
            f"WHERE key ~* '(password|secret|token|key|smtp|api|oauth|credential|uuid)' "
            f"AND key NOT IN ({placeholders})",
            _NAMED_CONFIG_KEYS,
        ),
    )


# ─── Step 4b/4c — Mail server credentials ──────────────────────────────────

def scrub_fetchmail_credentials(conn) -> None:
    if not table_exists(conn, "fetchmail_server"):
        return
    # "user" is a SQL reserved word — must be quoted.
    _safe_op(
        conn,
        "fetchmail_server credentials",
        lambda: _update(
            conn,
            'UPDATE fetchmail_server SET password = %s, "user" = %s',
            ("", "anon@example.com"),
        ),
    )


def scrub_ir_mail_server_credentials(conn) -> None:
    if not table_exists(conn, "ir_mail_server"):
        return
    # Build the SET clause from whichever credential columns actually exist.
    cred_cols = {
        "smtp_pass":            "",
        "smtp_user":            "anon@example.com",
        "smtp_ssl_certificate": None,
        "smtp_ssl_private_key": None,
    }
    present = {c: v for c, v in cred_cols.items() if column_info(conn, "ir_mail_server", c)}
    if not present:
        return
    set_clauses = ", ".join(f"{c} = %s" for c in present)
    params = tuple(present.values())
    _safe_op(
        conn,
        "ir_mail_server credentials",
        lambda: _update(conn, f"UPDATE ir_mail_server SET {set_clauses}", params),
    )


# ─── Step 4d — Account inalterability hash chain ────────────────────────────

def neutralize_account_hashing(conn) -> None:
    """Disable Odoo's GoBD/FEC hash chain so audit reports don't fail."""
    if table_exists(conn, "account_journal") and column_info(
        conn, "account_journal", "restrict_mode_hash_table"
    ):
        _safe_op(
            conn,
            "account_journal.restrict_mode_hash_table = false",
            lambda: _update(
                conn,
                "UPDATE account_journal SET restrict_mode_hash_table = false",
            ),
        )

    if table_exists(conn, "account_move"):
        cols = [
            c for c in ("secure_sequence_number", "inalterable_hash")
            if column_info(conn, "account_move", c)
        ]
        if cols:
            set_sql = ", ".join(f"{c} = NULL" for c in cols)
            _safe_op(
                conn,
                f"account_move hash neutralize ({', '.join(cols)})",
                lambda: _update(conn, f"UPDATE account_move SET {set_sql}"),
            )


# ─── Step 5 — res_users password reset + known login ────────────────────────

def reset_user_passwords(conn) -> None:
    """All active users (except id=1, the __system__ superuser) get the
    PBKDF2-SHA512 hash of 'admin'."""
    if not table_exists(conn, "res_users"):
        return
    _safe_op(
        conn,
        "res_users.password = pbkdf2('admin')",
        lambda: _update(
            conn,
            "UPDATE res_users SET password = %s WHERE active = true AND id != 1",
            (_make_admin_hash(),),
        ),
    )


def reset_admin_login(conn) -> None:
    """Pin the first active internal user's login to 'admin'.

    res_users.login is anonymized by the runner (B1 field), so without this
    step no developer would know what to type at the login screen.
    Combined with the password reset above, login: admin / password: admin.
    """
    if not table_exists(conn, "res_users"):
        return

    def _set_login() -> int:
        with conn.cursor() as cur:
            # share column absent on some Odoo versions / test fixtures — fall back.
            if column_info(conn, "res_users", "share"):
                cur.execute(
                    "SELECT id FROM res_users "
                    "WHERE active = true AND share = false AND id != 1 "
                    "ORDER BY id LIMIT 1"
                )
            else:
                cur.execute(
                    "SELECT id FROM res_users "
                    "WHERE active = true AND id != 1 "
                    "ORDER BY id LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return 0
            cur.execute(
                "UPDATE res_users SET login = 'admin' WHERE id = %s",
                (row[0],),
            )
            return 1

    _safe_op(conn, "res_users admin login → 'admin'", _set_login)


# ─── Step 5b — discuss/mail_channel names ──────────────────────────────────

def scrub_chat_channel_names(conn) -> None:
    """Personal channels (chat/livechat) get their name replaced with
    'Anonymized'. System channels are preserved."""
    # Odoo 16 renamed mail_channel → discuss_channel; try both.
    for table in ("discuss_channel", "mail_channel"):
        if not table_exists(conn, table):
            continue
        if not column_info(conn, table, "channel_type"):
            continue
        names_placeholders = ", ".join(["%s"] * len(_PRESERVED_CHANNEL_NAMES))
        _safe_op(
            conn,
            f"{table}.name (chat/livechat → 'Anonymized')",
            lambda t=table, p=names_placeholders: _update(
                conn,
                f"UPDATE {t} SET name = 'Anonymized' "
                f"WHERE channel_type IN ('chat', 'livechat') "
                f"AND (name IS NULL OR name NOT IN ({p}))",
                _PRESERVED_CHANNEL_NAMES,
            ),
        )
        break  # only one of the two should exist on any given DB


# ─── Step 6 — mail_alias nullify ────────────────────────────────────────────

def nullify_mail_aliases(conn) -> None:
    """alias_name = NULL except for catchall/bounce. NEVER DELETE — see
    CLAUDE.md: cascading FKs from crm.team / helpdesk.team / project.project."""
    if not table_exists(conn, "mail_alias"):
        return
    placeholders = ", ".join(["%s"] * len(_PRESERVED_ALIASES))
    _safe_op(
        conn,
        "mail_alias.alias_name = NULL (except catchall/bounce)",
        lambda: _update(
            conn,
            f"UPDATE mail_alias SET alias_name = NULL "
            f"WHERE alias_name IS NOT NULL "
            f"AND alias_name NOT IN ({placeholders})",
            _PRESERVED_ALIASES,
        ),
    )


# ─── Step 7 — ir_logging truncate ───────────────────────────────────────────

def truncate_ir_logging(conn) -> None:
    """TRUNCATE — safe, no FK dependencies, removes any tracebacks containing
    raw PII."""
    if not table_exists(conn, "ir_logging"):
        return

    def truncate() -> int:
        with conn.cursor() as cur:
            # rowcount is -1 for TRUNCATE in psycopg2 — count first for nice log.
            cur.execute("SELECT COUNT(*) FROM ir_logging")
            n = cur.fetchone()[0]
            cur.execute("TRUNCATE TABLE ir_logging RESTART IDENTITY")
        return n

    _safe_op(conn, "ir_logging TRUNCATE", truncate)


# ─── Entry points ───────────────────────────────────────────────────────────

def post_scrub(conn) -> None:
    """Steps 4 → 7 — runs AFTER the per-table loop."""
    scrub_ir_config_parameter(conn)
    scrub_fetchmail_credentials(conn)
    scrub_ir_mail_server_credentials(conn)
    neutralize_account_hashing(conn)
    reset_user_passwords(conn)
    reset_admin_login(conn)
    scrub_chat_channel_names(conn)
    nullify_mail_aliases(conn)
    truncate_ir_logging(conn)
