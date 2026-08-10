<!-- Created by Odoo — Technical Support tooling (scrubdb). -->

# scrubdb

One-way, version-independent PostgreSQL anonymizer. It turns a **copy** of a
customer database into a low-risk dataset that is safe to use with AI tooling,
without ever sending real customer data to an AI service. It is a standalone
Python tool — it talks to PostgreSQL directly and does not use Odoo — though
Odoo-specific knowledge ships as optional rules/exclusions.

## Quick start (standalone — e.g. from a zip)

Self-contained: it depends only on the Python standard library plus `psycopg2`
(and optionally `pyyaml`/`faker`). No other tooling required.

```bash
# 1. get the code (a checkout, or unzip the scrubdb/ folder somewhere)
cd path/to/scrubdb            # the folder containing __main__.py, cli.py, ...

# 2. install (either works)
pip install .                 # -> a real `scrubdb` command  (uses pyproject.toml)
pip install -r requirements.txt   # deps only; run with `python -m scrubdb`

# 3. run against a DISPOSABLE COPY (Python 3.9+)
scrubdb <database> --dry-run                       # preview, changes nothing
scrubdb <database> --apply --i-confirm-this-is-a-copy   # actually mask

# if you didn't `pip install .`, run from the folder that CONTAINS scrubdb/:
cd .. && python -m scrubdb <database> --dry-run
```

## Why it works the way it does

- **One-way.** A random key is generated in memory for each run and discarded
  when the run ends. No mapping between real and fake values is ever written,
  so the masking can't be reversed.
- **Deterministic within a run.** Equal inputs map to equal outputs (HMAC-SHA256
  keyed by that per-run secret, not Faker's global seed), so foreign keys and
  joins survive masking.
- **Version-independent.** Schema discovery uses `information_schema` and masking
  uses plain `UPDATE ... FROM (VALUES ...)` — both behave the same on every
  PostgreSQL version. No `pg_catalog` branching, no server extension.

## How it decides what to mask

**Anonymize everything, exclude what must stay.** No rules required: every
column is masked *except* those that must be preserved — you can't miss PII
you forgot to write a rule for.

Odoo **TransientModel** tables (`res.config.settings` and every settings/wizard,
detected via `ir_model.transient`) are **emptied** — their rows are ephemeral
throwaway data, so they're deleted rather than masked.

The rest of a table's columns are **kept** when they are:
- a **primary key**, or on **either side of a foreign key** — so identity and
  referential integrity survive;
- an Odoo **system/audit** column (`id`, `create_uid`, `write_uid`,
  `create_date`, `write_date`) — a leading custom `x_` prefix is stripped first,
  so `x_create_date` etc. are matched too (`x_studio_` is *not* stripped —
  Studio fields are arbitrary, so they mask normally);
- part of an Odoo **`ir_*` technical/registry table** — external IDs, model &
  field names, view XML, action/cron code, record-rule domains, config keys,
  sequences, module state. Masking these corrupts the ORM and breaks the
  database, so every `ir_*` table is kept by default (`[technical]`), **except**
  its known secret/PII columns which are still scrubbed: `ir_mail_server`
  credentials, `ir_logging` payloads, `ir_act_server.webhook_url`, and
  `ir_attachment` name/description/url/access_token (+ filestore columns).
  `--include-technical` disables this (dangerous — will break the DB);
- an Odoo **Selection field** — read authoritatively from
  `ir_model_fields.ttype = 'selection'`, so both **core** (values defined in
  Python) and **custom/Studio** selection fields are kept; masking one to an
  arbitrary string breaks the ORM (`Wrong value for …`). This is stronger than
  the sampled enum guard, which misses selection fields on small tables;
- under a real **CHECK constraint** (enum/format rules the app relies on; NOT
  NULL checks are ignored);
- matched by an **exclude pattern** — plus built-in integrity-protective
  defaults (`state`, `*_state`, `active`, `lang`, `*_type`, `*_code`,
  `currency`, `model`, `res_id`, …) that keep Odoo selection/config/reference
  fields valid; patterns match the `x_`-stripped name too, so `x_active`,
  `x_state`, `x_res_id` are covered (but not arbitrary `x_studio_` fields);
- a text column holding an Odoo **Reference** (`fields.Reference` stores
  `"model.name,id"` in one column) — detected by sampling, since there's no
  schema signal; masking would break the record link;
- a text column that **looks like a selection field** when sampled — few, short,
  repeated values (the **enum guard**; both content guards disable with
  `--no-content-scan`);
- of a **type with no safe blanket transform** (boolean, uuid, xml, bytea, DB
  enums, arrays).

Everything else is masked by type: text/jsonb → `mask_text` (shape-preserving),
dates/timestamps → `date_shift` (one run-global offset, so ordering and gaps
survive), numerics → `noise_number`. **Large text** (sampled average length ≥
`--big-text-threshold`, default 200 — HTML bodies, long notes) is instead
redacted **server-side** with `md5(value || run_secret)`: no data round-trip, so
an 8-minute HTML column becomes seconds (the value becomes a hash, not a
shape-preserving scramble). `--big-text-threshold 0` disables it. Columns under a UNIQUE/PK constraint get a
deterministic suffix so masked values stay distinct. An optional `--ruleset`
**overrides** the chosen transform for specific columns.

**Person names.** `res_partner.name` (which `res.users` inherits by delegation,
so it covers users too) and `hr_employee.name` are masked with `mask_name`: the
**first word is kept, the rest masked** (`John Smith` → `John Qguek`), and a
single-word value is kept as-is — so a masked record is still loosely
recognisable by first name.

**Filestore drop (default).** For a DB-only workflow (the filestore directory is
not copied), the `ir_attachment` filestore reference and orphaned payload are
**nulled** by default — `store_fname` (dangling file path), `db_datas` (the
in-DB blob, which blanket masking would otherwise *keep* as bytea → a content
leak), and `index_content` (extracted text). Pass `--keep-filestore` to apply
normal masking instead. A `--ruleset` override for any of these columns wins.

## Layout

A single-level Python package:

```
scrubdb/
  __init__.py           package marker + __version__
  __main__.py           entry point for `python -m scrubdb`
  transforms.py         Deterministic keyed value transforms (no DB, no Odoo)
  introspect.py         information_schema discovery (version-independent)
  classify.py           Content-sampling helpers for the Reference/enum guards
  policy.py             schema + content scan -> concrete blanket Plan
  executor.py           Applies the Plan via batched standard UPDATEs, one transaction
  finalize.py           Post-mask dev/AI-copy steps: logins, passwords, crons, user list
  cli.py                Pipeline orchestration; guards --apply behind a copy flag
  rules/
    odoo_common.yml     Odoo PII map, version-tolerant
  tests/
    test_engine.py      Runs without a live database (mock cursor)
```

## Dependencies

All optional and only needed for a real run:

- `psycopg2` (or `psycopg2-binary`) — to connect to a database.
- `pyyaml` — to load YAML rulesets (JSON rulesets work without it).
- `faker` — optional; without it, built-in fallback generators are used.

The test suite runs with zero third-party dependencies.

## Connecting

scrubdb anonymizes the database you name, **in place** — it never creates,
copies, or restores a database for you, and it never connects to a replica.
Making the disposable copy is your step; scrubdb operates on whatever you point
it at (which is why `--apply` is gated behind `--i-confirm-this-is-a-copy`).

The target database is a **required positional argument**. Unlike Odoo's
`-d/--database`, scrubdb never guesses the database from the environment or a
libpq default — because the operation rewrites it in place, you always name the
exact database:

```bash
scrubdb <database> [options...]
```

**Primary/recommended form:** if you have `~/.pgpass` (or peer authentication)
set up — which most setups do — you don't pass any connection options at all;
just the database name. libpq supplies host/port/user/password:

```bash
scrubdb <database> --apply --i-confirm-this-is-a-copy
```

The remaining connection options mirror the relevant subset of Odoo's
*Database related options* — same flag names and `PG*` env fallbacks — and are
all optional; anything omitted is resolved by libpq (peer auth / `~/.pgpass`):

| scrubdb / Odoo flag | libpq env | meaning |
|---|---|---|
| `<database>` (positional) | — | **required** exact database to anonymize |
| `-r`, `--db_user` | `PGUSER` | database user |
| `-w`, `--db_password` | `PGPASSWORD` | database password |
| `--db_host` | `PGHOST` | host |
| `--db_port` | `PGPORT` | port |
| `--db_sslmode` | `PGSSLMODE` | SSL mode |
| `--db_app_name` | `PGAPPNAME` | `application_name` (`{pid}` substituted) |

Odoo's `-d/--database` (made positional here), `--dsn`, `--pg_path`,
`--db_replica_host/_port`, `--db_maxconn`, and `--db-template` are intentionally
absent: the database is named explicitly, and scrubdb spawns no `pg_*` binaries,
must write to the primary (never a replica), uses a single connection, and never
creates a database.

For a local instance you often need nothing beyond the database name — libpq
peer auth / `~/.pgpass` supplies the rest:

```bash
scrubdb copy_db --dry-run          # local, dry-run
```

## Running it

Always against a **disposable copy**, never a live database.

```bash
pip install psycopg2-binary pyyaml             # faker optional

# you make the copy; scrubdb only anonymizes it
createdb copy_db && pg_restore -d copy_db customer_backup.dump

# PRIMARY form — with ~/.pgpass (or peer auth) set up, just name the database.
# dry-run: plan + coverage, change nothing (this is the default)
scrubdb copy_db --dry-run --report plan.json

# apply
scrubdb copy_db --apply --i-confirm-this-is-a-copy --report report.json

# only if you can't rely on ~/.pgpass/peer: pass connection options explicitly
scrubdb copy_db --db_host localhost --db_port 5432 --db_user odoo \
  --dry-run --report plan.json

# keep extra columns unchanged (on top of the automatic PK/FK/system keeps)
scrubdb copy_db \
  --exclude res_users.login --exclude '*_reference' \
  --exclude-file keep.txt \
  --dry-run --report plan.json

pg_dump copy_db > anonymized.sql && dropdb copy_db
```

`--apply` is refused unless `--i-confirm-this-is-a-copy` is also passed, so the
tool can't be pointed at production by accident.

## Making it a usable dev/AI copy (post-mask finalization)

Odoo-specific steps that run **after** masking, **on by default**, in the same
transaction (so they preview under `--dry-run` and the printed logins are the
masked ones — no real PII is printed). The **root/superuser `res_users` id=1 is
never modified** (nor by masking) since it's used for sudo.

```bash
scrubdb copy_db --apply --i-confirm-this-is-a-copy
```

- **`--normalize-logins`** (default) — replaces every non-root login (email or
  username, both PII) with **`user<id>`** (e.g. id 47 → `user47`). Anonymized,
  unique, and still identifiable: the id is never masked, so support can log in
  as any user by its id. Disable with `--no-normalize-logins`.
- **`--deactivate-crons`** (default) — `ir_cron.active = false`, so the copy
  fires no jobs/emails. Disable with `--no-deactivate-crons`.
- **`--reset-passwords [PW]`** (default `admin`) — sets every non-root password.
  Odoo accepts the plaintext value and re-hashes on first login. Disable with
  `--no-reset-passwords`. Tip: log in as `user2`/`admin` and use Odoo's
  "Log in as" to impersonate any user — no per-user password needed.
- **`--list-users [N]`** (default 100) — prints `id / name / login / password`
  (join to `res_partner` for the name) so you know how to connect. Disable with
  `--no-list-users`.

All are no-ops on a non-Odoo database.

**Progress output.** Milestones (connect, schema discovery, plan, per-column
`[i/n] table.column → transform`, commit) are logged to stderr at INFO by
default — so a long run isn't a blank screen. `-v/--verbose` adds per-column row
counts and enum-sampling detail; `-q/--quiet` limits output to warnings/errors.
Log lines never contain data values or the password.

**Exclusions:** `--exclude PATTERN` (repeatable) and `--exclude-file` (one glob
per line, `#` comments allowed) both feed the same pattern list — combine them
freely, order doesn't matter. A glob matches the bare column name,
`table.column`, or the `x_`-stripped name (`x_active` matches `active`); a hit
keeps the column unchanged and skips it, ahead of any type/content-based
classification. The built-in integrity-protective defaults are added unless you
pass `--no-default-excludes`.

You can also run it **without installing** — from the directory that *contains*
the `scrubdb/` folder:

```bash
python -m scrubdb --help
```

**Extras & notes.** `pip install .` pulls in `psycopg2-binary`; add optional
extras with `pip install ".[yaml,faker]"` (`pyyaml` for YAML rulesets, `faker`
for nicer fake values — a built-in fallback is used without it). The Odoo
ruleset ships inside the package at `scrubdb/rules/odoo_common.yml`. Requires
Python 3.9+. No ruleset is required — schema introspection and heuristics
give full coverage on their own; `--ruleset` only overrides specific columns.

## Tests

From inside the `scrubdb/` package directory:

```bash
python3 -m pytest tests/            # if pytest is available
# or the zero-dependency fallback runner:
python3 - <<'PY'
import importlib, sys, traceback
sys.path.insert(0, ".")
m = importlib.import_module("tests.test_engine")
p = f = 0
for n in [x for x in dir(m) if x.startswith("test_")]:
    try:
        getattr(m, n)(); p += 1
    except Exception:
        f += 1; print("FAIL", n); traceback.print_exc()
print(f"{p} passed, {f} failed")
PY
```

## Extending it

- New transform: implement `fn(value, ctx)`, register it in
  `transforms.REGISTRY` (or `FACTORIES` for a parametrized one), add a test.
- Type routing / default excludes: edit `_blanket_transform` and
  `DEFAULT_EXCLUDE_PATTERNS` in `policy.py`, add a test.
- New obvious-PII column name: add it to `RISKY_PATTERNS` in `policy.py`.

Keep the engine (`transforms`, `classify`, `policy`) DB-free and unit-testable;
DB access belongs in `executor` and the samplers.

## Limitations

- Masking reduces but does not eliminate re-identification from indirect
  identifiers — treat the output as low-risk, not zero-risk.
- **Masking is aggressive.** Odoo selection/state fields are plain `varchar`
  with no hard schema signal. They are caught three ways — CHECK-constraint
  exclusion, the built-in/user exclude patterns, and the sampled enum guard —
  but none is exhaustive. Review the dry-run plan's masked list and extend
  `--exclude` before `--apply`. Format/CHECK constraints on genuinely free-text
  columns can still be violated.
- Uniqueness: only **single-column unique text** columns are masked (with a
  deterministic suffix). Columns in a **composite** unique constraint, and
  non-text single-column unique columns, are **kept** — masking a participant
  can violate the constraint. A unique text column inside a composite constraint
  is therefore kept too (rare; override via `--ruleset` if it's PII).
- `res.company` is masked for PII (name, email, address, VAT), but also holds
  some **configuration** text/number fields (e.g. code prefixes). FK and boolean
  config on it is already kept; selection config is caught by the enum guard;
  but a config *char* value with no signal can still be masked — review the
  plan and `--exclude` such columns (or override) if your DB has them.
- `ir_config_parameter.value` is **kept** (technical), but can hold secrets
  (API keys, `database.secret`). Scrubbing it safely needs a per-key allowlist
  (row-level), which is not yet implemented — scrub specific keys manually if
  present, or override via `--ruleset`.
- Unique indexes created outside a constraint are not seen (constraint-based
  discovery only, to stay off `pg_catalog`).
- Not yet validated against a real database — that must be run on the host
  against a copy (never inside a sandbox).
