# Created by Odoo — Technical Support tooling (scrubdb).
"""Post-mask finalization for an Odoo dev/AI copy.

These steps run *after* masking, in the same transaction, so a dry-run previews
them and the values read back (logins, names) are the already-masked ones — no
real PII is ever printed. All steps are Odoo-specific and no-op on a database
where the table isn't present.
"""
from __future__ import annotations

import logging

_logger = logging.getLogger("scrubdb.finalize")


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s LIMIT 1",
        (schema, table),
    )
    return bool(cur.fetchall())


def reset_passwords(cur, schema: str, password: str) -> int:
    """Set every ``res_users`` password to ``password`` (plaintext), except the
    root/superuser (id=1), which is never modified.

    Odoo stores the deprecated ``plaintext`` scheme and re-hashes on first login
    (res_users.init / verify_and_update), so this is a valid known password.
    """
    if not _table_exists(cur, schema, "res_users"):
        return 0
    cur.execute(f'UPDATE "{schema}"."res_users" SET password = %s WHERE id <> 1',
                (password,))
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    _logger.info("reset %d user password(s)", n)
    return n


def normalize_logins(cur, schema: str) -> int:
    """Replace every non-root login with ``user<id>``.

    The login (email or free-text username) is PII, so it is replaced with a
    handle built from the user's own database id — anonymised, unique, and still
    identifiable for support (the id is never masked, so ``user47`` is the user
    with id 47). The root/superuser (id=1) is never modified.
    """
    if not _table_exists(cur, schema, "res_users"):
        return 0
    cur.execute(
        f'''UPDATE "{schema}"."res_users" SET login = 'user' || id::text WHERE id <> 1'''
    )
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    _logger.info("normalized %d login(s) to user<id>", n)
    return n


def deactivate_crons(cur, schema: str) -> int:
    """Disable all scheduled actions so the copy can't fire jobs/emails."""
    if not _table_exists(cur, schema, "ir_cron"):
        return 0
    cur.execute(f'UPDATE "{schema}"."ir_cron" SET active = false WHERE active')
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    _logger.info("deactivated %d cron(s)", n)
    return n


def list_users(cur, schema: str, limit: int) -> list:
    """Return up to ``limit`` users as ``(id, name, login, password)``.

    LEFT JOIN res_partner for the display name. Read after masking, so login and
    name are the masked values (safe to print); password is whatever it is now
    (``admin`` if reset_passwords ran).
    """
    if not _table_exists(cur, schema, "res_users"):
        return []
    cur.execute(
        f'''
        SELECT u.id, p.name, u.login, u.password
        FROM "{schema}"."res_users" u
        LEFT JOIN "{schema}"."res_partner" p ON p.id = u.partner_id
        ORDER BY u.id
        LIMIT %s
        ''',
        (limit,),
    )
    return cur.fetchall()
