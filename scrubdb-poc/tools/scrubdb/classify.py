# Created by Odoo — Technical Support tooling (scrubdb).
"""Content-based sampling helpers for blanket mode's integrity guards.

Blanket mode masks every column by type unless it can prove a column must be
kept — two of those proofs need to look at actual sampled values rather than
schema/name alone: an Odoo Reference field (``model,id`` in one varchar, no
schema signal) and a Selection-like field that a small table's CHECK/
ir_model_fields metadata didn't already catch. Pure and unit-testable:
:func:`looks_like_reference`/:func:`looks_like_enum` take an iterable of
values and need no database.
"""
from __future__ import annotations

import re

# Odoo `fields.Reference` stores "<model.name>,<id>" in one varchar column
# (source: orm/fields_reference.py -> "%s,%s" % (value._name, value.id)).
# Masking it corrupts the reference, and there is no schema signal — detect by
# content. Model names are dotted lowercase identifiers (res.partner, sale.order).
_REFERENCE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+,\d+$")


def sample_column(cur, schema, table, column, limit: int = 300):
    """Sample up to ``limit`` values from a column, random where feasible."""
    ident = f'"{schema}"."{table}"."{column}"'
    src = f'"{schema}"."{table}"'
    try:
        cur.execute(
            f"SELECT {ident} FROM {src} "
            f"WHERE {ident} IS NOT NULL ORDER BY random() LIMIT %s",
            (limit,),
        )
    except Exception:
        # Fallback for tables where ORDER BY random() is too expensive/unsupported.
        cur.execute(
            f"SELECT {ident} FROM {src} WHERE {ident} IS NOT NULL LIMIT %s",
            (limit,),
        )
    return [r[0] for r in cur.fetchall()]


def looks_like_reference(values, min_rows: int = 5, min_fraction: float = 0.8) -> bool:
    """Heuristic: does a sample look like an Odoo Reference field ("model,id")?

    Pure and DB-free. Keeps such columns unmasked so the record reference stays
    intact. Conservative: needs a few rows and a strong majority match.
    """
    vals = [str(v) for v in values if v is not None]
    if len(vals) < min_rows:
        return False
    matches = sum(1 for v in vals if _REFERENCE_RE.match(v))
    return matches / len(vals) >= min_fraction


def looks_like_enum(values, max_distinct: int = 25, max_len: int = 32,
                    min_rows: int = 20) -> bool:
    """Heuristic: does a sample look like a selection/enum field rather than PII?

    Odoo selection fields (``state``, ``type``, ...) are plain varchar with no
    schema signal. A sample of them shows *few, short, repeated* values, whereas
    real free-text/PII is mostly distinct. Pure and DB-free so it can be tested.
    Conservative: needs enough rows to trust, and only fires when values repeat.
    """
    vals = [str(v) for v in values if v is not None]
    if len(vals) < min_rows:
        return False                      # too little signal — don't guess
    distinct = set(vals)
    if len(distinct) > max_distinct:
        return False                      # too varied to be an enum
    if len(distinct) >= len(vals):
        return False                      # all distinct — not an enum
    if max((len(v) for v in distinct), default=0) > max_len:
        return False                      # long values — free text, not codes
    return True
