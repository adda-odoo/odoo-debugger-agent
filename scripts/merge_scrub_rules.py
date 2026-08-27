#!/usr/bin/env python3
"""Merge the shared scrubdb ruleset with an optional per-database override file.

Usage: merge_scrub_rules.py <base.yml> <override.yml> <output.yml>

Merging happens at the (table, column) level: an overridden column replaces
the base's transform for that column, a table only present in the override
is added as-is, and every other column in a table touched by the override
keeps its base rule.
"""
import sys

import yaml

base_path, override_path, out_path = sys.argv[1:4]

with open(base_path, encoding="utf-8") as fh:
    base = yaml.safe_load(fh) or {}
with open(override_path, encoding="utf-8") as fh:
    override = yaml.safe_load(fh) or {}

merged = {table: dict(cols or {}) for table, cols in (base.get("rules") or {}).items()}
for table, cols in (override.get("rules") or {}).items():
    merged.setdefault(table, {}).update(cols or {})

with open(out_path, "w", encoding="utf-8") as fh:
    yaml.safe_dump({"rules": merged}, fh, sort_keys=False)
