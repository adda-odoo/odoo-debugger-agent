import ast
import re
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

def get_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if hasattr(ast, 'Str') and isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l, r = get_str(node.left), get_str(node.right)
        if l is not None and r is not None:
            return l + r
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            s = get_str(v)
            if s is None:
                return None
            parts.append(s)
        return "".join(parts)
    return None

def get_list(node):
    vals = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for e in node.elts:
            s = get_str(e)
            if s is not None:
                vals.append(s)
    return vals

def get_dict_keys(node):
    keys = []
    if isinstance(node, ast.Dict):
        for k in node.keys:
            s = get_str(k)
            if s is not None:
                keys.append(s)
    return keys

def resolve(node):
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

def get_comodel_name(val):
    if val.args:
        comodel = get_str(val.args[0])
        if comodel:
            return comodel
    for kw in val.keywords:
        if kw.arg == 'comodel_name':
            return get_str(kw.value)
    return None

def model_matches(model_name, patterns):
    for p in patterns:
        if re.search(p, model_name):
            return True
    return False

def get_generator_for_field(field_name, field_type, string_val, help_val, kwargs, model_name, comodel_name=""):
    # 1. Explicit field name patterns
    for pattern, generator_type, req_text in FIELD_PATTERNS:
        if re.search(pattern, field_name, re.IGNORECASE):
            if req_text and field_type not in TEXT_TYPES:
                continue
            return generator_type

    # 2. Special handling for plain "name" field
    if field_name == "name" or field_name == "_name":
        if model_matches(model_name, PERSON_MODELS):
            return "full_name"
        elif model_matches(model_name, DOCUMENT_MODELS):
            return "alphanum"
        elif string_val and re.search(r"\b(name|nom|nombre|naam)\b", string_val, re.IGNORECASE):
            doc_indicators = r"\b(reference|number|code|label|stage|status|state|type|category|tag|sequence|order|priority|level|rank|score|rate|ratio|percentage|count|quantity|amount|total|sum|balance|period|date|time|year|month|day|hour|minute|second|duration|interval|range|zone|region|area|sector|industry|market|segment|channel|medium|source|campaign|program|project|task|activity|event|session|course|class|lesson|module|chapter|section|paragraph|sentence|word|term|phrase|expression|statement|declaration|definition|description|explanation|instruction|direction|guideline|policy|procedure|process|workflow|pipeline|route|path|way|method|mode|manner|style|format|layout|design|pattern|template|model|schema|structure|framework|architecture|system|platform|application|software|hardware|firmware|middleware|network|database|dataset|datastore|repository|warehouse|lake|pool|bucket|container|volume|drive|disk|tape|card|stick|key|lock|gate|door|window|wall|floor|ceiling|roof|foundation|base|ground|soil|earth|land|terrain|surface|face|side|edge|corner|point|spot|place|site|location|position|coordinate|address|destination|origin|source|start|beginning|end|finish|close|opening|entrance|exit|entry|access|approach|pathway|walkway|roadway|highway|freeway|expressway|turnpike|tollway|parkway|causeway|bridge|tunnel|overpass|underpass|railway|subway|metro|tram|monorail|maglev|bus|coach|shuttle|van|truck|lorry|rig|tractor|trailer|caravan|camper|motorhome|rv|boat|ship|vessel|craft|yacht|ferry|barge|tug|submarine|hovercraft|aircraft|airplane|aeroplane|plane|jet|propeller|helicopter|chopper|copter|glider|balloon|blimp|zeppelin|airship|rocket|missile|spaceship|spacecraft|shuttle|capsule|module|station|base|colony|settlement|outpost|camp|bivouac|shelter|refuge|haven|sanctuary|retreat|resort|hotel|motel|inn|lodge|hostel|guesthouse|pension|bed_and_breakfast|bnb|airbnb|vacation_rental|timeshare|condo|condominium|apartment|flat|unit|suite|room|studio|loft|penthouse|duplex|triplex|quadplex|townhouse)$"
            if not re.search(doc_indicators, string_val, re.IGNORECASE):
                return "full_name"
            else:
                return "alphanum"

    # 3. Relational fields pointing to person models (e.g. employee_id -> hr.employee)
    if field_type in ('Many2one', 'One2many', 'Many2many') and comodel_name:
        if model_matches(comodel_name, PERSON_MODELS):
            return "clear" # Clear relations

    # 4. Text and HTML types without matches are typically notes/descriptions which should be cleared
    if field_type in ("Html", "Text"):
        return "clear"
    
    # 5. Fallback for explicit binary
    if field_type == "Binary":
        return "clear"

    return None

def parse_models(filepath):
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
            n = resolve(base)
            if n and ("Model" in n or "model" in n):
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
                    _name = get_str(item.value)
                elif target.id == "_description":
                    _description = get_str(item.value) or ""
                elif target.id == "_inherit":
                    _inherit = get_list(item.value)
                    if not _inherit:
                        v = get_str(item.value)
                        if v:
                            _inherit = [v]
                elif target.id == "_inherits":
                    _inherit += get_dict_keys(item.value)
                else:
                    val = item.value
                    if isinstance(val, ast.Call):
                        func = resolve(val.func)
                        if func and func.startswith("fields."):
                            ftype = func.split(".", 1)[1]
                            kwargs = {}
                            string_val = None
                            help_val = None
                            for kw in val.keywords:
                                kn = kw.arg
                                kv = get_str(kw.value)
                                if kv is not None:
                                    kwargs[kn] = kv
                                elif isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool):
                                    kwargs[kn] = str(kw.value.value)
                                elif hasattr(ast, 'NameConstant') and isinstance(kw.value, ast.NameConstant):
                                    kwargs[kn] = str(kw.value.value)
                                elif isinstance(kw.value, ast.Name):
                                    kwargs[kn] = kw.value.id

                                if kn == "string":
                                    string_val = kv
                                if kn == "help":
                                    help_val = kv

                            comodel_name = None
                            if ftype in ('Many2one', 'One2many', 'Many2many'):
                                comodel_name = get_comodel_name(val)

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
                for model in parse_models(py_file):
                    mname = model["_name"]
                    if not mname:
                        if model["_inherit"]:
                            mname = model["_inherit"][0]
                        else:
                            continue
                    for f in model["fields"]:
                        f["module"] = module_dir.name
                        all_models[mname].append(f)

    return all_models

def analyze_fields(addons_paths):
    """
    Scans the given paths for Odoo models and returns a dictionary:
    { "table_name.column_name": {"type": "generator_type"} }
    """
    models = scan_addons(addons_paths)
    results = {}
    
    for model_name, fields in models.items():
        if model_name.startswith("test."):
            continue
        
        table_name = model_name.replace(".", "_")
        seen = set()
        for f in fields:
            field_name = f["name"]
            key = (f["module"], field_name)
            if key in seen:
                continue
            seen.add(key)
            
            # Skip system models
            if model_name.startswith("ir.") or model_name in {
                "res.country", "res.country.state", "res.lang", "res.currency", "res.groups",
                "res.users.role", "account.account", "account.journal", "account.tax", "account.fiscal.position",
                "product.category", "uom.uom", "uom.category", "fetchmail.server", "ir.mail_server"
            }:
                continue

            generator = get_generator_for_field(
                field_name, f["type"], f["string"], f["help"], f["kwargs"], model_name, f.get("comodel", "")
            )
            
            if generator:
                results[f"{table_name}.{field_name}"] = {"type": generator}
                
                # Add unique_suffix requirement for standard unique fields
                if generator in {"vat", "barcode", "passport"} or field_name in {"vat", "barcode", "passport_id", "identification_id", "company_registry", "global_location_number"}:
                    results[f"{table_name}.{field_name}"]["unique_suffix"] = True
                    
    # Hardcoded overrides to guarantee consistency based on anon_tool's B1 logic
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
    
    for key, val in overrides.items():
        results[key] = val
        
    return results
