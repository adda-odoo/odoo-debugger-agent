# Created by Odoo — Technical Support tooling (scrubdb).
"""Command-line orchestration for scrubdb.

Pipeline: connect to a COPY of the database -> discover schema/constraints ->
build the blanket plan -> dry-run or apply. ``--apply`` is refused unless
``--i-confirm-this-is-a-copy`` is passed, so the tool can never be pointed at
a production database by accident.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import introspect
from .policy import build_blanket_plan
from .executor import apply_plan, connect

_logger = logging.getLogger("scrubdb")


def _configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(
        level=level, stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    logging.getLogger("scrubdb").setLevel(level)


def _pk_column_set(cur, schema: str) -> set:
    """Flatten ``fetch_primary_keys`` into a set of ``(table, column)``."""
    pks = introspect.fetch_primary_keys(cur, schema)
    return {(table, col) for table, cols in pks.items() for col in cols}


def _load_excludes(patterns, path) -> list:
    """Combine ``--exclude`` globs with the lines of ``--exclude-file``."""
    out = list(patterns or [])
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    return out


def load_ruleset(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "pyyaml is required to load a YAML ruleset; "
                "install pyyaml or supply a JSON ruleset"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text) or {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrubdb",
        description="One-way, version-independent PostgreSQL anonymizer "
                    "(run against a COPY only).",
    )
    # The target database is a REQUIRED positional — unlike Odoo's -d/--database,
    # scrubdb never guesses the database from env or libpq defaults. You name the
    # exact database to anonymize, because the operation rewrites it in place.
    p.add_argument("database", help="exact name of the database to anonymize "
                                    "in place (must be a disposable copy)")

    # The remaining connection options mirror the relevant subset of Odoo's
    # "Database related options" (same flag names / PG* env fallbacks). They are
    # optional: anything omitted is resolved by libpq (peer auth, ~/.pgpass).
    # Replica, pooling, pg_path and db-template options are intentionally absent.
    db = p.add_argument_group("connection options (Odoo-compatible; all optional)")
    db.add_argument("-r", "--db_user", dest="db_user",
                    default=os.environ.get("PGUSER") or None,
                    help="database user name [env: PGUSER]")
    db.add_argument("-w", "--db_password", dest="db_password",
                    default=os.environ.get("PGPASSWORD") or None,
                    help="database password [env: PGPASSWORD]")
    db.add_argument("--db_host", dest="db_host",
                    default=os.environ.get("PGHOST") or None,
                    help="database host [env: PGHOST]")
    db.add_argument("--db_port", dest="db_port", type=int,
                    default=os.environ.get("PGPORT") or None,
                    help="database port [env: PGPORT]")
    db.add_argument("--db_sslmode", dest="db_sslmode",
                    choices=("disable", "allow", "prefer", "require",
                             "verify-ca", "verify-full"),
                    default=os.environ.get("PGSSLMODE") or None,
                    help="database SSL connection mode [env: PGSSLMODE]")
    db.add_argument("--db_app_name", dest="db_app_name",
                    default=os.environ.get("PGAPPNAME") or "scrubdb-{pid}",
                    help="application_name reported to PostgreSQL; "
                         "{pid} is substituted [env: PGAPPNAME]")

    p.add_argument("--ruleset",
                   help="optional YAML/JSON ruleset that overrides the chosen "
                        "transform for specific columns (see rules/odoo_common.yml)")

    # Exclusions (columns to KEEP unchanged, on top of the automatic
    # PK/FK/system/type keeps).
    ex = p.add_argument_group("exclusions")
    ex.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="glob of column or table.column to keep (repeatable)")
    ex.add_argument("--exclude-file", help="file with one exclude glob per line")
    ex.add_argument("--no-default-excludes", action="store_true",
                    help="drop the built-in integrity-protective exclude patterns")
    ex.add_argument("--keep-filestore", action="store_true",
                    help="keep ir_attachment filestore refs/payload instead of "
                         "nulling them (store_fname, db_datas, index_content)")
    ex.add_argument("--include-technical", action="store_true",
                    help="DANGEROUS: also mask Odoo ir_* registry tables "
                         "(external IDs, views, config) — will break the database")

    p.add_argument("--schema", default="public")
    # --apply and --dry-run are mutually exclusive: passing both is an error
    # rather than silently picking one (default when neither given is dry-run).
    run_grp = p.add_mutually_exclusive_group()
    run_grp.add_argument("--apply", action="store_true",
                         help="write changes (needs the confirm flag)")
    run_grp.add_argument("--dry-run", action="store_true",
                         help="plan and roll back, changing nothing (default)")
    p.add_argument("--report", help="write a JSON report to this path")
    p.add_argument("--no-content-scan", action="store_true",
                   help="disable content classification (reintroduces the blind spot)")
    p.add_argument("--sample", type=int, default=300, help="rows sampled per column for scanning")
    p.add_argument("--big-text-threshold", type=int, default=200, metavar="N",
                   help="text columns whose sampled average length >= N are "
                        "redacted server-side (fast md5) instead of shape-masked; "
                        "0 disables (mask everything in Python)")
    # Post-mask finalization for an Odoo dev/AI copy — ON BY DEFAULT (all no-op
    # on non-Odoo DBs; res_users id=1 root/superuser is never touched).
    fin = p.add_argument_group("post-mask finalization (Odoo dev copy; on by default)")
    fin.add_argument("--reset-passwords", nargs="?", const="admin", default="admin",
                     metavar="PW",
                     help="set every non-root res_users password (default 'admin')")
    fin.add_argument("--no-reset-passwords", action="store_const", const=None,
                     dest="reset_passwords", help="leave passwords as masked")
    fin.add_argument("--deactivate-crons", action=argparse.BooleanOptionalAction,
                     default=True,
                     help="set ir_cron.active=false (default on; --no-deactivate-crons to keep)")
    fin.add_argument("--normalize-logins", action=argparse.BooleanOptionalAction,
                     default=True,
                     help="replace email-address logins with user<n> (default on)")
    fin.add_argument("--list-users", nargs="?", type=int, const=100, default=100,
                     metavar="N",
                     help="print the first N users (id/name/login/password) so you "
                          "know how to connect (default 100)")
    fin.add_argument("--no-list-users", action="store_const", const=0,
                     dest="list_users", help="do not print the user list")

    p.add_argument("--i-confirm-this-is-a-copy", action="store_true",
                   help="required for --apply; asserts the named database is a disposable copy")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log per-column progress to stderr (no data values)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="only warnings and errors")
    return p


def connection_arguments(args) -> dict:
    """Resolve CLI args into libpq ``connect_kwargs`` for :func:`executor.connect`.

    Pure and DB-free so it can be unit-tested. ``dbname`` always comes from the
    required positional; the remaining Odoo-style options are mapped to libpq
    keyword names, empty values are dropped (libpq env/.pgpass fallback covers
    them), and ``{pid}`` in the application name is substituted (and trimmed to
    NAMEDATALEN=63) exactly as Odoo does.
    """
    app_name = getattr(args, "db_app_name", None)
    if app_name:
        app_name = app_name.replace("{pid}", str(os.getpid()))[:63]
    kwargs = {
        "dbname": getattr(args, "database", None),
        "user": getattr(args, "db_user", None),
        "password": getattr(args, "db_password", None),
        "host": getattr(args, "db_host", None),
        "port": getattr(args, "db_port", None),
        "sslmode": getattr(args, "db_sslmode", None),
        "application_name": app_name,
    }
    return {k: v for k, v in kwargs.items() if v not in (None, "")}


def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose, args.quiet)

    if args.apply and not args.i_confirm_this_is_a_copy:
        print("refusing --apply without --i-confirm-this-is-a-copy", file=sys.stderr)
        return 2
    if not args.apply and not args.dry_run:
        args.dry_run = True  # default to safe dry-run

    ruleset = load_ruleset(args.ruleset) if args.ruleset else {}
    exclude_patterns = _load_excludes(args.exclude, args.exclude_file)
    # dbname is the exact target (required positional); everything else optional
    # and resolved by libpq (peer auth / ~/.pgpass) when omitted.
    _logger.info("connecting to database %r (schema %r)",
                 args.database or "(libpq default)", args.schema)
    conn, driver = connect(**connection_arguments(args))
    try:
        cur = conn.cursor()
        _logger.info("connected via %s; discovering schema…", driver)
        columns = introspect.fetch_columns(cur, args.schema)
        _logger.info("discovered %d base-table columns", len(columns))
        _logger.info("introspecting keys/constraints…")
        pk = _pk_column_set(cur, args.schema)
        fk = introspect.fetch_foreign_key_columns(cur, args.schema)
        uq_single, uq_composite = introspect.fetch_unique_columns(cur, args.schema)
        ck = introspect.fetch_check_constraint_columns(cur, args.schema)
        sel = introspect.fetch_selection_columns(cur, args.schema)
        transient = introspect.fetch_transient_tables(cur, args.schema)
        _logger.info("building blanket plan%s…",
                     "" if args.no_content_scan else " (sampling text columns)")
        plan = build_blanket_plan(
            columns,
            pk_columns=pk, fk_columns=fk,
            unique_columns=uq_single, composite_unique_columns=uq_composite,
            check_columns=ck, selection_columns=sel, transient_tables=transient,
            exclude_patterns=exclude_patterns,
            use_default_excludes=not args.no_default_excludes,
            drop_filestore=not args.keep_filestore,
            exclude_technical=not args.include_technical,
            overrides=ruleset,
            # enum guard samples text columns; disabled by --no-content-scan
            cur=None if args.no_content_scan else cur,
            schema=args.schema,
            sample=args.sample,
            big_text_threshold=args.big_text_threshold,
            normalize_logins=args.normalize_logins,
        )
        _logger.info("plan ready: %d columns to change, %d kept",
                     len(plan.actions), len(plan.excluded))

        _logger.info("%s %d columns…",
                     "DRY-RUN over" if (args.dry_run or not args.apply) else "APPLYING to",
                     len(plan.actions))
        report = apply_plan(
            conn, plan, schema=args.schema,
            dry_run=(args.dry_run or not args.apply),
            reset_password=args.reset_passwords,
            deactivate_crons=args.deactivate_crons,
            normalize_logins=args.normalize_logins,
            list_users=args.list_users,
        )
        report.driver = driver
        _logger.info("%s: %d columns changed, %d rows updated",
                     "dry-run complete (rolled back)" if report.dry_run else "committed",
                     report.columns_changed, report.rows_updated)

        summary = {
            "driver": driver,
            "plan": plan.summary(),
            "run": report.as_dict(),
            "skipped_rules": plan.skipped_rules,
            "unmatched": plan.unmatched,
            "excluded": plan.excluded,
        }
        _print_human(summary, plan)
        if report.user_list:
            _print_user_list(report.user_list, dry_run=report.dry_run)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, default=str)
            print(f"report written to {args.report}", file=sys.stderr)

        return 0
    finally:
        conn.close()


def _print_user_list(rows, dry_run: bool) -> None:
    """Print id/name/login/password for the sampled users (values are masked)."""
    note = " (dry-run preview — not committed)" if dry_run else ""
    print(f"\nUsers — login with these credentials{note}:")
    print(f"  {'ID':>6}  {'NAME':<28}  {'LOGIN':<32}  PASSWORD")
    for uid, name, login, password in rows:
        name = (str(name) if name is not None else "")[:28]
        login = (str(login) if login is not None else "")[:32]
        print(f"  {uid:>6}  {name:<28}  {login:<32}  {password}")


def _print_human(summary: dict, plan) -> None:
    run = summary["run"]
    mode = "DRY-RUN" if run["dry_run"] else "APPLIED"
    print(f"[{mode}] driver={summary['driver']}")
    print(f"  columns masked : {run['columns_changed']}")
    print(f"  rows updated   : {run['rows_updated']}")
    if run.get("truncated_tables"):
        print(f"  tables emptied : {len(run['truncated_tables'])} transient "
              f"({run['rows_deleted']} rows)")
    if run.get("logins_normalized"):
        print(f"  logins renamed : {run['logins_normalized']} (email → user<n>)")
    if run.get("passwords_reset"):
        print(f"  passwords reset: {run['passwords_reset']} users")
    if run.get("crons_deactivated"):
        print(f"  crons disabled : {run['crons_deactivated']}")
    print(f"  columns kept   : {len(plan.excluded)} (pk/fk/system/type/excluded)")
