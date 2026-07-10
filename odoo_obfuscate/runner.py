"""Anonymization runner — Phase 2+."""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from pathlib import Path

import psycopg2

import filestore, generators as gen, infra
from db import column_info, connect, jsonb_keys, odoo_version, pg_version, table_exists
from identity import PartnerIdentity, build_partner_identities
from manifest import RunManifest

log = logging.getLogger(__name__)

# Maps res_partner column names to PartnerIdentity attributes.
# Only columns where cross-table consistency matters (e.g. email drives login).
_PARTNER_COL_TO_IDENTITY: dict[str, str] = {
    "name":          "name",
    "display_name":  "name",
    "complete_name": "name",
    "email":         "email",
    "phone":         "phone",
    "mobile":        "mobile",
    "street":        "street",
    "street2":       "street2",
    "zip":           "zip",
    "city":          "city",
    "vat":           "vat",
    "website":       "website",
}


def _can_use_replica_role(conn) -> bool:
    with conn.cursor() as cur:
        try:
            cur.execute("SET session_replication_role = 'replica'")
            cur.execute("SET session_replication_role = 'origin'")
        except psycopg2.errors.InsufficientPrivilege:
            conn.rollback()
            return False
    conn.rollback()
    return True


def _process_table(
    conn,
    table: str,
    fields: dict[str, dict],
    identity_map: dict[int, PartnerIdentity],
    batch_size: int = 10_000,
    manifest: RunManifest | None = None,
) -> int:
    """Anonymize one table via temp-table bulk UPDATE. Returns row count processed."""
    if not table_exists(conn, table):
        log.info("Table %s absent — skipping", table)
        if manifest:
            manifest.record_absent_table(table)
        return 0

    # Filter to columns that actually exist; record data_type for cast decisions
    active: dict[str, dict] = {}
    for col, props in fields.items():
        info = column_info(conn, table, col)
        if info is None:
            log.info("Column %s.%s absent — skipping", table, col)
            if manifest:
                manifest.record_absent_column(table, col)
            continue
        active[col] = {**props, "_is_jsonb": info["data_type"] == "jsonb",
                       "_data_type": info["data_type"]}

    if not active:
        return 0

    # Auto-sync display_name when name is being updated but display_name is not listed
    if "name" in active and "display_name" not in active:
        dn_info = column_info(conn, table, "display_name")
        if dn_info is not None:
            active["display_name"] = {
                **active["name"],
                "coherent_with": "name",
                "_is_jsonb": dn_info["data_type"] == "jsonb",
                "_data_type": dn_info["data_type"],
            }

    # Pre-fetch locale keys for JSONB columns
    jsonb_keys_map: dict[str, list[str]] = {}
    for col, props in active.items():
        if props["_is_jsonb"]:
            keys = jsonb_keys(conn, table, col)
            jsonb_keys_map[col] = keys if keys else ["en_US"]

    is_res_partner = table == "res_partner"

    # Determine extra columns needed for the SELECT
    extra_sel: set[str] = set()
    for col, props in active.items():
        gtype = props.get("type", "clear")
        if gtype == "from_partner":
            extra_sel.add("partner_id")
        elif gtype == "date_shift":
            extra_sel.add(col)

    sel_cols = ["id"] + sorted(extra_sel)
    col_order = list(active.keys())
    col_defs  = ", ".join(f"{c} TEXT" for c in col_order)
    temp      = f"_anon_{table}"

    _CAST = {
        "jsonb":                       "::jsonb",
        "bytea":                       "::bytea",
        "date":                        "::date",
        "timestamp without time zone": "::timestamp",
        "timestamp with time zone":    "::timestamptz",
        "integer":                     "::integer",
        "bigint":                      "::bigint",
        "boolean":                     "::boolean",
    }
    set_clauses = []
    for col in col_order:
        cast = _CAST.get(active[col]["_data_type"], "")
        set_clauses.append(f"{col} = a.{col}{cast}")
    set_sql = ", ".join(set_clauses)

    start = time.perf_counter()
    total_processed = 0
    last_id = -1

    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(sel_cols)} FROM {table} "
                f"WHERE id > %s ORDER BY id LIMIT %s",
                (last_id, batch_size),
            )
            batch = cur.fetchall()

        if not batch:
            break

        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {temp}")
            cur.execute(f"CREATE UNLOGGED TABLE {temp} (id BIGINT, {col_defs})")

        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)

        for row in batch:
            row_id = row[0]
            extra_vals = dict(zip(sorted(extra_sel), row[1:]))
            partner_identity = identity_map.get(row_id) if is_res_partner else None

            raw_values: dict[str, object] = {}
            out_row = [str(row_id)]

            for col in col_order:
                props = active[col]
                gtype = props.get("type", "clear")

                coherent_with = props.get("coherent_with")
                if coherent_with and coherent_with in raw_values:
                    raw_val = raw_values[coherent_with]

                elif is_res_partner and partner_identity is not None:
                    attr = _PARTNER_COL_TO_IDENTITY.get(col)
                    raw_val = (
                        getattr(partner_identity, attr)
                        if attr is not None
                        else gen.generate(props, row_id)
                    )

                else:
                    partner_id_val = extra_vals.get("partner_id")
                    identity = (
                        identity_map.get(partner_id_val)
                        if partner_id_val is not None else None
                    )
                    original = extra_vals.get(col) if gtype == "date_shift" else None
                    raw_val = gen.generate(props, row_id, original=original, identity=identity)

                raw_values[col] = raw_val

                if props["_is_jsonb"] and raw_val is not None:
                    keys = jsonb_keys_map.get(col, ["en_US"])
                    out_val = gen.wrap_jsonb(str(raw_val), keys)
                else:
                    out_val = raw_val

                out_row.append(str(out_val) if out_val is not None else "")

            writer.writerow(out_row)

        buf.seek(0)
        try:
            with conn.cursor() as cur:
                cur.copy_expert(f"COPY {temp} FROM STDIN WITH (FORMAT CSV, NULL '')", buf)
                cur.execute(f"UPDATE {table} SET {set_sql} FROM {temp} a WHERE {table}.id = a.id")
                cur.execute(f"DROP TABLE {temp}")
        except psycopg2.Error as exc:
            conn.rollback()
            raise RuntimeError(
                f"Database error while updating {table} (likely a trigger or constraint firing "
                f"without session_replication_role privilege): {exc}"
            ) from exc

        conn.commit()
        last_id = batch[-1][0]
        total_processed += len(batch)

    if total_processed == 0:
        return 0

    # VACUUM ANALYZE once after all batches (cannot run inside a transaction)
    try:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"VACUUM ANALYZE {table}")
    except Exception as exc:
        log.warning("VACUUM ANALYZE %s failed (non-critical): %s", table, exc)
    finally:
        conn.autocommit = False

    elapsed = time.perf_counter() - start
    log.info("%-40s %6d rows  %.2fs", table, total_processed, elapsed)
    return total_processed


# Categories that should be anonymized when no specific generator matches.
_PII_CATEGORIES = {
    "direct_pii",
    "communication",
    "authentication",
    "financial",
    "business_sensitive",
    "quasi_identifier",
    "health",
    "hr",
}

# Field types that can carry meaningful sensitive text/data.
_TEXT_TYPES = {"Char", "Text", "Html", "Binary", "Json"}

# Field-name patterns that map to generator types. Each entry is
# (regex, generator_type, requires_text_type).
_FIELD_PATTERNS = [
    # Names
    (r"^(company_name|commercial_company_name)$", "company_name", False),
    (r"^(complete_name|display_name|legal_name|contact_name|partner_name|full_name|first_name|last_name|middle_name|maiden_name|nickname|alias)$", "full_name", False),

    # Contact
    (r"^(email|private_email|work_email|personal_email|emergency_email)$", "email", False),
    (r"^(phone|mobile|fax|private_phone|private_mobile|work_phone|work_mobile|personal_phone|personal_mobile|home_phone|emergency_phone|emergency_mobile)$", "phone", False),
    (r"^(street|address|contact_address|formatted_address|postal_address|delivery_address|invoice_address|home_address|emergency_address)$", "street", False),
    (r"^(street2)$", "street2", False),
    (r"^(zip|postal_code|postcode)$", "zip", False),
    (r"^city$", "city", False),

    # Identifiers
    (r"^(vat|tax_id|vat_number|tin)$", "vat", False),
    (r"^(ssn|sin|social_security|social_security_number)$", "ssn", False),
    (r"^(passport|passport_id|passport_number)$", "passport", False),
    (r"^(barcode|qrcode|rfid|ean|upc)$", "barcode", False),
    (r"^(company_registry|registry_number|chamber_of_commerce|commercial_register|trade_register|duns)$", "alphanum", False),
    (r"^(drivers_license|driver_license|id_card|id_number|identification_id|identity_number|national_id_number|citizen_id|resident_id)$", "alphanum", False),

    # Dates
    (r"^(birthday|birthdate|date_of_birth|dob|date_birth)$", "date_shift", False),

    # URLs
    (r"^(website|linkedin|twitter|facebook|social_media|skype|whatsapp|telegram|instagram|youtube|github|gitlab|map_url|map_link)$", "website", False),

    # Financial
    (r"^(bank_account|acc_number|account_number|sanitized_acc_number|routing_number|sort_code|clearing_number|bank_code|branch_code)$", "iban", False),
    (r"^(iban|iban_number)$", "iban", False),
    (r"^(bic|swift|bank_bic|swift_code)$", "alphanum", False),
    (r"^(acc_holder_name|account_holder|holder_name|bank_account_name)$", "full_name", False),

    # Secrets
    (r"^(password|passwd|pwd|new_password|old_password|confirm_password|current_password|reset_password|temp_password|user_password|smtp_pass|imap_pass|pop_pass|ftp_pass|db_password|db_pass|admin_pass|root_password|ldap_password|oauth_password|proxy_password|vpn_password|wifi_password|sip_password)$", "clear", False),
    (r"^(token|access_token|refresh_token|auth_token|api_token|csrf_token|verification_token|reset_token|invite_token|share_token|download_token|preview_token|unsubscribe_token|sync_token|session_token|id_token|oauth_token|bearer_token)$", "clear", False),
    (r"^(api_key|api_key_id|api_secret|secret_key|client_secret|app_secret|consumer_secret|shared_secret|signing_secret|webhook_secret)$", "clear", False),
    (r"^(private_key|public_key|ssl_key|ssh_key|pgp_key|gpg_key|ssl_certificate|tls_certificate|client_certificate|server_certificate|signing_certificate|code_signing_certificate|cert_pem|certificate_pem|cert_file|certificate_file|x509_certificate|x509_cert|public_cert|private_cert|pem|csr|x509|rsa_key|ecdsa_key|ed25519_key|dsa_key|dh_key|symmetric_key|aes_key|des_key)$", "clear", False),
    (r"^(credential|credentials|auth_code|oauth_code|authorization_code|session_id|session_key|cookie|csrf|nonce|challenge|otp|totp_secret|hotp_secret|seed|recovery_code|backup_code|pin|pin_code|passcode)$", "clear", False),

    # Images
    (r"^(avatar|image|image_1920|image_128|image_1024|image_512|image_256|photo|portrait|selfie|signature|sign|digital_signature)$", "clear", False),

    # Text / Communication
    (r"^(body|message|note|notes|internal_notes|memo|description|desc|comment|comments|feedback|reason|justification|narration|observation|finding|diagnosis|remark|remarks|annotation|transcript|dialogue|conversation|chat|thread)$", "clear", True),
    (r"^(subject|email_subject|title|email_from|reply_to|reply_to_address|sender|sender_address|sender_email|recipient|recipient_address|to_address|cc|bcc|sent_to|sent_from|originator|return_path|envelope_from|mail_from|rcpt_to)$", "clear", False),
    (r"^(old_value_char|old_value_text|old_value_datetime|old_value_integer|old_value_float|new_value_char|new_value_text|new_value_datetime|new_value_integer|new_value_float)$", "clear", False),

    # Other IP / geo
    (r"^(ip_address|ip|remote_ip|client_ip|user_agent|browser|device_id|mac_address|imei|serial_number|fingerprint)$", "clear", False),
    (r"^(latitude|longitude|geo|gps|partner_latitude|partner_longitude|location|geo_location|geolocation|coordinates|coordinate|lat|lng|long)$", "clear", False),
]

_PERSON_MODELS = {
    r"res\.partner", r"res\.users", r"hr\.employee", r"hr\.applicant", r"hr\.candidate",
    r"crm\.lead", r"event\.registration", r"event\.attendee", r"mailing\.contact",
    r"res\.partner\.bank", r"portal\.wizard\.user", r"auth\.totp\.wizard",
    r"mail\.followers", r"mail\.alias", r"res\.users\.apikeys", r"res\.users\.deletion",
    r"gamification\.badge\.user", r"website\.visitor", r"slide\.slide",
    r"survey\.user_input", r"calendar\.attendee", r"project\.task",
    r"resource\.resource",
}

_DOCUMENT_MODELS = {
    r"sale\.order", r"purchase\.order", r"account\.move", r"account\.invoice",
    r"project\.project", r"project\.task", r"helpdesk\.ticket", r"event\.event",
    r"mrp\.production", r"stock\.picking", r"stock\.move", r"pos\.order",
    r"repair\.order", r"subscription\.order", r"fleet\.vehicle", r"fleet\.vehicle\.log",
    r"maintenance\.request", r"quality\.check", r"mrp\.workorder",
}


def _model_matches(model_name: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        if re.search(pattern, model_name):
            return True
    return False


def _get_generator_for_field(
    field_name: str,
    field_type: str,
    string_val: str,
    help_val: str,
    model_name: str,
    comodel_name: str = "",
) -> str | None:
    """Return a generator type for a field based on name/type heuristics.

    This is the same heuristic previously provided by analyzer.py so that
    curated CSV files without an explicit generator column can still be
    converted into obfuscation instructions.
    """
    # 1. Explicit field name patterns
    for pattern, generator_type, req_text in _FIELD_PATTERNS:
        if re.search(pattern, field_name, re.IGNORECASE):
            if req_text and field_type not in _TEXT_TYPES:
                continue
            return generator_type

    # 2. Special handling for plain "name" field
    if field_name == "name":
        if _model_matches(model_name, _PERSON_MODELS):
            return "full_name"
        if _model_matches(model_name, _DOCUMENT_MODELS):
            return "alphanum"
        if string_val and re.search(r"\b(name|nom|nombre|naam)\b", string_val, re.IGNORECASE):
            return "full_name"

    # 3. Relational fields pointing to person models
    if field_type in ("Many2one", "One2many", "Many2many") and comodel_name:
        if _model_matches(comodel_name, _PERSON_MODELS):
            return "clear"

    # 4. Text and HTML types without matches are typically notes/descriptions
    if field_type in ("Html", "Text"):
        return "clear"

    # 5. Fallback for explicit binary
    if field_type == "Binary":
        return "clear"

    return None


def _load_pii_csv(csv_path: Path) -> dict[str, dict]:
    """Load curated PII fields from a version-specific CSV."""
    results: dict[str, dict] = {}
    categories: dict[str, str] = {}
    field_info: dict[str, tuple[str, str, str, str]] = {}

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            model = row.get("model", "").strip()
            field = row.get("field", "").strip()
            if not model or not field:
                continue

            table_name = model.replace(".", "_")
            key = f"{table_name}.{field}"

            field_type = row.get("field_type", "").strip()
            string = row.get("string", "").strip()
            help_text = row.get("help", "").strip()
            category = row.get("category", "").strip()

            # Prefer direct_pii over other categories for duplicate keys.
            existing = categories.get(key)
            if existing == "direct_pii" and category != "direct_pii":
                continue
            categories[key] = category
            field_info[key] = (field, field_type, string, help_text)

    for key, category in categories.items():
        field, field_type, string, help_text = field_info[key]
        # model_name is rebuilt from the key prefix (table_name) so the
        # plain-"name" heuristics in _get_generator_for_field resolve correctly.
        table_name, _ = key.split(".", 1)
        model_name = table_name.replace("_", ".")
        generator = _get_generator_for_field(
            field, field_type, string, help_text, model_name, ""
        )
        if generator:
            props: dict[str, object] = {"type": generator}
            if generator in {"vat", "barcode", "passport"} or field in {
                "vat",
                "barcode",
                "passport_id",
                "identification_id",
                "company_registry",
                "global_location_number",
            }:
                props["unique_suffix"] = True
            results[key] = props
        elif category in _PII_CATEGORIES:
            results[key] = {"type": "clear"}

    # Hardcoded overrides so cross-table coherence (login/email, name/display_name)
    # stays intact. These used to live in analyzer.py.
    overrides = {
        "res_partner.name": {"type": "full_name", "jsonb": True, "coherent_with": "display_name"},
        "res_partner.display_name": {"type": "full_name", "jsonb": True, "coherent_with": "name"},
        "res_partner.complete_name": {"type": "full_name", "jsonb": True},
        "res_partner.company_name": {"type": "company_name", "jsonb": True},
        "res_partner.email": {"type": "email", "unique_suffix": True},
        "res_users.login": {"type": "from_partner", "field": "email", "via": "partner_id"},
        "hr_employee.name": {"type": "full_name", "jsonb": True},
        "res_company.name": {"type": "company_name", "jsonb": True},
        "product_template.name": {"type": "product_name", "jsonb": True},
        "product_product.name": {"type": "product_name", "jsonb": True},
        "mail_message.body": {"type": "literal", "value": "<p>Anonymized.</p>"},
        "mail_mail.body_html": {"type": "literal", "value": "<p>Anonymized.</p>"},
    }
    results.update(overrides)
    return results


def _build_fields_section(args, manifest: RunManifest) -> dict[str, dict]:
    """Load the curated PII fields section for the current Odoo version.

    Obfuscation is strictly CSV-driven. A version-specific CSV must exist
    under pii_fields/; the previous AST-based analyzer is now a standalone
    script (tools/analyze_pii_fields.py) that can be used to generate or
    refresh these CSVs.
    """
    odoo_ver = manifest.odoo_ver
    csv_path = Path(__file__).with_name("pii_fields") / f"sensitive_fields_{odoo_ver}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"No curated PII fields CSV found for Odoo {odoo_ver}: {csv_path}. "
            "Use tools/analyze_pii_fields.py to generate one, or provide a "
            "suitable CSV in the pii_fields/ directory."
        )

    log.info("Using curated PII fields for Odoo %s: %s", odoo_ver, csv_path)
    return _load_pii_csv(csv_path)


def run_anonymize(args) -> None:
    """Entry point for the `run` command."""
    dry_run = getattr(args, "dry_run", False)
    db_url = getattr(args, "db_url", None)
    conn = connect(db_url=db_url) if db_url else connect(dbname=args.db)

    # Collect version info for manifest before anything else.
    pg_ver = pg_version(conn)
    odoo_ver = odoo_version(conn)
    dbname = getattr(args, "db", None) or db_url or "unknown"

    manifest = RunManifest(dbname, pg_ver, odoo_ver, dry_run=dry_run)

    # Locale for Faker (default en_US).
    locale = getattr(args, "locale", "en_US") or "en_US"
    if locale != "en_US":
        gen.configure_locale(locale)

    fields_section = _build_fields_section(args, manifest)

    # Group by table (key format: "table_name.column").
    by_table: dict[str, dict[str, dict]] = {}
    for key, props in fields_section.items():
        dot = key.rfind(".")
        if dot == -1:
            continue
        table = key[:dot]
        col = key[dot + 1:]
        by_table.setdefault(table, {})[col] = props

    # ── Dry-run path ────────────────────────────────────────────────────────
    if dry_run:
        log.info("[DRY RUN] Counting rows — no changes will be written.")
        for table in by_table:
            if table_exists(conn, table):
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cur.fetchone()[0]
                manifest.record_table(table, count)
            else:
                manifest.record_absent_table(table)
        conn.close()
        manifest.complete()
        manifest.print_summary()
        return

    # ── Live run ────────────────────────────────────────────────────────────

    # Idempotency guard
    if not getattr(args, "force", False):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM ir_config_parameter "
                "WHERE key = 'anon_tool.run_completed' LIMIT 1"
            )
            if cur.fetchone():
                log.error("Already anonymized. Use --force to re-run.")
                conn.close()
                return

    has_errors = False

    log.info("--- Step 1b: pre-neutralization ---")
    try:
        infra.pre_neutralize(conn)
    except Exception as exc:
        log.warning("Unhandled error in pre_neutralize: %s", exc)
        has_errors = True

    replica_role_set = False
    if _can_use_replica_role(conn):
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = 'replica'")
        replica_role_set = True
        log.info("session_replication_role = replica (triggers suppressed)")
    else:
        log.warning(
            "Cannot set session_replication_role — insufficient privilege. "
            "Proceeding without trigger suppression."
        )

    identity_map = build_partner_identities(conn)
    log.info("Built %d partner identities", len(identity_map))
    log.info("--- Step 3: anonymize registered fields ---")

    batch_size = getattr(args, "batch_size", 10_000)
    total = 0
    for table, fields in by_table.items():
        try:
            count = _process_table(
                conn, table, fields, identity_map,
                batch_size=batch_size, manifest=manifest,
            )
            manifest.record_table(table, count)
            total += count
        except Exception as exc:
            has_errors = True
            try:
                conn.rollback()
            except Exception:
                pass
            log.warning("Skipping %s — unhandled %s: %s", table, type(exc).__name__, exc)

    try:
        conn.rollback()
    except Exception:
        pass

    log.info("--- Steps 4-7: infrastructure scrub ---")
    try:
        infra.post_scrub(conn)
    except Exception as exc:
        log.warning("Unhandled error in post_scrub: %s", exc)
        has_errors = True

    log.info("--- Step 8: filestore scrub ---")
    dbname_for_filestore = getattr(args, "db", None) or "anon"
    try:
        filestore.run_filestore_scrub(conn, args, dbname_for_filestore)
    except Exception as exc:
        log.warning("Unhandled error in filestore scrub: %s", exc)
        has_errors = True

    if getattr(infra, "ERRORS", []):
        has_errors = True
        log.warning("Infrastructure/filestore scrub reported errors: %s", infra.ERRORS)

    if replica_role_set:
        try:
            with conn.cursor() as cur:
                cur.execute("SET session_replication_role = 'origin'")
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

    if not has_errors:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ir_config_parameter (key, value) VALUES "
                "('anon_tool.run_completed', 'true') "
                "ON CONFLICT (key) DO UPDATE SET value = 'true'"
            )
        conn.commit()
    else:
        log.error("Anonymization completed with errors. 'anon_tool.run_completed' flag NOT set.")

    log.info("Anonymization complete. %d rows processed across %d tables.", total, len(by_table))
    conn.close()

    manifest.complete()
    manifest.print_summary()
