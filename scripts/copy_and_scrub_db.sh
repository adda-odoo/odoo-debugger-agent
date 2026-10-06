#!/bin/bash
# Copies <db_name> from the host's main Postgres cluster into the aidev
# cluster and runs scrubdb against the copy, marking it clean only if a
# structural diff against the main original (verify_scrub_diff.py) confirms
# every column scrubdb touched actually changed and no table's row count
# drifted unexpectedly. Called from start.sh, which has already confirmed
# the database isn't already present-and-marked on aidev — this script does
# not re-check that itself.
set -euo pipefail

DB_NAME="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES_DIR="$SCRIPT_DIR/../scrubdb-poc/tools/scrubdb/rules"

SRC_HOST=/var/run/postgresql
SRC_PORT=5432
SRC_USER=odoo

DST_HOST=/var/run/postgresql
DST_PORT=5433
DST_USER=odoo

# Scrub-config registry, on the main cluster (overridable by the caller).
REGISTRY_HOST="${REGISTRY_HOST:-/var/run/postgresql}"
REGISTRY_PORT="${REGISTRY_PORT:-5432}"
REGISTRY_USER="${REGISTRY_USER:-odoo}"
REGISTRY_DB="${REGISTRY_DB:-scrubdb_registry}"

START_TS="$(date --iso-8601=seconds)"

REPORT_DIR="${HOME}/.scrubdb-reports"
mkdir -p "$REPORT_DIR"
REPORT_PATH="${REPORT_DIR}/${DB_NAME}_$(date +%s).json"
DIFF_REPORT_PATH="${REPORT_DIR}/${DB_NAME}_$(date +%s)_diff.json"

DUMP_FILE="$(mktemp --suffix=.dump)"
MERGED_RULES_FILE=""
RESTORE_LIST=""
# Cleanup-trap guards: whether an aidev copy exists to drop, and whether this
# run has the context (registry-sourced config) needed to be recorded.
AIDEV_DB_PRESENT=0
RECORD_RUN=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Append an immutable snapshot of this run to the registry (best-effort: a
# registry hiccup must not discard an otherwise-good scrub). Only when the
# config came from the registry; the fallback path has no registry to write to.
record_run() {
    local status="$1"
    [ "$RECORD_RUN" = "1" ] || return 0
    python3 "$SCRIPT_DIR/scrub_registry.py" record-run "$DB_NAME" \
        --host "$REGISTRY_HOST" --port "$REGISTRY_PORT" \
        --user "$REGISTRY_USER" --dbname "$REGISTRY_DB" \
        --status "$status" \
        --allowlist "$ALLOWLIST_FILE" --ruleset "$RULESET_FILE" \
        --odoo-version "$BASE_VERSION" \
        --report "$REPORT_PATH" --diff-report "$DIFF_REPORT_PATH" \
        --started-at "$START_TS" \
        || log "Warning: failed to record run in registry (scrub result unaffected)."
}

# Any non-zero exit after the aidev copy is created drops it, so a failed or
# partial scrub never leaves an unverified database on aidev. Disarmed only once
# the copy is verified clean and marked.
cleanup() {
    local rc=$?
    rm -f "$DUMP_FILE" "$MERGED_RULES_FILE" "$RESTORE_LIST"
    if [ "$rc" -ne 0 ] && [ "$AIDEV_DB_PRESENT" = "1" ]; then
        log "Scrub failed (exit=$rc) — dropping '$DB_NAME' from aidev."
        record_run failed
        dropdb -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" --if-exists "$DB_NAME" || true
    fi
}
trap cleanup EXIT

for bin in pg_dump pg_restore createdb dropdb psql scrubdb python3; do
    command -v "$bin" >/dev/null || { echo "Missing required host tool: $bin (see README prerequisites)" >&2; exit 1; }
done

psql -h "$SRC_HOST" -p "$SRC_PORT" -U "$SRC_USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1 \
    || { echo "Source database '$DB_NAME' not found on main (port $SRC_PORT)" >&2; exit 1; }

# Determine the source's Odoo major version from the base module. Odoo 19+
# uses the pgvector extension; earlier versions (18 and below) do not need it.
# The aidev role isn't a superuser, so `CREATE EXTENSION vector` — which a dump
# of any database that happens to have vector installed will carry — fails with
# "permission denied to create extension". For pre-19 databases that extension
# is dead weight (no vector-typed columns depend on it), so we strip it from
# the restore rather than try (and fail) to create it.
BASE_VERSION=$(psql -h "$SRC_HOST" -p "$SRC_PORT" -U "$SRC_USER" -d "$DB_NAME" -tAc \
    "SELECT latest_version FROM ir_module_module WHERE name = 'base'" 2>/dev/null || true)
ODOO_MAJOR="${BASE_VERSION%%.*}"
[ -n "$ODOO_MAJOR" ] || ODOO_MAJOR=0
log "Source '$DB_NAME' reports Odoo major version ${ODOO_MAJOR} (base=${BASE_VERSION:-unknown})."

log "Creating '$DB_NAME' on aidev..."
createdb -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" "$DB_NAME" \
    --encoding unicode --template template0 --lc-collate C --lc-ctype C
AIDEV_DB_PRESENT=1

log "Dumping '$DB_NAME' from main..."
pg_dump -h "$SRC_HOST" -p "$SRC_PORT" -U "$SRC_USER" -Fc -f "$DUMP_FILE" "$DB_NAME"

if [ "$ODOO_MAJOR" -ge 19 ]; then
    log "Restoring into aidev (Odoo ${ODOO_MAJOR}: keeping pgvector extension)..."
    pg_restore -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" -d "$DB_NAME" \
        --no-owner --no-privileges -j 4 "$DUMP_FILE"
else
    # Pre-19: filter the vector extension (and its comment) out of the archive's
    # table of contents so pg_restore never issues CREATE EXTENSION vector, which
    # the non-superuser aidev role can't run. Match the extension entries
    # precisely to avoid dropping anything else.
    log "Restoring into aidev (Odoo ${ODOO_MAJOR}: stripping unused pgvector extension)..."
    RESTORE_LIST="$(mktemp --suffix=.list)"
    pg_restore -l "$DUMP_FILE" \
        | grep -vE 'EXTENSION - vector( |$)|EXTENSION vector( |$)' > "$RESTORE_LIST"
    pg_restore -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" -d "$DB_NAME" \
        --no-owner --no-privileges -j 4 -L "$RESTORE_LIST" "$DUMP_FILE"
fi

# Scrub config. start.sh collects it per database from the registry and passes
# the resolved files in via SCRUB_ALLOWLIST_FILE / SCRUB_RULESET_FILE. Without
# them (e.g. a direct, non-interactive call), fall back to the shared files
# merged with any scripts/scrub_rules/<db>.yml override.
if [ -n "${SCRUB_ALLOWLIST_FILE:-}" ] && [ -n "${SCRUB_RULESET_FILE:-}" ]; then
    log "Using scrub config collected in the registry for '$DB_NAME'."
    ALLOWLIST_FILE="$SCRUB_ALLOWLIST_FILE"
    RULESET_FILE="$SCRUB_RULESET_FILE"
    RECORD_RUN=1
else
    ALLOWLIST_FILE="$RULES_DIR/scrub_allowlist.txt"
    COMMON_RULES_FILE="$RULES_DIR/odoo_common.yml"
    DB_RULES_FILE="$SCRIPT_DIR/scrub_rules/${DB_NAME}.yml"
    RULESET_FILE="$COMMON_RULES_FILE"
    if [ -f "$DB_RULES_FILE" ]; then
        log "Merging per-database scrub rules for '$DB_NAME' onto odoo_common.yml..."
        MERGED_RULES_FILE="$(mktemp --suffix=.yml)"
        python3 "$SCRIPT_DIR/merge_scrub_rules.py" "$COMMON_RULES_FILE" "$DB_RULES_FILE" "$MERGED_RULES_FILE"
        RULESET_FILE="$MERGED_RULES_FILE"
    fi
fi

log "Running scrubdb (apply)..."
PGHOST="$DST_HOST" PGPORT="$DST_PORT" PGUSER="$DST_USER" \
    scrubdb "$DB_NAME" --apply --i-confirm-this-is-a-copy --report "$REPORT_PATH" \
    --exclude-file "$ALLOWLIST_FILE" \
    --ruleset "$RULESET_FILE"

log "Running structural diff check (main vs aidev)..."
set +e
python3 "$SCRIPT_DIR/verify_scrub_diff.py" \
    --db "$DB_NAME" --report "$REPORT_PATH" \
    --src-host "$SRC_HOST" --src-port "$SRC_PORT" --src-user "$SRC_USER" \
    --dst-host "$DST_HOST" --dst-port "$DST_PORT" --dst-user "$DST_USER" \
    --diff-report "$DIFF_REPORT_PATH"
DIFF_EXIT=$?
set -e

if [ "$DIFF_EXIT" -eq 0 ]; then
    # Drop Odoo's generated CSS/JS asset bundles, then mark the copy clean.
    #
    # Masking leaves these attachments with a nulled url/store_fname — dead
    # rows that 404, and a cold first page load whose bundle rebuild reads
    # like an application performance bug. They are regenerable caches, so
    # dropping them just moves that rebuild to the first request.
    #
    # Deliberately after the structural diff above: that check requires every
    # non-truncated table's row count to match the source and walks both
    # databases as PK-ordered cursors zipped together, so deleting rows before
    # it would fail the count check and slide the value diff out of alignment.
    # Matched on res_model/res_id because both survive masking (name is
    # masked, url nulled).
    log "Dropping generated asset attachments (Odoo rebuilds them on demand)..."
    psql -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" -d "$DB_NAME" -c "
        DELETE FROM ir_attachment
        WHERE res_model = 'ir.ui.view' AND COALESCE(res_id, 0) = 0;
    "
    psql -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" -d "$DB_NAME" -c "
        INSERT INTO ir_config_parameter (key, value) VALUES ('scrubdb.run_completed', 'true')
        ON CONFLICT (key) DO UPDATE SET value = 'true';
    "
    # Verified clean and marked: disarm the cleanup trap so it keeps this copy.
    AIDEV_DB_PRESENT=0
    record_run success
    log "Done. '$DB_NAME' is clean and marked on aidev. Diff report: $DIFF_REPORT_PATH"
else
    # The cleanup trap records the failed run and drops the copy from aidev.
    log "Structural diff check failed (exit=$DIFF_EXIT). Diff report kept for review: $DIFF_REPORT_PATH"
    exit 1
fi
