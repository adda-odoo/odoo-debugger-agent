# Per-database scrub rules (fallback)

**The normal way to set a database's scrub rules is interactively:** when
`start.sh <db_name>` copies a database, it opens `$EDITOR` with that database's
allowlist and rules (seeded from the shared defaults, or from the last run) and
stores what you save in the `scrubdb_registry` database. See the main README's
**Per-database scrub config** section. The files in this directory are the older
mechanism, now a **fallback for when `start.sh` isn't driving the scrub** — e.g.
calling `copy_and_scrub_db.sh` directly without the registry env vars set.

In that fallback path, `copy_and_scrub_db.sh` applies the shared ruleset
(`../../scrubdb-poc/tools/scrubdb/rules/odoo_common.yml`) via scrubdb's
`--ruleset`. If a file named `<db_name>.yml` exists in this directory, it is
merged on top of that shared ruleset (per table/column — a column listed here
replaces the shared rule for that column; anything else in the table is kept)
before scrubbing `<db_name>`.

Format matches `odoo_common.yml`:

```yaml
rules:
  some_custom_table:
    some_column: scrub_text
  account_move:
    name: scrub_text
```

Delete the database's already-scrubbed copy on `ai-dev` (`dropdb`) before
retrying `start.sh <db_name>` after adding or editing a rules file here —
`copy_and_scrub_db.sh` only scrubs a database once, on first copy.
