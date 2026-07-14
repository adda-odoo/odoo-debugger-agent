# Odoo Smart Synthetic Obfuscator

This is an Odoo database obfuscation tool designed for high performance, ease of use, and LLM-compatibility. 

Key Features:
- **Curated PII Field Lists:** Uses version-specific CSV files (`pii_fields/sensitive_fields_<version>.csv`) to identify sensitive fields.
- **Standalone Analyzer:** `tools/analyze_pii_fields.py` can scan Odoo addon source code and emit a CSV of candidate PII fields for curation.
- **Synthetic Data Generation (Faker):** Replaces sensitive data with realistic, fake data (e.g., `John Doe`, `user@anon.example.com`).
- **High-Speed Execution:** Uses PostgreSQL `COPY` commands into `UNLOGGED` tables, bypassing the ORM for blazing-fast obfuscation of multi-gigabyte databases.
- **Filestore Scrubbing:** Systematically replaces sensitive PDF and image attachments with blank dummies while preserving UI/theme assets.

## Installation

```bash
cd odoo_obfuscate
pip install -r requirements.txt
```

## Usage

```bash
./main.py --db <database_name>
```

### Options:
- `--db DBNAME`: The target PostgreSQL database (using peer authentication).
- `--dry-run`: Scans the database and counts rows, but makes no changes.
- `--filestore PATH`: (Optional) Explicit path to the Odoo filestore. If omitted, the tool auto-detects it.
- `--force`: Bypass the idempotency guard (allows re-running on an already obfuscated DB).

## Why this tool?
This tool guarantees that your staging and dev environments are completely sanitized, look realistic, and are perfectly safe to share with external developers and AI agents.

By generating synthetic data rather than relying on encryption or other obfuscation methods, this tool ensures:
1. **Token Efficiency:** Fake realistic strings cost significantly fewer tokens when passed to LLMs compared to encrypted text.
2. **Semantic Understanding:** LLMs can understand and reason about realistic fake data, allowing for meaningful interactions and testing.
3. **Comprehensive Data Safety:** This tool comprehensively scrubs both the database and the filestore (including unstructured chatter like `mail.message.body`), preventing re-identification attacks.
