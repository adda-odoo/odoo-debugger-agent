# Odoo Smart Synthetic Obfuscator

This is the next-generation Odoo database obfuscation tool, designed for high performance, ease of use, and LLM-compatibility.

It merges the best features of two legacy tools:
- **AST Static Analysis:** Automatically discovers sensitive PII fields by parsing Odoo Python source code (from the old `us-support` wrapper).
- **Synthetic Data Generation (Faker):** Replaces sensitive data with realistic, fake data (e.g., `John Doe`, `user@anon.example.com`) instead of symmetric encryption gibberish.
- **High-Speed Execution:** Uses PostgreSQL `COPY` commands into `UNLOGGED` tables, bypassing the ORM for blazing-fast obfuscation of multi-gigabyte databases.
- **Filestore Scrubbing:** Systematically replaces sensitive PDF and image attachments with blank dummies while preserving UI/theme assets.

## Installation

```bash
cd odoo_obfuscate
pip install -r requirements.txt
```

## Usage

```bash
./main.py --db <database_name> --addons-paths /path/to/odoo/addons /path/to/enterprise
```

### Options:
- `--db DBNAME`: The target PostgreSQL database (using peer authentication).
- `--addons-paths PATHS`: One or more paths to Odoo addons directories to scan for PII.
- `--dry-run`: Scans the database and counts rows, but makes no changes.
- `--filestore PATH`: (Optional) Explicit path to the Odoo filestore. If omitted, the tool auto-detects it.
- `--force`: Bypass the idempotency guard (allows re-running on an already obfuscated DB).

## Why not `pgcrypto`?
Legacy obfuscation tools used symmetric encryption (`pgp_sym_encrypt`). While reversible, this created massive problems:
1. **Token Explosion:** Encrypted strings cost 10x more tokens when passed to LLMs.
2. **Semantic Destruction:** LLMs cannot understand or reason about encrypted gibberish.
3. **Data Leakage:** Legacy tools often ignored the filestore and unstructured chatter (`mail.message.body`), enabling re-identification attacks.

This tool guarantees that your staging and dev environments are completely sanitized, look realistic, and are perfectly safe to share with external developers and AI agents.
