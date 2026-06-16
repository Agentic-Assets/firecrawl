#!/usr/bin/env python3
"""
om_classify_existing.py: one-time ADDITIVE re-classification of the
cre_listing_documents rows that are currently ALL stored as doc_type='brochure'.

WHY THIS EXISTS
---------------
The 70,414 existing cre_listing_documents rows were written before the widened
doc_type taxonomy (sql/011 adds om / financials / rent_roll / floor_plan / flyer).
Every row today carries doc_type='brochure'. This script re-runs the canonical
classify_doc(url, title) function (cre_parse.py, Section D of the Phase-2
contract) over each 'brochure' row and UPDATEs doc_type ONLY when the classifier
returns a MORE SPECIFIC type. It never downgrades and never touches rows already
typed to a non-brochure value.

INVARIANTS (binding per contract Section D + H)
------------------------------------------------
* UPGRADE-ONLY. A row whose classify_doc() still returns 'brochure', 'other',
  or None is LEFT as 'brochure'. No row is ever downgraded from a more-specific
  type ('om', 'financials', 'rent_roll', 'floor_plan', 'flyer') to 'brochure'
  or any less-specific token.
* Only rows currently typed 'brochure' are candidates. Rows already typed to any
  other value (om, financials, rent_roll, floor_plan, flyer, other) are ignored.
* The SQL UPDATE is wrapped in a to_regclass('credeals.cre_listing_documents')
  guard. If the table is somehow absent the script exits cleanly (this guard is
  purely a safety rail; the table has existed since sql/002 and is always present
  in any non-empty environment).
* --dry-run is the DEFAULT. Running without --apply prints a per-old->new-type
  count table and exits without writing. --apply is gated and explicit.
* Same DB-connection convention as backfill_media_from_raw_data.py and
  cre_ingest.py: POSTGRES_URL_NON_POOLING / POSTGRES_URL via psql, discovered
  through cre_ingest.load_db_url / cre_ingest.find_psql. The URL is never printed.

CONTRACT with cre_parse.classify_doc
-------------------------------------
classify_doc(url, title) is the SINGLE source of truth (cre_parse.py, Section D).
This script never re-implements the keyword logic; it always delegates there.

MORE-SPECIFIC type set (can upgrade TO these from 'brochure'):
    om, financials, rent_roll, floor_plan, flyer

NEVER-UPGRADE types (returned by classify_doc but must not cause a change here):
    'brochure'  -> already at that level, no change
    'other'     -> less specific than 'brochure'; never downgrade
    None        -> unclassifiable url; no change

Usage:
    python3 om_classify_existing.py              # dry-run (default; prints counts)
    python3 om_classify_existing.py --dry-run
    python3 om_classify_existing.py --apply      # writes (gated; explicit)
    python3 om_classify_existing.py --keep-sql /tmp/classify.sql --dry-run
    python3 om_classify_existing.py --env-file /path/to/.env.local

Reads the same env discovery order as cre_ingest.py:
    --env-file flag > CRE_ENV_FILE env var > ~/Documents defaults.
"""

import argparse
import os
import subprocess
import sys
import tempfile

# Put cre_collector/ on sys.path so cre_ingest and cre_parse are importable
# whether this script is run directly or under pytest.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cre_ingest import find_psql, iter_copy_json_rows, load_db_url, sql_lit  # noqa: E402
from cre_parse import classify_doc  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The types that classify_doc() can return and that represent an upgrade from
# 'brochure'. Only these trigger an UPDATE.
UPGRADE_TYPES = frozenset({"om", "financials", "rent_roll", "floor_plan", "flyer"})

# For guard messages and counts.
BROCHURE = "brochure"


# ---------------------------------------------------------------------------
# DB read: fetch all brochure-typed rows from cre_listing_documents
# ---------------------------------------------------------------------------


def read_brochure_rows_sql():
    """Inner SELECT (one JSON object per row) for iter_copy_json_rows: (id, url,
    title) for all doc_type='brochure' rows. Cast to ::text for a clean CSV
    round-trip (CSV COPY avoids the text-format backslash-doubling that would
    silently drop a row whose title/url contains a backslash)."""
    return (
        "SELECT jsonb_build_object("
        "'id', d.id, "
        "'url', d.url, "
        "'title', d.title)::text "
        "FROM credeals.cre_listing_documents d "
        "WHERE d.doc_type = 'brochure'"
    )


def fetch_brochure_rows(db_url, psql):
    """Yield (row_id, url, title) per brochure row via iter_copy_json_rows (CSV
    COPY; aborts loudly on an undecodable row instead of silently skipping it)."""
    for obj in iter_copy_json_rows(psql, db_url, read_brochure_rows_sql(), label="classify"):
        row_id = obj.get("id")
        if row_id is not None:
            yield row_id, obj.get("url"), obj.get("title")


# ---------------------------------------------------------------------------
# Classification pass: determine which rows upgrade and to what
# ---------------------------------------------------------------------------


def classify_upgrades(rows):
    """Apply classify_doc to each (id, url, title) tuple.

    Returns a list of (row_id, old_type, new_type) where old_type is always
    'brochure' and new_type is one of UPGRADE_TYPES. Rows that classify to
    'brochure', 'other', or None are excluded (no upgrade warranted).

    This is the pure, testable core; it never touches the DB.
    """
    upgrades = []
    for row_id, url, title in rows:
        new_type = classify_doc(url, title)
        if new_type in UPGRADE_TYPES:
            upgrades.append((row_id, BROCHURE, new_type))
    return upgrades


# ---------------------------------------------------------------------------
# SQL builder: one UPDATE statement per upgraded row (batched via literal list)
# ---------------------------------------------------------------------------


def build_sql(upgrades):
    """Build the guarded, upgrade-only UPDATE SQL.

    Uses a VALUES list in a single UPDATE ... FROM (VALUES ...) AS u(id, doc_type)
    statement so the entire batch lands in one round-trip. The statement is:
    - Wrapped in a to_regclass guard (safety rail; the table always exists).
    - WHERE doc_type = 'brochure' double-guard: even if a row was upgraded by a
      concurrent write between our read and this UPDATE, the WHERE prevents a
      downgrade.
    - BEGIN/COMMIT for atomicity.
    """
    lines = []
    w = lines.append
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("SET LOCAL statement_timeout = '300s';")
    w("SET LOCAL standard_conforming_strings = on;")

    # to_regclass guard: the table always exists (sql/002), but mirror the
    # backfill_media_from_raw_data.py safety pattern.
    w("""
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_documents') IS NULL THEN
    RAISE EXCEPTION 'cre_listing_documents table not found; aborting classify pass';
  END IF;
END $$;""")

    if not upgrades:
        # Nothing to do; emit a harmless SQL comment so the file is valid.
        w("-- [classify] no upgrade candidates found; nothing to UPDATE.")
    else:
        # Build VALUES list: (uuid, doc_type_text)
        value_rows = ",\n    ".join(
            f"({sql_lit(str(row_id))}::uuid, {sql_lit(new_type)})"
            for row_id, _old, new_type in upgrades
        )
        w(f"""
-- Upgrade-only UPDATE: only rows STILL typed 'brochure' are changed.
-- The WHERE doc_type = 'brochure' guard prevents any downgrade even if a row
-- was reclassified concurrently between our read and this UPDATE.
UPDATE credeals.cre_listing_documents AS d
SET    doc_type = u.new_doc_type
FROM   (VALUES
    {value_rows}
) AS u(id, new_doc_type)
WHERE  d.id        = u.id
  AND  d.doc_type  = 'brochure';""")

    w("COMMIT;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry-run summary
# ---------------------------------------------------------------------------


def _summarize(upgrades, total_brochure_rows):
    """Return a per-old->new count dict and print a human-readable table."""
    by_type = {}
    for _row_id, old_type, new_type in upgrades:
        key = (old_type, new_type)
        by_type[key] = by_type.get(key, 0) + 1

    print(f"[classify] total rows scanned (doc_type='brochure'): {total_brochure_rows}")
    print(f"[classify] upgrade candidates: {len(upgrades)}")
    if by_type:
        print("[classify] per-old->new-type counts:")
        for (old_type, new_type), count in sorted(by_type.items()):
            print(f"           {old_type:12s} -> {new_type:12s} : {count}")
    else:
        print("[classify] (no upgrades; all rows remain 'brochure')")
    return by_type


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="(default) classify, print per-old->new-type counts, write nothing",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="actually UPDATE the doc_type column (gated; off by default)",
    )
    ap.add_argument(
        "--env-file",
        default=None,
        help="env file holding POSTGRES_URL* (same discovery as cre_ingest.py)",
    )
    ap.add_argument(
        "--keep-sql",
        default=None,
        help="write the generated SQL to this path (works in dry-run too)",
    )
    args = ap.parse_args()

    apply = bool(args.apply)

    db_url, env_path = load_db_url(args.env_file)
    print(f"[classify] env file: {env_path}")  # path only, never the URL
    psql = find_psql()

    rows = list(fetch_brochure_rows(db_url, psql))
    total_scanned = len(rows)

    upgrades = classify_upgrades(rows)

    by_type = _summarize(upgrades, total_scanned)

    sql = build_sql(upgrades)
    if args.keep_sql:
        with open(args.keep_sql, "w") as f:
            f.write(sql)
        print(f"[classify] SQL written to {args.keep_sql}")

    if not apply:
        print("[classify] DRY-RUN: no rows written. Re-run with --apply to write.")
        return

    if not upgrades:
        print("[classify] nothing to upgrade; skipping --apply write.")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql)
        sql_path = tf.name
    try:
        proc = subprocess.run(
            [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
            capture_output=True,
            text=True,
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            sys.exit(f"[classify] psql apply exited {proc.returncode}")
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
    finally:
        os.unlink(sql_path)

    print(f"[classify] APPLIED: {len(upgrades)} row(s) upgraded.")
    for (old_type, new_type), count in sorted(by_type.items()):
        print(f"           {old_type:12s} -> {new_type:12s} : {count}")


if __name__ == "__main__":
    main()
