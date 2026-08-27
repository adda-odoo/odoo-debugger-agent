# Per-database scrub rules

`copy_and_scrub_db.sh` always applies the shared ruleset
(`../../scrubdb-poc/tools/scrubdb/rules/odoo_common.yml`) via scrubdb's
`--ruleset`. If a file named `<db_name>.yml` exists in this directory, it is
merged on top of that shared ruleset (per table/column — a column listed here
replaces the shared rule for that column; anything else in the table is kept)
before scrubbing `<db_name>`, so a database with custom fields, modules, or
unique constraints the shared ruleset doesn't anticipate can get its own
targeted overrides without touching the shared file.

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
