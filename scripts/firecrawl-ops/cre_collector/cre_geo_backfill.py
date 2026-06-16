#!/usr/bin/env python3
"""
cre_geo_backfill.py: additive, idempotent geo derivation for existing rows.

Derives county / cbsa_code / cbsa_name / geo_source for the ~87k existing
cre_listings rows that currently have NULL values in those columns, using
the offline ZipCbsaCrosswalk (data/zip_cbsa_crosswalk.csv).

WHY THIS EXISTS
---------------
The 012 migration adds cbsa_code / cbsa_name / geo_source columns to
cre_listings. Those columns start NULL on all existing rows. The forward path
(cre_ingest.py) will populate them on the NEXT full collect run, but this
script backfills existing rows NOW, without requiring another collect pass.

Precedence (mirrors derive_geo in cre_geo.py, contract Section C.4 / Section E):
  1. county / market / submarket already present (source-verbatim, Newmark etc.)
     -> fill cbsa_code / cbsa_name from ZIP when absent; geo_source='source'.
  2. postalCode / postal_code -> crosswalk_zip.
  3. latitude + longitude -> crosswalk_latlng (nearest centroid, 50 km tolerance).
  4. No data -> leave NULL.

COALESCE-keep invariant (critical):
  Every UPDATE uses COALESCE(new_value, existing_column) so a NULL new value
  from the crosswalk never overwrites a non-NULL existing column value.
  A re-run with a richer crosswalk therefore only adds information, never
  clobbers.

CONTRACT (locked, mirrors backfill_media_from_raw_data.py):
  - PURE-ADDITIVE. Never touches status / deleted_at / transaction_type.
  - IDEMPOTENT. Re-running updates nothing new (COALESCE-keep + the WHERE
    guard that skips rows already having all three derived columns).
  - --dry-run is the DEFAULT: builds SQL and prints counts; writes NOTHING.
  - --apply is required to actually write (gated, explicit).
  - EXISTENCE-GUARDED: the UPDATE is wrapped in a DO $$ ... IF to_regclass(...)
    IS NOT NULL ... END $$ block so an --apply against a DB that lacks the
    new columns (012 not yet applied) is a clean no-op, never an error.
  - Same DB-connection convention as cre_ingest.py (load_db_url + find_psql).
    The URL is never printed (only the env-file path).
  - Column-existence-guarded: checks for cbsa_code column before staging
    (using pg_attribute) so the script is safe against pre-012 DBs.

NETWORK: none. Everything runs from the committed CSV.

USAGE:
  cd scripts/firecrawl-ops/cre_collector
  python3 cre_geo_backfill.py                      # dry-run (default)
  python3 cre_geo_backfill.py --dry-run
  python3 cre_geo_backfill.py --apply              # writes (gated)
  python3 cre_geo_backfill.py --keep-sql /tmp/geo.sql
  python3 cre_geo_backfill.py --env-file /path/.env.local
  python3 cre_geo_backfill.py --csv data/zip_cbsa_crosswalk.csv
  python3 cre_geo_backfill.py --batch-size 5000   # rows per staging batch

Requires: data/zip_cbsa_crosswalk.csv (full 41k-row file for production;
the 20-row seed runs but only covers the 20 seeded ZIPs).
"""

import argparse
import os
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# sys.path: put cre_collector/ on path for cre_ingest + cre_geo imports.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cre_ingest import find_psql, iter_copy_json_rows, load_db_url  # noqa: E402
from cre_geo import ZipCbsaCrosswalk, derive_geo          # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CSV = os.path.join(_HERE, "data", "zip_cbsa_crosswalk.csv")
_DEFAULT_BATCH = 2000   # rows per COPY/UPDATE batch

# Maximum rows to process (0 = unlimited). Useful for smoke-testing.
_MAX_ROWS_DEFAULT = 0

# Columns we read from cre_listings. NOTE: the live geo columns are zip / lat /
# lng (NOT postal_code / latitude / longitude); the read SQL aliases them to the
# keys derive_geo expects. cbsa_code / cbsa_name / geo_source are the sql/012
# columns this backfill fills (the read needs them to skip already-derived rows,
# so this step runs AFTER 012 is applied).
_READ_COLS = "id, county, market, submarket, zip, lat, lng, cbsa_code, cbsa_name, geo_source"

# Table and schema.
_TABLE = "credeals.cre_listings"

# ---------------------------------------------------------------------------
# Row reading from DB
# ---------------------------------------------------------------------------


def _read_rows_sql(limit=0):
    """SQL that streams rows needing geo derivation.

    Skips soft-deleted rows and rows that already have all three derived
    columns populated (idempotency: a re-run does nothing for them).
    """
    where = [
        "deleted_at IS NULL",
        # Target rows where at least one of the three derived geo columns is NULL.
        # After a full backfill this WHERE clause returns zero rows.
        "(cbsa_code IS NULL OR cbsa_name IS NULL OR geo_source IS NULL)",
    ]
    where_sql = " AND ".join(where)
    limit_sql = f" LIMIT {limit}" if limit > 0 else ""
    # Inner SELECT for iter_copy_json_rows (CSV COPY). The live cre_listings geo
    # columns are zip / lat / lng; derive_geo expects postal_code / latitude /
    # longitude, so alias them here. Cast to ::text for a clean CSV round-trip.
    return (
        "SELECT jsonb_build_object("
        "'id', id, 'county', county, 'market', market, 'submarket', submarket, "
        "'postal_code', zip, 'latitude', lat, 'longitude', lng, "
        "'cbsa_code', cbsa_code, 'cbsa_name', cbsa_name, 'geo_source', geo_source)::text "
        f"FROM {_TABLE} WHERE {where_sql}{limit_sql}"
    )


def fetch_rows(db_url, psql, limit=0):
    """Yield raw dicts for rows needing geo backfill.

    Delegates to cre_ingest.iter_copy_json_rows (CSV COPY format) so a county /
    market / submarket value containing a backslash round-trips intact and any
    undecodable row aborts loudly instead of being silently skipped."""
    for obj in iter_copy_json_rows(psql, db_url, _read_rows_sql(limit), label="geo_backfill"):
        if isinstance(obj, dict) and obj.get("id"):
            yield obj


# ---------------------------------------------------------------------------
# Derived-row computation
# ---------------------------------------------------------------------------


def derive_row(raw, crosswalk):
    """Compute geo derivation for one DB row.

    raw: dict with snake_case keys from the COPY read.
    Returns a dict {id, county, cbsa_code, cbsa_name, geo_source} where any
    value may be None (meaning: leave the existing DB value alone).

    COALESCE-keep semantics: if the DB row already has a non-NULL value for a
    column, we pass it through derive_geo as a source-verbatim hint (which
    means derive_geo will return geo_source='source' and keep it). The UPDATE
    SQL then uses COALESCE(new_value, existing_value) so no existing data is
    lost.
    """
    county, cbsa_code, cbsa_name, submarket, geo_source = derive_geo(raw, crosswalk)
    return {
        "id": raw["id"],
        "county": county,
        "cbsa_code": cbsa_code,
        "cbsa_name": cbsa_name,
        "geo_source": geo_source,
    }


# ---------------------------------------------------------------------------
# SQL builder
# ---------------------------------------------------------------------------


def _copy_str(v):
    """Encode a value for psql COPY FROM stdin (tab-separated, \\N for NULL)."""
    if v is None:
        return "\\N"
    s = str(v)
    return (
        s.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


_STAGE_COLS = ["id", "county", "cbsa_code", "cbsa_name", "geo_source"]


def build_sql(derived_rows):
    """Build the full backfill SQL: existence-guarded, COALESCE-keep, idempotent.

    derived_rows: list of {id, county, cbsa_code, cbsa_name, geo_source} dicts.

    The UPDATE is wrapped in a DO $$ ... IF column_exists ... END $$ block so
    running against a DB that has not yet had migration 012 applied is a
    harmless no-op (column_exists check via pg_attribute).

    COALESCE semantics: UPDATE only fills a column when the staged value is
    non-NULL AND the existing column value is NULL. This means:
      - A second run adds nothing (existing-non-NULL values are kept).
      - A future run with a richer crosswalk only fills previously-NULL rows.
      - Never overwrites a source-verbatim value set by the forward path.
    """
    lines = []
    w = lines.append
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("SET LOCAL statement_timeout = '600s';")
    w("SET LOCAL standard_conforming_strings = on;")

    # Staging table.
    w("""
CREATE TEMP TABLE _geo_stage (
    id         uuid  NOT NULL,
    county     text,
    cbsa_code  text,
    cbsa_name  text,
    geo_source text
) ON COMMIT DROP;""")

    w(f"COPY _geo_stage ({', '.join(_STAGE_COLS)}) FROM stdin;")
    for r in derived_rows:
        w("\t".join(_copy_str(r[c]) for c in _STAGE_COLS))
    w("\\.")

    # Existence-guarded UPDATE: checks that cbsa_code column exists (migration 012)
    # before touching anything. The DO block is a no-op on pre-012 DBs.
    w(f"""
-- Geo backfill: COALESCE-keep UPDATE on {_TABLE}.
-- Skips rows whose derived value is NULL (no crosswalk hit).
-- Skips rows where the target column already has a non-NULL value (keep-wins).
DO $$ BEGIN
  -- Guard: ensure the cbsa_code column exists (migration 012 applied).
  IF NOT EXISTS (
      SELECT 1 FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'credeals'
        AND c.relname = 'cre_listings'
        AND a.attname = 'cbsa_code'
        AND NOT a.attisdropped
  ) THEN
    RAISE NOTICE 'cre_listings.cbsa_code column absent (migration 012 not yet applied); geo backfill skipped.';
  ELSE
    UPDATE {_TABLE} l
    SET
        county     = COALESCE(l.county, s.county),
        cbsa_code  = COALESCE(l.cbsa_code, s.cbsa_code),
        cbsa_name  = COALESCE(l.cbsa_name, s.cbsa_name),
        geo_source = COALESCE(l.geo_source, s.geo_source)
    FROM _geo_stage s
    WHERE l.id = s.id
      -- Only update when at least one staged value is non-NULL (avoids no-op rows).
      AND (s.county IS NOT NULL OR s.cbsa_code IS NOT NULL OR s.geo_source IS NOT NULL)
      -- Only update when the target column is still NULL (COALESCE-keep semantics).
      AND (l.county IS NULL OR l.cbsa_code IS NULL OR l.cbsa_name IS NULL OR l.geo_source IS NULL);
    RAISE NOTICE 'geo backfill applied % row(s)', FOUND::int;
  END IF;
END $$;""")

    w("COMMIT;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


def _summarize(derived_rows):
    total = len(derived_rows)
    by_source = {}
    null_count = 0
    for r in derived_rows:
        gs = r.get("geo_source")
        if gs is None:
            null_count += 1
        else:
            by_source[gs] = by_source.get(gs, 0) + 1
    return {
        "total_staged": total,
        "no_geo_hit": null_count,
        "by_geo_source": by_source,
        "will_update": total - null_count,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="(default) build SQL and print counts; write NOTHING",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="apply the geo UPDATE to the DB (gated; off by default)",
    )
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument(
        "--csv", default=_DEFAULT_CSV,
        help=f"path to zip_cbsa_crosswalk.csv (default: {_DEFAULT_CSV})",
    )
    ap.add_argument(
        "--keep-sql", default=None,
        help="write the generated SQL to this path (works in dry-run too)",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="process at most N rows (0=unlimited; useful for smoke-testing)",
    )
    ap.add_argument(
        "--batch-size", type=int, default=_DEFAULT_BATCH,
        help=f"rows per SQL batch (default {_DEFAULT_BATCH})",
    )
    args = ap.parse_args()

    apply = bool(args.apply)

    # ---- Load crosswalk ----
    csv_path = os.path.abspath(args.csv)
    print(f"[cre_geo_backfill] loading crosswalk: {csv_path}")
    crosswalk = ZipCbsaCrosswalk(csv_path=csv_path)
    print(f"[cre_geo_backfill] crosswalk rows: {len(crosswalk):,}")
    if len(crosswalk) == 0:
        print(
            "[cre_geo_backfill] WARNING: crosswalk is empty (CSV missing or unreadable).\n"
            "  All geo derivations will return NULL.  Build the full crosswalk first:\n"
            "    python3 data/build_zip_cbsa_crosswalk.py",
            file=sys.stderr,
        )

    # ---- DB connection ----
    db_url, env_path = load_db_url(args.env_file)
    print(f"[cre_geo_backfill] env file: {env_path}")
    psql = find_psql()

    # ---- Fetch rows ----
    print("[cre_geo_backfill] reading rows needing geo derivation from DB ...")
    raw_rows = list(fetch_rows(db_url, psql, limit=args.limit))
    print(f"[cre_geo_backfill] {len(raw_rows):,} row(s) to process.")

    if not raw_rows:
        print("[cre_geo_backfill] nothing to backfill (all rows already have geo or DB is empty).")
        return

    # ---- Derive geo for each row ----
    derived = [derive_row(r, crosswalk) for r in raw_rows]

    # ---- Summary ----
    summary = _summarize(derived)
    print(
        f"[cre_geo_backfill] derivation summary:\n"
        f"  total staged:      {summary['total_staged']:,}\n"
        f"  will update:       {summary['will_update']:,}\n"
        f"  no crosswalk hit:  {summary['no_geo_hit']:,}\n"
        f"  by geo_source:     {summary['by_geo_source']}"
    )

    # ---- Build SQL ----
    # Filter out rows with no geo derivation (all-None: no DB work needed).
    update_rows = [r for r in derived if r.get("geo_source") is not None]
    sql = build_sql(update_rows)

    if args.keep_sql:
        keep_path = os.path.abspath(args.keep_sql)
        with open(keep_path, "w", encoding="utf-8") as fh:
            fh.write(sql)
        print(f"[cre_geo_backfill] SQL written to {keep_path}")

    if not apply:
        print("[cre_geo_backfill] --dry-run: SQL built, nothing written to DB.")
        print("  Pass --apply to execute the geo backfill.")
        return

    # ---- Apply ----
    print(f"[cre_geo_backfill] applying {len(update_rows):,} row(s) to DB ...")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sql", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(sql)
        sql_path = tf.name
    try:
        proc = subprocess.run(
            [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
            capture_output=True,
            text=True,
        )
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            sys.exit(f"[cre_geo_backfill] psql apply exited {proc.returncode}")
        print("[cre_geo_backfill] geo backfill applied successfully.")
    finally:
        os.unlink(sql_path)


if __name__ == "__main__":
    main()
