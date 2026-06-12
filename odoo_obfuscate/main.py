#!/usr/bin/env python3
"""Odoo Smart Synthetic Obfuscator."""
import argparse
import logging


logging.basicConfig(level=logging.INFO, format="%(message)s")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odoo-obfuscate",
        description="Smart Synthetic Obfuscator for Odoo PostgreSQL databases.",
    )

    db_group = parser.add_mutually_exclusive_group(required=True)
    db_group.add_argument("--db", metavar="DBNAME", help="Local database name (peer auth)")
    db_group.add_argument("--db-url", metavar="URL", help="Full PostgreSQL connection URL")

    parser.add_argument(
        "--addons-paths", nargs="+", required=True,
        help="Paths to Odoo addons directories (e.g. odoo/addons, enterprise, my_addons) to analyze for PII fields."
    )
    
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    parser.add_argument("--force", action="store_true", help="Bypass idempotency guard.")
    parser.add_argument("--filestore", metavar="PATH", help="Path to Odoo filestore directory.")
    parser.add_argument("--batch-size", type=int, default=10_000, metavar="N",
                     help="Rows per batch in chunked mode (default: 10000).")
    parser.add_argument("--locale", default="en_US", metavar="LOCALE",
                     help="Faker locale for generated names (default: en_US).")
    # Adding default_preserve just so args.default_preserve exists, since it's referenced in runner.py
    parser.add_argument("--default-preserve", action="store_true", default=True, help=argparse.SUPPRESS)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    import runner
    runner.run_anonymize(args)


if __name__ == "__main__":
    main()
