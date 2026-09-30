# Created by Odoo — Technical Support tooling (scrubdb).
"""Deterministic, keyed value transforms — the core of scrubdb.

No database access, no Odoo dependency. Every transform has the signature
``fn(value, ctx) -> new_value`` and MUST be null-safe: given ``None`` it returns
``None`` unchanged.

Determinism comes from an HMAC-SHA256 keyed by a random per-run secret held in
:class:`Context` — never from Faker's process-global seed (which its own docs
say is not stable across versions). Equal inputs map to equal outputs *within a
run* so foreign keys and joins survive masking; across runs the key is discarded
so the mapping is irreversible (one-way).
"""
from __future__ import annotations

import datetime as _datetime
import decimal as _decimal
import hashlib
import hmac
import re
from typing import Callable, Sequence

# Faker is optional. When absent, the built-in word lists below are used. We
# never rely on Faker's global seed for determinism — see module docstring.
try:  # pragma: no cover - import guard
    from faker import Faker as _Faker  # noqa: F401

    _HAVE_FAKER = True
except Exception:  # pragma: no cover
    _HAVE_FAKER = False


class Context:
    """Per-run randomness source keyed by a secret that is never persisted."""

    def __init__(self, run_key: bytes) -> None:
        if not isinstance(run_key, (bytes, bytearray)):
            raise TypeError("run_key must be bytes")
        self.run_key = bytes(run_key)

    def digest(self, *parts) -> bytes:
        """Stable HMAC digest for the given parts (order-sensitive)."""
        msg = "\x1f".join("" if p is None else str(p) for p in parts)
        return hmac.new(self.run_key, msg.encode("utf-8"), hashlib.sha256).digest()

    def int_for(self, *parts) -> int:
        """Stable non-negative integer derived from the parts."""
        return int.from_bytes(self.digest(*parts), "big")

    def choice(self, seq: Sequence, *parts):
        """Deterministically pick one element of ``seq`` for the parts."""
        items = list(seq)
        if not items:
            raise ValueError("choice() on an empty sequence")
        return items[self.int_for(*parts) % len(items)]


# --- built-in fake pools (deterministic, no external deps) -----------------

_FIRST = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Sam", "Jamie",
    "Drew", "Quinn", "Avery", "Reese", "Rowan", "Skyler", "Cameron", "Devon",
    "Emerson", "Harper", "Kendall", "Logan",
]
_LAST = [
    "Smith", "Johnson", "Lee", "Garcia", "Brown", "Martin", "Davis", "Clark",
    "Lewis", "Walker", "Hall", "Allen", "Young", "King", "Wright", "Scott",
    "Green", "Baker", "Adams", "Nelson",
]
_COMPANY = [
    "Acme", "Globex", "Initech", "Umbrella", "Soylent", "Vandelay", "Wonka",
    "Stark", "Wayne", "Cyberdyne", "Hooli", "Pied Piper", "Aperture", "Tyrell",
]
_COMPANY_SUFFIX = ["Inc", "LLC", "Ltd", "Group", "Holdings", "Co"]
_CITY = [
    "Springfield", "Riverton", "Fairview", "Franklin", "Greenville", "Bristol",
    "Clinton", "Georgetown", "Salem", "Madison", "Ashland", "Oakdale",
]
_STREET = [
    "Main St", "Oak Ave", "Pine Rd", "Maple Dr", "Cedar Ln", "Elm St",
    "Washington Ave", "Lake Rd", "Hill St", "Park Ave", "Sunset Blvd",
]

_EMAIL_IN_TEXT = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s().-]{6,}\d")


# --- simple transforms -----------------------------------------------------

def keep(value, ctx):  # noqa: ANN001
    return value


def null(value, ctx):
    return None


def empty(value, ctx):
    return None if value is None else ""


def redact(value, ctx):
    return None if value is None else "***"


def fake_first_name(value, ctx):
    if value is None:
        return None
    return ctx.choice(_FIRST, value, "first")


def fake_last_name(value, ctx):
    if value is None:
        return None
    return ctx.choice(_LAST, value, "last")


def fake_name(value, ctx):
    if value is None:
        return None
    return f"{ctx.choice(_FIRST, value, 'first')} {ctx.choice(_LAST, value, 'last')}"


def fake_company(value, ctx):
    if value is None:
        return None
    return f"{ctx.choice(_COMPANY, value)} {ctx.choice(_COMPANY_SUFFIX, value, 'sfx')}"


def fake_city(value, ctx):
    if value is None:
        return None
    return ctx.choice(_CITY, value)


def fake_street(value, ctx):
    if value is None:
        return None
    number = ctx.int_for(value, "num") % 9998 + 1
    return f"{number} {ctx.choice(_STREET, value, 'street')}"


def fake_email(value, ctx):
    """Deterministic address that always ends ``@example.com``.

    verify.py treats ``@example.com`` as clean, so this is safe output.
    """
    if value is None:
        return None
    first = ctx.choice(_FIRST, value, "first").lower()
    last = ctx.choice(_LAST, value, "last").lower()
    tag = ctx.int_for(value, "email") % 1000
    return f"{first}.{last}{tag}@example.com"


def fake_phone(value, ctx):
    """Deterministic phone in the reserved 555-01xx range."""
    if value is None:
        return None
    line = ctx.int_for(value, "phone") % 100
    return f"+1-555-01{line:02d}"


def fake_ip(value, ctx):
    """Deterministic address in the private 10.0.0.0/8 block."""
    if value is None:
        return None
    d = ctx.digest(value, "ip")
    return f"10.{d[0]}.{d[1]}.{d[2] % 254 + 1}"


def scramble_digits(value, ctx):
    """Replace each digit with a deterministic other digit; keep formatting.

    Hashes the whole value once and derives each digit from a distinct byte of
    that single digest, rather than one HMAC per digit — deterministic and
    format-preserving, but far cheaper on digit-heavy values. Past 32 digits the
    digest is reused cyclically (fine for anonymisation; this is not a cipher).
    """
    if value is None:
        return None
    s = str(value)
    digest = ctx.digest(value, "d")  # one HMAC for the whole value
    out = []
    k = 0
    for ch in s:
        if ch.isdigit():
            out.append(str(digest[k % len(digest)] % 10))
            k += 1
        else:
            out.append(ch)
    return "".join(out)


def scrub_text(value, ctx):
    """Strip PII embedded in free text (emails, phone numbers)."""
    if value is None:
        return None
    s = str(value)
    s = _EMAIL_IN_TEXT.sub(lambda m: fake_email(m.group(0), ctx), s)
    s = _PHONE_IN_TEXT.sub(lambda m: scramble_digits(m.group(0), ctx), s)
    return s


def _keystream(digest_fn, value, tag, nbytes):
    """Deterministic byte stream of length >= ``nbytes`` for ``value``.

    Built from 32-byte HMAC blocks (``digest(value, tag, block)``) — a stream
    cipher-style expansion — so masking a long value costs ~len/32 HMACs instead
    of one per character.
    """
    out = bytearray()
    block = 0
    while len(out) < nbytes:
        out += digest_fn(value, tag, block)
        block += 1
    return out


def mask_text(value, ctx):
    """Deterministic blanket mask that preserves shape/length.

    Letters map to letters (case kept), digits to digits; whitespace and
    punctuation are preserved. Used in blanket mode for text columns with no
    recognised semantics — the structure survives but the content does not.
    Cost scales with ~len/32 HMACs, not one per character, so large HTML/Text
    values stay cheap.
    """
    if value is None:
        return None
    s = str(value)
    n_alnum = sum(1 for ch in s if ch.isalpha() or ch.isdigit())
    ks = _keystream(ctx.digest, s, "m", n_alnum)
    out = []
    j = 0
    for ch in s:
        if ch.isalpha():
            base = ord("A") if ch.isupper() else ord("a")
            out.append(chr(base + ks[j] % 26))
            j += 1
        elif ch.isdigit():
            out.append(str(ks[j] % 10))
            j += 1
        else:
            out.append(ch)
    return "".join(out)


def mask_name(value, ctx):
    """Keep the first word (first name), mask the remaining words (surnames).

    A single-word value is kept unchanged. Whitespace between words is preserved
    and each masked word keeps its shape via :func:`mask_text`. Used for person
    names (res_partner.name, which res.users inherits) so a masked record is
    still loosely recognisable by first name.
    """
    if value is None:
        return None
    s = str(value)
    parts = re.split(r"(\s+)", s)          # words + captured separators
    word_idx = [i for i, p in enumerate(parts) if p and not p.isspace()]
    if len(word_idx) <= 1:
        return s                            # single word -> keep
    for i in word_idx[1:]:                   # every word after the first
        parts[i] = mask_text(parts[i], ctx)
    return "".join(parts)


def _date_offset(ctx) -> "_datetime.timedelta":
    """The run-global shift: 1 to 365 days, in either direction, never zero.

    Magnitude and sign are drawn separately so that zero is not in the range.
    Drawing it as ``% 731 - 365`` put zero in the range once every ~731 runs
    (the run key is fresh ``os.urandom`` per run), and a zero shift makes
    ``date_shift`` a no-op on every date in the database — which does not show
    up as "nothing was masked" but as corruption, because ``_ensure_changed``
    then hands each unchanged value to :func:`_nudge`, and nudging a date's
    text mutates one character in place ("9999-12-31" -> "9999-12-32").
    """
    magnitude = ctx.int_for("date_shift", "global") % 365 + 1
    sign = -1 if ctx.int_for("date_shift", "global", "sign") % 2 else 1
    return _datetime.timedelta(days=sign * magnitude)


def _shift(point, offset):
    """Apply ``offset`` to a date/datetime, turning it around at the domain edge.

    Odoo pins sentinel dates to the edges of the range Python and PostgreSQL
    share — this database holds 60352 ``product_value.date`` rows sitting on
    ``0001-01-01 00:00:00`` — and about half of all runs draw a negative
    offset, so every one of those rows raised ``OverflowError: date value out
    of range`` and aborted the run. Shifting by the same magnitude in the
    opposite direction keeps the value inside the domain and still genuinely
    changed. Edge-pinned values all turn around together, so they stay
    correctly ordered among themselves; the alternative (clamping, i.e.
    leaving the value where it is) loses that and feeds :func:`_nudge` a date
    it can corrupt.
    """
    try:
        return point + offset
    except OverflowError:
        return point - offset


def date_shift(value, ctx):
    """Shift a date/timestamp by a single run-global offset (+/- 365 days).

    Every date in the run shifts by the *same* amount, so absolute values are
    anonymised while ordering and the gaps between related timestamps are
    preserved (before/after and duration logic still holds). Operates on the
    ISO text form; unparseable values are left unchanged rather than corrupted.
    Dates already at the edge of the representable range are shifted the other
    way instead of overflowing — see :func:`_shift`.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return value
    offset = _date_offset(ctx)
    # Pure date (YYYY-MM-DD).
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            return _shift(_datetime.date.fromisoformat(s), offset).isoformat()
        except ValueError:
            return value
    # Timestamp: tolerate a space or 'T' separator, drop any tz/fractional tail.
    head = s.replace("T", " ")
    core = head[:19]  # YYYY-MM-DD HH:MM:SS
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = _datetime.datetime.strptime(core, fmt)
        except ValueError:
            continue
        # isoformat, not strftime: glibc's %Y does not zero-pad years below
        # 1000, so a value shifted into year 1-999 came back as "1-05-19
        # 00:00:00" — text PostgreSQL rejects on the cast back. isoformat
        # always pads the year to four digits.
        return _shift(dt, offset).isoformat(sep=" ", timespec="seconds")
    return value


REGISTRY: dict[str, Callable] = {
    "keep": keep,
    "null": null,
    "empty": empty,
    "redact": redact,
    "fake_name": fake_name,
    "fake_first_name": fake_first_name,
    "fake_last_name": fake_last_name,
    "fake_company": fake_company,
    "fake_city": fake_city,
    "fake_email": fake_email,
    "fake_phone": fake_phone,
    "fake_street": fake_street,
    "fake_ip": fake_ip,
    "scramble_digits": scramble_digits,
    "scrub_text": scrub_text,
    "mask_text": mask_text,
    "mask_name": mask_name,
    "date_shift": date_shift,
}


# --- parametrized factories ------------------------------------------------

def partial_mask(keep_last: int = 4, mask_char: str = "*") -> Callable:
    def _fn(value, ctx):
        if value is None:
            return None
        s = str(value)
        if keep_last <= 0 or keep_last >= len(s):
            return mask_char * len(s)
        return mask_char * (len(s) - keep_last) + s[-keep_last:]

    return _fn


def generalize_number(bucket: float = 10) -> Callable:
    if bucket <= 0:
        raise ValueError("bucket must be positive")

    def _fn(value, ctx):
        if value is None:
            return None
        try:
            n = float(value)
        except (TypeError, ValueError):
            return value
        floored = (int(n // bucket)) * bucket
        return int(floored) if float(n).is_integer() and float(bucket).is_integer() else floored

    return _fn


def noise_number(pct: float = 10) -> Callable:
    if pct < 0:
        raise ValueError("pct must be >= 0")

    def _fn(value, ctx):
        if value is None:
            return None
        try:
            n = float(value)
        except (TypeError, ValueError):
            return value
        # deterministic offset in [-pct%, +pct%]
        span = 2 * pct
        frac = (ctx.int_for(value, "noise") % 10001) / 10000.0  # 0..1
        delta = (frac * span - pct) / 100.0
        result = n * (1 + delta)
        return int(round(result)) if float(n).is_integer() else result

    return _fn


FACTORIES: dict[str, Callable[..., Callable]] = {
    "partial_mask": partial_mask,
    "generalize_number": generalize_number,
    "noise_number": noise_number,
}


def _nudge(result, ctx):
    """Deterministically perturb a value that collided with its own input.

    Only called when a transform's output happened to equal its input — the
    goal is just to guarantee *some* visible change, not to pick a "good"
    anonymized value (the transform already had its chance to do that). Uses
    a full +/-1 unit rather than a tiny epsilon: a fractional nudge on a
    numeric column can get rounded away by the column's own storage precision
    (e.g. ``numeric(10,2)``), silently recreating the exact bug this exists to
    close.
    """
    sign = 1 if ctx.int_for(result, "nudge") % 2 == 0 else -1
    if isinstance(result, bool):
        return not result
    if isinstance(result, (int, float, _decimal.Decimal)):
        return result + sign
    if isinstance(result, (_datetime.datetime, _datetime.date)):
        return result + _datetime.timedelta(days=sign)
    if isinstance(result, str):
        for i, ch in enumerate(result):
            if ch.isalpha():
                flipped = ch.upper() if ch.islower() else ch.lower()
                return result[:i] + flipped + result[i + 1:]
        if not result:
            return "_"  # empty string: nothing to mutate in place, must grow
        # No letter anywhere (digits/punctuation only, e.g. a fixed-width
        # customs/reference number) — flip the last character IN PLACE rather
        # than appending. Appending would grow the string by one, which can
        # overflow a varchar(n) column already at its max length (seen live
        # on a 21-char customs number field where mask_text happened to
        # round-trip to itself).
        last = result[-1]
        bumped = str((int(last) + 1) % 10) if last.isdigit() else ("_" if last != "_" else "-")
        return result[:-1] + bumped
    if isinstance(result, bytes):
        if not result:
            return b"\x00"
        return result[:-1] + bytes([(result[-1] + 1) % 256])
    return result  # unknown type: leave as-is rather than guess wrong


def _values_equal(value, result) -> bool:
    """True if ``value`` and ``result`` represent the same value.

    Bare ``==`` isn't enough: the executor passes every non-text column's
    original through a ``::text`` cast, so ``value`` can be the string "1"
    while a numeric transform's ``result`` is the native int ``1`` — and for
    an unconstrained ``numeric`` column Postgres preserves each row's own
    decimal scale, so two equal values can render as different text ("0" vs
    "0.0"). A plain string comparison misses that second case; try comparing
    as ``Decimal`` first (order-of-magnitude/scale-independent) and only fall
    back to string equality for genuinely non-numeric values (text, dates).
    """
    if value == result:
        return True
    try:
        return _decimal.Decimal(str(value)) == _decimal.Decimal(str(result))
    except _decimal.InvalidOperation:
        return str(value) == str(result)


def _ensure_changed(fn: Callable) -> Callable:
    """Wrap a transform so it can never silently leave a value unmasked.

    Every transform here is expected to anonymize its input, but a
    percentage-based one like ``noise_number`` can round back to the exact
    original for small enough numbers — and the executor drops any (old, new)
    pair where they're equal *before* ever building the UPDATE, so that
    failure is invisible: the report claims the column was masked, but the
    row never actually changes. This closes that gap for every transform,
    current or future, not just the one it was first noticed on — except
    ``keep``, which means "leave this column alone" and must stay a genuine
    no-op.
    """
    if fn is keep:
        return fn

    def _wrapped(value, ctx):
        result = fn(value, ctx)
        if value is None or result is None:
            return result
        if not _values_equal(value, result):
            return result
        return _nudge(result, ctx)

    return _wrapped


def resolve(spec) -> Callable:
    """Turn a rule spec into a callable transform.

    ``spec`` is either a transform name (``"fake_email"``) or a single-key dict
    mapping a factory name to its kwargs (``{"partial_mask": {"keep_last": 2}}``).
    The returned callable is guaranteed to change any non-null input it
    doesn't return as ``None`` (see :func:`_ensure_changed`), except for the
    literal ``"keep"`` transform.
    """
    if isinstance(spec, str):
        try:
            fn = REGISTRY[spec]
        except KeyError:
            raise KeyError(f"unknown transform: {spec!r}") from None
    elif isinstance(spec, dict) and len(spec) == 1:
        (name, kwargs), = spec.items()
        if name in FACTORIES:
            fn = FACTORIES[name](**(kwargs or {}))
        else:
            raise KeyError(f"unknown transform factory: {name!r}")
    else:
        raise ValueError(f"invalid transform spec: {spec!r}")
    return _ensure_changed(fn)
