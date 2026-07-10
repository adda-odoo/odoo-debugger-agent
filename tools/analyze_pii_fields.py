#!/usr/bin/env python3
"""Standalone PII field analyzer for Odoo addons.

Scans Odoo addon Python source code and emits a CSV of fields that look like
personally identifiable information (PII), along with a suggested generator
type for later obfuscation.

Usage:
    python tools/analyze_pii_fields.py \
        --addons-paths /path/to/odoo/addons /path/to/enterprise

    python tools/analyze_pii_fields.py \
        --addons-paths /path/to/odoo/addons \
        --output pii_fields.csv
"""
from __future__ import annotations

import argparse
import ast
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

# Field types that can carry meaningful sensitive text/data
TEXT_TYPES = {"Char", "Text", "Html", "Binary", "Json"}

PERSON_MODELS = {
    r"res\.partner", r"res\.users", r"hr\.employee", r"hr\.applicant", r"hr\.candidate",
    r"crm\.lead", r"event\.registration", r"event\.attendee", r"mailing\.contact",
    r"res\.partner\.bank", r"portal\.wizard\.user", r"auth\.totp\.wizard",
    r"mail\.followers", r"mail\.alias", r"res\.users\.apikeys", r"res\.users\.deletion",
    r"gamification\.badge\.user", r"website\.visitor", r"slide\.slide",
    r"survey\.user_input", r"calendar\.attendee", r"project\.task",
    r"resource\.resource",
}

DOCUMENT_MODELS = {
    r"sale\.order", r"purchase\.order", r"account\.move", r"account\.invoice",
    r"project\.project", r"project\.task", r"helpdesk\.ticket", r"event\.event",
    r"mrp\.production", r"stock\.picking", r"stock\.move", r"pos\.order",
    r"repair\.order", r"subscription\.order", r"fleet\.vehicle", r"fleet\.vehicle\.log",
    r"maintenance\.request", r"quality\.check", r"mrp\.workorder",
}

# (pattern, generator_type, requires_text_type)
FIELD_PATTERNS = [
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


def _get_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if hasattr(ast, "Str") and isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _get_str(node.left), _get_str(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            s = _get_str(value)
            if s is None:
                return None
            parts.append(s)
        return "".join(parts)
    return None


def _get_list(node):
    vals = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            s = _get_str(elt)
            if s is not None:
                vals.append(s)
    return vals


def _get_dict_keys(node):
    keys = []
    if isinstance(node, ast.Dict):
        for key in node.keys:
            s = _get_str(key)
            if s is not None:
                keys.append(s)
    return keys


def _resolve(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts = []
        n = node
        while isinstance(n, ast.Attribute):
            parts.append(n.attr)
            n = n.value
        if isinstance(n, ast.Name):
            parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def _get_comodel_name(val):
    if val.args:
        comodel = _get_str(val.args[0])
        if comodel:
            return comodel
    for kw in val.keywords:
        if kw.arg == "comodel_name":
            return _get_str(kw.value)
    return None


def _model_matches(model_name, patterns):
    for pattern in patterns:
        if re.search(pattern, model_name):
            return True
    return False


def get_generator_for_field(field_name, field_type, string_val, help_val, model_name, comodel_name=""):
    """Return the suggested generator type for a field, or None if not PII."""
    # 1. Explicit field name patterns
    for pattern, generator_type, req_text in FIELD_PATTERNS:
        if re.search(pattern, field_name, re.IGNORECASE):
            if req_text and field_type not in TEXT_TYPES:
                continue
            return generator_type

    # 2. Special handling for plain "name" field
    if field_name in ("name", "_name"):
        if _model_matches(model_name, PERSON_MODELS):
            return "full_name"
        if _model_matches(model_name, DOCUMENT_MODELS):
            return "alphanum"
        if string_val and re.search(r"\b(name|nom|nombre|naam)\b", string_val, re.IGNORECASE):
            doc_indicators = (
                r"\b(reference|number|code|label|stage|status|state|type|category|tag|"
                r"sequence|order|priority|level|rank|score|rate|ratio|percentage|count|"
                r"quantity|amount|total|sum|balance|period|date|time|year|month|day|hour|"
                r"minute|second|duration|interval|range|zone|region|area|sector|industry|"
                r"market|segment|channel|medium|source|campaign|program|project|task|"
                r"activity|event|session|course|class|lesson|module|chapter|section|"
                r"paragraph|sentence|word|term|phrase|expression|statement|declaration|"
                r"definition|description|explanation|instruction|direction|guideline|"
                r"policy|procedure|process|workflow|pipeline|route|path|way|method|mode|"
                r"manner|style|format|layout|design|pattern|template|model|schema|"
                r"structure|framework|architecture|system|platform|application|software|"
                r"hardware|firmware|middleware|network|database|dataset|datastore|"
                r"repository|warehouse|lake|pool|bucket|container|volume|drive|disk|tape|"
                r"card|stick|key|lock|gate|door|window|wall|floor|ceiling|roof|foundation|"
                r"base|ground|soil|earth|land|terrain|surface|face|side|edge|corner|point|"
                r"spot|place|site|location|position|coordinate|address|destination|origin|"
                r"source|start|beginning|end|finish|close|opening|entrance|exit|entry|"
                r"access|approach|pathway|walkway|roadway|highway|freeway|expressway|"
                r"turnpike|tollway|parkway|causeway|bridge|tunnel|overpass|underpass|"
                r"railway|subway|metro|tram|monorail|maglev|bus|coach|shuttle|van|truck|"
                r"lorry|rig|tractor|trailer|caravan|camper|motorhome|rv|boat|ship|vessel|"
                r"craft|yacht|ferry|barge|tug|submarine|hovercraft|aircraft|airplane|"
                r"aeroplane|plane|jet|propeller|helicopter|chopper|copter|glider|balloon|"
                r"blimp|zeppelin|airship|rocket|missile|spaceship|spacecraft|shuttle|"
                r"capsule|module|station|base|colony|settlement|outpost|camp|bivouac|"
                r"shelter|refuge|haven|sanctuary|retreat|resort|hotel|motel|inn|lodge|"
                r"hostel|guesthouse|pension|bed_and_breakfast|bnb|airbnb|vacation_rental|"
                r"timeshare|condo|condominium|apartment|flat|unit|suite|room|studio|loft|"
                r"penthouse|duplex|triplex|quadplex|townhouse|rowhouse|bungalow|cottage|"
                r"cabin|chalet|villa|mansion|estate|castle|palace|fort|fortress|citadel|"
                r"stronghold|keep|tower|spire|dome|arch|vault|ceiling|roof|awning|canopy|"
                r"shade|shelter|cover|tent|teepee|yurt|igloo|hut|shack|shed|barn|stable|"
                r"coop|kennel|hive|nest|den|burrow|lair|hole|cave|cavern|grotto|tunnel|"
                r"mine|pit|quarry|excavation|trench|ditch|channel|canal|aqueduct|conduit|"
                r"pipeline|tube|pipe|hose|line|cable|wire|fiber|rope|chain|link|bond|tie|"
                r"strap|belt|band|ribbon|lace|string|thread|yarn|fiber|filament|strand|"
                r"wire|cable|cord|rope|chain)\b"
            )
            if not re.search(doc_indicators, string_val, re.IGNORECASE):
                return "full_name"
            return "alphanum"

    # 3. Relational fields pointing to person models
    if field_type in ("Many2one", "One2many", "Many2many") and comodel_name:
        if _model_matches(comodel_name, PERSON_MODELS):
            return "clear"

    # 4. Text and HTML types without matches are typically notes/descriptions
    if field_type in ("Html", "Text"):
        return "clear"

    # 5. Fallback for explicit binary
    if field_type == "Binary":
        return "clear"

    return None


def _parse_models(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except Exception:
        return

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        is_model = False
        for base in node.bases:
            name = _resolve(base)
            if name and ("Model" in name or "model" in name):
                is_model = True
                break

        if not is_model:
            continue

        _name = None
        _inherit = []
        _description = ""
        fields = []

        for item in node.body:
            targets = []
            if isinstance(item, ast.Assign):
                targets = [t for t in item.targets if isinstance(t, ast.Name)]
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                targets = [item.target]

            for target in targets:
                if target.id == "_name":
                    _name = _get_str(item.value)
                elif target.id == "_description":
                    _description = _get_str(item.value) or ""
                elif target.id == "_inherit":
                    _inherit = _get_list(item.value)
                    if not _inherit:
                        v = _get_str(item.value)
                        if v:
                            _inherit = [v]
                elif target.id == "_inherits":
                    _inherit += _get_dict_keys(item.value)
                else:
                    val = item.value
                    if isinstance(val, ast.Call):
                        func = _resolve(val.func)
                        if func and func.startswith("fields."):
                            ftype = func.split(".", 1)[1]
                            kwargs = {}
                            string_val = None
                            help_val = None
                            for kw in val.keywords:
                                kn = kw.arg
                                kv = _get_str(kw.value)
                                if kv is not None:
                                    kwargs[kn] = kv
                                elif isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool):
                                    kwargs[kn] = str(kw.value.value)
                                elif hasattr(ast, "NameConstant") and isinstance(kw.value, ast.NameConstant):
                                    kwargs[kn] = str(kw.value.value)
                                elif isinstance(kw.value, ast.Name):
                                    kwargs[kn] = kw.value.id

                                if kn == "string":
                                    string_val = kv
                                if kn == "help":
                                    help_val = kv

                            comodel_name = None
                            if ftype in ("Many2one", "One2many", "Many2many"):
                                comodel_name = _get_comodel_name(val)

                            fields.append({
                                "name": target.id,
                                "type": ftype,
                                "string": string_val or "",
                                "help": help_val or "",
                                "kwargs": kwargs,
                                "comodel": comodel_name or "",
                            })

        yield {
            "class_name": node.name,
            "_name": _name,
            "_inherit": _inherit,
            "_description": _description,
            "fields": fields,
        }


def scan_addons(addons_paths):
    """Walk the given addons directories and return {model_name: [field_dict, ...]}."""
    all_models = defaultdict(list)

    for path_str in addons_paths:
        addons = Path(path_str)
        if not addons.exists() or not addons.is_dir():
            continue

        for module_dir in addons.iterdir():
            if not module_dir.is_dir():
                continue
            if module_dir.name.startswith("test_"):
                continue
            models_dir = module_dir / "models"
            if not models_dir.exists():
                continue
            for py_file in models_dir.rglob("*.py"):
                for model in _parse_models(py_file):
                    mname = model["_name"]
                    if not mname:
                        if model["_inherit"]:
                            mname = model["_inherit"][0]
                        else:
                            continue
                    for field in model["fields"]:
                        field["module"] = module_dir.name
                        all_models[mname].append(field)

    return all_models


def analyze_fields(addons_paths):
    """Scan addons and return [(model, field, field_type, generator), ...] for PII fields."""
    models = scan_addons(addons_paths)
    results = []

    for model_name, fields in models.items():
        if model_name.startswith("test."):
            continue

        # Skip system models
        if model_name.startswith("ir.") or model_name in {
            "res.country", "res.country.state", "res.lang", "res.currency", "res.groups",
            "res.users.role", "account.account", "account.journal", "account.tax",
            "account.fiscal.position", "product.category", "uom.uom", "uom.category",
            "fetchmail.server", "ir.mail_server",
        }:
            continue

        seen = set()
        for f in fields:
            field_name = f["name"]
            key = (f["module"], field_name)
            if key in seen:
                continue
            seen.add(key)

            generator = get_generator_for_field(
                field_name,
                f["type"],
                f["string"],
                f["help"],
                model_name,
                f.get("comodel", ""),
            )
            if generator:
                results.append((model_name, field_name, f["type"], generator))

    return results


def write_csv(rows, out):
    """Write PII rows to a CSV file-like object."""
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["model", "field", "field_type", "generator"])
    for row in rows:
        writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze-pii-fields",
        description="Scan Odoo addons and emit a CSV of PII fields with suggested generators.",
    )
    parser.add_argument(
        "--addons-paths",
        nargs="+",
        required=True,
        help="Paths to Odoo addons directories (e.g. odoo/addons, enterprise).",
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        help="Output CSV file (default: stdout).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    rows = analyze_fields(args.addons_paths)

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            write_csv(rows, f)
        print(f"Wrote {len(rows)} PII fields to {args.output}", file=sys.stderr)
    else:
        write_csv(rows, sys.stdout)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
