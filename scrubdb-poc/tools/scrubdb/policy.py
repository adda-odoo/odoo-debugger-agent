# Created by Odoo — Technical Support tooling (scrubdb).
"""Turn discovered schema (+ optional overrides) into a concrete Plan.

:func:`build_blanket_plan` is deny-by-default in the opposite sense from a
rule-driven tool: every column is masked unless it can be *proven* safe to
keep, via real schema introspection (PK/FK/UNIQUE/CHECK constraints, Odoo
``ir_*``/Selection/technical metadata) plus two content-based guards
(classify.py) for what schema alone can't see — an Odoo Reference field and a
selection-like column. A name matching :data:`RISKY_PATTERNS` (obvious PII
columns) is trusted ahead of those content guards. An optional ``overrides``
ruleset (``{"rules": {table: {col: spec}}}``) replaces the chosen transform
for specific columns.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field

from . import classify as _classify
from . import transforms as _transforms
from .introspect import Column

_logger = logging.getLogger("scrubdb.policy")

# Layer 1: obvious PII column names -> transform.
RISKY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"e?mail", re.I), "fake_email"),
    (re.compile(r"phone|mobile|fax|gsm", re.I), "fake_phone"),
    (re.compile(r"first[_ ]?name", re.I), "fake_first_name"),
    (re.compile(r"last[_ ]?name|surname", re.I), "fake_last_name"),
    (re.compile(r"(^|_)name$|partner_name|contact_name|display_name", re.I), "fake_name"),
    (re.compile(r"street|address|addr", re.I), "fake_street"),
    (re.compile(r"city|town", re.I), "fake_city"),
    (re.compile(r"zip|postal|postcode", re.I), "scramble_digits"),
    (re.compile(r"vat|tax|ssn|siret|siren|nif|fiscal", re.I), "scramble_digits"),
    (re.compile(r"iban|bank|account_number|acc_number|bic|swift", re.I), "scramble_digits"),
    (re.compile(r"password|passwd|secret|token|api[_ ]?key|private[_ ]?key", re.I), "redact"),
    (re.compile(r"(^|_)ip$|ip_address|remote_addr", re.I), "fake_ip"),
    (re.compile(r"note|comment|description|memo|remark", re.I), "scrub_text"),
]


@dataclass
class Action:
    table: str
    column: str
    transform_name: str
    fn: object  # callable(value, ctx) -> value
    data_type: str = ""  # information_schema data_type; drives json/jsonb handling
    unique: bool = False  # column has a UNIQUE/PK constraint -> keep masked values distinct
    is_nullable: bool = True  # NOT NULL columns can't be set to NULL (use an empty value)


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    skipped_rules: list[str] = field(default_factory=list)   # rules for absent table/col
    flagged: list[str] = field(default_factory=list)         # matched by name pattern
    classified: list[str] = field(default_factory=list)      # matched by content scan
    unmatched: list[str] = field(default_factory=list)       # textual, left for review
    excluded: list[str] = field(default_factory=list)        # blanket mode: kept, with reason
    truncate_tables: list[str] = field(default_factory=list)  # emptied wholesale (transient)

    def summary(self) -> dict:
        return {
            "actions": len(self.actions),
            "skipped_rules": len(self.skipped_rules),
            "flagged": len(self.flagged),
            "classified": len(self.classified),
            "unmatched": len(self.unmatched),
            "excluded": len(self.excluded),
            "truncate_tables": len(self.truncate_tables),
        }


def _name_pattern(column: str) -> str | None:
    for rx, transform in RISKY_PATTERNS:
        if rx.search(column):
            return transform
    return None


# --- blanket mode ----------------------------------------------------------
#
# Anonymize everything; keep only what preserves integrity or carries no PII.
# Type -> transform routing. Types not listed here are KEPT (uuid, boolean, xml,
# bytea, USER-DEFINED enums, ARRAY, and anything unknown) because masking them
# would break formats/constraints or add no privacy.
_BLANKET_TEXT = {"character varying", "varchar", "character", "char", "text",
                 "citext", "name"}
_BLANKET_JSON = {"json", "jsonb"}
_BLANKET_DATE = {"date", "timestamp without time zone", "timestamp with time zone",
                 "time without time zone", "time with time zone"}
_BLANKET_NUMERIC = {"integer", "bigint", "smallint", "numeric", "decimal",
                    "real", "double precision", "money"}

# Odoo audit/system columns — kept so ownership/timestamps stay coherent.
_SYSTEM_COLUMNS = {"id", "create_uid", "write_uid", "create_date", "write_date"}


def _base_name(name: str) -> str:
    """Strip a leading custom ``x_`` prefix so ``x_create_date``, ``x_active``,
    ``x_state``, ``x_res_id`` etc. match the special-column rules like their bare
    form.

    ``x_studio_`` is deliberately NOT collapsed to the bare name: those are
    arbitrary user-created Studio fields that could hold anything, so we don't
    risk treating them as special — they fall through to normal content/type
    masking (``x_studio_active`` → ``studio_active`` → masked, not kept).
    """
    if name.startswith("x_"):
        return name[2:]
    return name

# Integrity-protective default excludes: technical/selection/config fields that
# are plain varchar but hold enum-like values Odoo validates at the app layer,
# so masking them breaks the app while exposing no PII. Overridable / disable
# with use_default_excludes=False.
DEFAULT_EXCLUDE_PATTERNS = [
    "state", "*_state", "status", "*_status",
    "active", "lang", "*_lang",
    "type", "*_type", "code", "*_code",
    "currency", "model", "res_model", "*_model", "sequence", "color",
    "res_id",  # Odoo polymorphic record reference (with res_model) — not a real FK
    "tz", "*_tz",  # IANA timezone (res_users.tz, event.date_tz): masking → invalid tz
    # Odoo's _parent_store materialized path ("1/4/17/") — the index behind
    # every child_of/parent_of domain. It holds no PII (only ids the scrub
    # keeps anyway), it carries no FK constraint to protect it, and nothing
    # else here matches the name, so without this it is masked as ordinary
    # text and every hierarchy query in the database silently returns wrong
    # results — on stock_location, product_category, res_partner,
    # account_analytic_account, hr_department and any other _parent_store
    # model. (ir_ui_menu is spared only because ir_* tables are kept.)
    "parent_path",
]

# Odoo filestore/attachment payload columns, nulled by default in blanket mode.
# In a DB-only workflow the filestore isn't copied, so store_fname dangles and
# the in-DB blob (db_datas) / extracted text (index_content) are orphaned and may
# hold PII. Disable with keep_filestore=True / --keep-filestore. Only columns
# actually present in the schema are touched.
FILESTORE_COLUMNS = {
    "ir_attachment": ("store_fname", "db_datas", "index_content"),
}
_FILESTORE_PAIRS = {(t, c) for t, cols in FILESTORE_COLUMNS.items() for c in cols}

# Odoo technical/registry tables. Their text columns hold external IDs, model &
# field names, view XML, action/cron code, record-rule domains, config keys,
# sequences and module state — masking them corrupts the ORM/security/UI and
# makes the DB unusable. Blanket mode KEEPS every ir_* table by default (tag
# [technical]); override with exclude_technical=False / --include-technical.
# (Verified against Odoo 16-19 base/models/ir_*.py.)
TECHNICAL_TABLE_PREFIXES = ("ir_",)

# The few technical-table columns that DO hold secrets/PII and are scrubbed even
# though their table is otherwise kept. Applied before the [technical] exclusion.
# ir_attachment filestore columns are handled separately (FILESTORE_COLUMNS).
SECRET_COLUMNS = {
    "ir_mail_server": {
        "smtp_user": "mask_text", "smtp_pass": "null", "smtp_host": "mask_text",
        "smtp_ssl_certificate": "null", "smtp_ssl_private_key": "null",
    },
    "ir_logging": {
        "message": "null", "func": "null", "path": "null",
        "name": "null", "dbname": "null",
    },
    "ir_attachment": {
        "name": "mask_text", "description": "mask_text",
        "url": "null", "access_token": "null",
    },
    "ir_act_server": {"webhook_url": "null"},
}
_SECRET_SPEC = {(t, c): spec for t, cols in SECRET_COLUMNS.items()
                for c, spec in cols.items()}


def _is_technical(table: str) -> bool:
    return table.startswith(TECHNICAL_TABLE_PREFIXES)


# Person-name columns: keep the first name, mask the surname(s). res.users
# inherits its name from res_partner (delegation), so masking res_partner.name
# covers users too; hr_employee has its own name.
PERSON_NAME_COLUMNS = {
    ("res_partner", "name"),
    ("res_users", "name"),      # usually delegated (no column) — harmless if absent
    ("hr_employee", "name"),
}


def _blanket_transform(col: Column):
    if col.data_type in _BLANKET_TEXT or col.data_type in _BLANKET_JSON:
        return "mask_text"
    if col.data_type in _BLANKET_DATE:
        return "date_shift"
    if col.data_type in _BLANKET_NUMERIC:
        return {"noise_number": {}}
    return None  # keep


def _avg_len(values) -> float:
    vals = [v for v in values if v is not None]
    if not vals:
        return 0.0
    return sum(len(str(v)) for v in vals) / len(vals)


def _excluded_by_name(table: str, column: str, names, patterns) -> bool:
    key = f"{table}.{column}"
    base = _base_name(column)  # also match x_/x_studio_ variants
    if key in names or column in names or base in names:
        return True
    for pat in patterns:
        if (fnmatch.fnmatch(column, pat) or fnmatch.fnmatch(base, pat)
                or fnmatch.fnmatch(key, pat)):
            return True
    return False


def build_blanket_plan(
    schema_columns: list[Column],
    pk_columns=None,
    fk_columns=None,
    unique_columns=None,
    composite_unique_columns=None,
    check_columns=None,
    selection_columns=None,
    transient_tables=None,
    exclude_names=None,
    exclude_patterns=None,
    use_default_excludes: bool = True,
    drop_filestore: bool = True,
    exclude_technical: bool = True,
    overrides: dict | None = None,
    cur=None,
    schema: str = "public",
    sample: int = 300,
    enum_max_distinct: int = 25,
    big_text_threshold: int = 200,
    normalize_logins: bool = True,
) -> Plan:
    """Blanket anonymization: mask every column except what must be kept.

    Odoo TransientModel tables (wizards, ``res.config.settings``) are recorded in
    ``plan.truncate_tables`` and emptied by the executor rather than masked.

    Kept (recorded in ``plan.excluded`` with a reason tag):
      * ``[pk]`` primary-key columns and ``[fk]`` columns on either side of a
        foreign key (referential integrity);
      * ``[system]`` Odoo system/audit columns;
      * ``[check]`` columns under a real CHECK constraint (enum/format rules);
      * ``[selection]`` Odoo Selection fields (``ir_model_fields.ttype``);
      * ``[technical]`` every column of an Odoo ``ir_*`` registry table (unless
        ``exclude_technical`` is False) — except its known secret/PII columns,
        which are scrubbed;
      * ``[excluded]`` columns matched by ``exclude_names`` / ``exclude_patterns``
        (plus the integrity-protective defaults unless ``use_default_excludes``);
      * ``[unique]`` columns in a composite unique constraint, or non-text
        single-column unique columns (masking risks a UniqueViolation);
      * ``[reference]`` text columns holding an Odoo Reference (``model,id``)
        when sampled — only when ``cur`` is given;
      * ``[enum]`` text columns that *look* like a selection field when sampled
        (few, short, repeated values) — only when ``cur`` is given;
      * ``[type:X]`` columns whose type has no safe blanket transform (uuid,
        boolean, xml, bytea, enums, arrays, unknown).

    Everything else gets a type-appropriate transform. An optional ``overrides``
    ruleset (``{"rules": {table: {col: spec}}}``) replaces the chosen transform
    for specific columns (and skips the enum guard). Columns under a UNIQUE/PK
    constraint are flagged ``unique`` so the executor keeps masked values distinct.

    Before the ``[reference]``/``[enum]`` content guards run, a text column
    whose name matches :data:`RISKY_PATTERNS` is routed straight to its
    designated transform and recorded in ``plan.flagged`` — obvious PII names
    (phone, email, VAT, IBAN, ...) are trusted over content sampling, so a
    low-cardinality real value (e.g. a handful of shared company phone
    numbers) can't be misread as a Selection field and skipped.
    """
    pk_columns = set(pk_columns or set())
    fk_columns = set(fk_columns or set())
    unique_columns = set(unique_columns or set())
    composite_unique_columns = set(composite_unique_columns or set())
    check_columns = set(check_columns or set())
    selection_columns = set(selection_columns or set())
    transient_tables = set(transient_tables or set())
    exclude_names = set(exclude_names or [])
    patterns = list(exclude_patterns or [])
    if use_default_excludes:
        patterns = DEFAULT_EXCLUDE_PATTERNS + patterns
    rules = (overrides or {}).get("rules", {}) or {}

    plan = Plan()
    # Odoo TransientModel tables (wizards / res.config.settings) are emptied
    # wholesale — the rows are ephemeral, so there's nothing to preserve.
    plan.truncate_tables = sorted({c.table for c in schema_columns
                                   if c.table in transient_tables})
    total = len(schema_columns)
    for idx, col in enumerate(schema_columns, 1):
        if cur is not None and idx % 200 == 0:
            _logger.info("  …analysed %d/%d columns", idx, total)
        key = f"{col.table}.{col.name}"
        pair = (col.table, col.name)
        if col.table in transient_tables:
            continue  # table is truncated (plan.truncate_tables); no column work
        if pair in pk_columns:
            plan.excluded.append(f"{key} [pk]")
            continue
        if pair in fk_columns:
            plan.excluded.append(f"{key} [fk]")
            continue
        if _base_name(col.name) in _SYSTEM_COLUMNS:
            plan.excluded.append(f"{key} [system]")
            continue
        if pair in check_columns:
            plan.excluded.append(f"{key} [check]")
            continue
        if pair in selection_columns:
            # Odoo Selection field (fixed value set validated by the ORM) —
            # masking it to an arbitrary string breaks the ORM.
            plan.excluded.append(f"{key} [selection]")
            continue
        if normalize_logins and pair == ("res_users", "login"):
            # Owned by finalization (email → user<n>); don't blanket-mask it,
            # or non-email logins like 'admin' would be scrambled unusable.
            plan.excluded.append(f"{key} [finalize]")
            continue
        if _excluded_by_name(col.table, col.name, exclude_names, patterns):
            plan.excluded.append(f"{key} [excluded]")
            continue

        override = rules.get(col.table, {}).get(col.name)
        if override is not None:
            spec = override
        elif drop_filestore and pair in _FILESTORE_PAIRS:
            # Drop dangling filestore refs and orphaned attachment payload.
            spec = "null"
        elif pair in _SECRET_SPEC:
            # Secret/PII column inside an otherwise-kept technical table.
            spec = _SECRET_SPEC[pair]
        elif exclude_technical and _is_technical(col.table):
            # Odoo registry/metadata table — masking would break the database.
            plan.excluded.append(f"{key} [technical]")
            continue
        else:
            # Unique-constraint safety. Masking a column in a COMPOSITE unique
            # constraint, or a non-text single-column unique (can't be safely
            # suffixed), risks a UniqueViolation — keep those. Single-column
            # unique TEXT is masked with a deterministic suffix (below).
            if pair in composite_unique_columns or (
                pair in unique_columns and col.data_type not in _BLANKET_TEXT
            ):
                plan.excluded.append(f"{key} [unique]")
                continue
            spec = _blanket_transform(col)
            if spec is None:
                plan.excluded.append(f"{key} [type:{col.data_type}]")
                continue
            if pair in PERSON_NAME_COLUMNS:
                # Person name: keep first word, mask the rest (skip content guards).
                spec = "mask_name"
                fn = _transforms.resolve(spec)
                plan.actions.append(
                    Action(col.table, col.name, spec, fn, col.data_type,
                           pair in unique_columns, col.is_nullable))
                continue
            # Layer 1 (RISKY_PATTERNS, a name-based table of obvious PII
            # columns): trusted over content sampling and
            # skips the enum/reference guards below. Without this, a column
            # like a shared company phone number — a handful of distinct
            # values repeated across many rows — reads exactly like a real
            # Selection field to looks_like_enum() and gets excluded as
            # [enum], never masked at all (confirmed on res_partner.phone in
            # production; that heuristic has no name-based safety net of its
            # own, unlike PERSON_NAME_COLUMNS above).
            if col.data_type in _BLANKET_TEXT:
                name_transform = _name_pattern(col.name)
                if name_transform:
                    fn = _transforms.resolve(name_transform)
                    plan.actions.append(
                        Action(col.table, col.name, name_transform, fn, col.data_type,
                               pair in unique_columns, col.is_nullable))
                    plan.flagged.append(key)
                    continue
            # Content guards: sample text columns once and keep those that are
            # Odoo Reference fields ("model,id") or look like selection fields;
            # route large text to a fast server-side redaction.
            if cur is not None and col.data_type in _BLANKET_TEXT:
                _logger.debug("sampling %s for content guards", key)
                values = _classify.sample_column(cur, schema, col.table, col.name, sample)
                if _classify.looks_like_reference(values):
                    plan.excluded.append(f"{key} [reference]")
                    continue
                if _classify.looks_like_enum(values, max_distinct=enum_max_distinct):
                    plan.excluded.append(f"{key} [enum]")
                    continue
                if big_text_threshold and _avg_len(values) >= big_text_threshold:
                    # Big text (HTML bodies, long notes): mask server-side, no
                    # value round-trip. Loses shape but is dramatically faster.
                    spec = "hash_text"

        # hash_text is server-side only (no Python fn); everything else resolves.
        fn = None if spec == "hash_text" else _transforms.resolve(spec)
        name = spec if isinstance(spec, str) else next(iter(spec))
        plan.actions.append(
            Action(col.table, col.name, name, fn, col.data_type,
                   pair in unique_columns, col.is_nullable)
        )
    return plan
