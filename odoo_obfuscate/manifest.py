"""Run manifest — summary printed to stdout after every anonymization run."""
from __future__ import annotations

import datetime

_ATTESTATION = (
    "ATTESTATION: This database {verb} on {ts} by anon-tool. "
    "No personal data was transmitted to external services."
)


class RunManifest:
    """Collects per-run metadata; prints a structured summary to stdout."""

    def __init__(
        self,
        dbname: str,
        pg_ver: str,
        odoo_ver: str | None,
        dry_run: bool = False,
    ) -> None:
        self.dbname = dbname
        self.pg_ver = pg_ver
        self.odoo_ver = odoo_ver or "unknown"
        self.dry_run = dry_run
        self.started_at: datetime.datetime = datetime.datetime.now()
        self.completed_at: datetime.datetime | None = None

        self.tables: dict[str, int] = {}      # table → row count
        self.absent_columns: list[str] = []   # "table.col"
        self.absent_tables: list[str] = []    # table names
        self.undecided_review: list[str] = [] # review.yaml keys with action: null
        self.low_confidence: list[str] = []   # review.yaml keys with confidence: low
        self.exceptions_applied: list[str] = []

    # ── recording helpers ────────────────────────────────────────────────────

    def record_table(self, table: str, row_count: int) -> None:
        self.tables[table] = row_count

    def record_absent_column(self, table: str, col: str) -> None:
        self.absent_columns.append(f"{table}.{col}")

    def record_absent_table(self, table: str) -> None:
        self.absent_tables.append(table)

    def record_undecided(self, key: str) -> None:
        self.undecided_review.append(key)

    def record_low_confidence(self, key: str) -> None:
        self.low_confidence.append(key)

    def record_exception(self, key: str, action: str) -> None:
        self.exceptions_applied.append(f"{key} → {action}")

    def complete(self) -> None:
        self.completed_at = datetime.datetime.now()

    # ── output ───────────────────────────────────────────────────────────────

    def print_summary(self) -> None:
        ts = (self.completed_at or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        mode = " [DRY RUN — no changes written]" if self.dry_run else ""
        sep = "=" * 56

        out: list[str] = [
            "",
            sep,
            f" anon-tool{mode}",
            sep,
            f"  Database   : {self.dbname}",
            f"  Odoo       : {self.odoo_ver}",
            f"  PostgreSQL : {self.pg_ver}",
            f"  Completed  : {ts}",
            "",
        ]

        touched = {t: n for t, n in self.tables.items() if n > 0}
        total_rows = sum(touched.values())
        out += [
            f"  Tables processed : {len(touched)}",
            f"  Total rows       : {total_rows}",
        ]

        if touched:
            out.append("")
            out.append("  Per table:")
            for table, count in self.tables.items():
                if count > 0:
                    out.append(f"    {table:<42} {count:>6} rows")

        if self.absent_columns:
            out.append("")
            out.append(f"  Absent columns skipped ({len(self.absent_columns)}):")
            for col in self.absent_columns:
                out.append(f"    {col}")

        if self.exceptions_applied:
            out.append("")
            out.append(f"  exceptions.yaml overrides applied ({len(self.exceptions_applied)}):")
            for entry in self.exceptions_applied:
                out.append(f"    {entry}")

        if self.low_confidence:
            out.append("")
            out.append(f"  Low-confidence LLM decisions — spot-check recommended ({len(self.low_confidence)}):")
            for key in self.low_confidence[:20]:
                out.append(f"    {key}")
            if len(self.low_confidence) > 20:
                out.append(f"    ... and {len(self.low_confidence) - 20} more")

        if self.undecided_review:
            out.append("")
            n = len(self.undecided_review)
            out.append(f"  WARNING: {n} review.yaml entr{'y' if n == 1 else 'ies'} have no decision (treated as preserve).")
            out.append("  Run: python anonymize.py prepare --llm-export  to get a prompt for claude.ai.")
            for key in self.undecided_review[:10]:
                out.append(f"    {key}")
            if n > 10:
                out.append(f"    ... and {n - 10} more")

        verb = "would be anonymized" if self.dry_run else "was anonymized"
        out += [
            "",
            _ATTESTATION.format(verb=verb, ts=ts),
            sep,
            "",
        ]

        print("\n".join(out))
