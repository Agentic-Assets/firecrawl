#!/usr/bin/env python3
"""Queue every active listing in one source for a guarded detail refresh."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir
from cre_ingest import (
    assert_expected_database_target,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
    sql_lit,
)
from cre_validate import SOURCE_KEY_SQL


SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Must match cre_enrich.QUEUE_MUTATION_ADVISORY_LOCK. Keeping the literal local
# avoids importing the worker and its runtime orchestration into this guarded
# administrative command.
QUEUE_MUTATION_ADVISORY_LOCK = 1_687_068_469


def validate_source(source: str) -> str:
    value = source.strip()
    if not SOURCE_RE.fullmatch(value):
        raise ValueError("source must be a lowercase source key")
    return value


def source_scope_sql(source: str) -> str:
    source_lit = sql_lit(validate_source(source))
    return f"""
WITH active AS (
  SELECT
    l.brokerage_id,
    l.external_id,
    l.source_url AS url,
    {SOURCE_KEY_SQL} AS source_key
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE l.deleted_at IS NULL
),
scope AS (
  SELECT * FROM active WHERE source_key = {source_lit}
)
SELECT
  count(*)::text AS active,
  count(*) FILTER (
    WHERE external_id IS NULL OR external_id = ''
       OR url IS NULL OR url !~* '^https?://'
  )::text AS invalid,
  (
    SELECT count(*)::text
    FROM credeals.cre_enrichment_queue q
    WHERE q.source_key = {source_lit}
      AND q.done_at IS NULL
      AND q.attempts < 5
  ) AS pending,
  (
    SELECT count(*)::text
    FROM credeals.cre_enrichment_queue q
    WHERE q.source_key = {source_lit}
  ) AS queued_total,
  (
    SELECT count(*)::text
    FROM credeals.cre_enrichment_queue q
    WHERE q.source_key = {source_lit}
      AND q.claimed_at IS NOT NULL
  ) AS claimed
FROM scope;
"""


def enqueue_sql(source: str, expected_active: int) -> str:
    source_lit = sql_lit(validate_source(source))
    expected = int(expected_active)
    if expected < 1:
        raise ValueError("expected_active must be positive")
    return f"""
\\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '2min';
SET LOCAL standard_conforming_strings = on;
SELECT pg_advisory_xact_lock({QUEUE_MUTATION_ADVISORY_LOCK});

CREATE TEMP TABLE _source_refresh_scope ON COMMIT DROP AS
SELECT
  l.brokerage_id,
  l.external_id,
  l.source_url AS url,
  {SOURCE_KEY_SQL} AS source_key
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
WHERE l.deleted_at IS NULL
  AND ({SOURCE_KEY_SQL}) = {source_lit};

DO $$
DECLARE
  actual bigint;
  invalid bigint;
  claimed bigint;
BEGIN
  SELECT count(*) INTO actual FROM _source_refresh_scope;
  SELECT count(*) INTO invalid
  FROM _source_refresh_scope
  WHERE external_id IS NULL OR external_id = ''
     OR url IS NULL OR url !~* '^https?://';
  IF actual <> {expected} THEN
    RAISE EXCEPTION 'active source count changed: expected %, got %',
      {expected}, actual;
  END IF;
  IF invalid <> 0 THEN
    RAISE EXCEPTION 'source refresh scope has % invalid identities or URLs',
      invalid;
  END IF;
  SELECT count(*) INTO claimed
  FROM credeals.cre_enrichment_queue
  WHERE source_key = {source_lit}
    AND claimed_at IS NOT NULL;
  IF claimed <> 0 THEN
    RAISE EXCEPTION 'source refresh scope has % claimed queue rows', claimed;
  END IF;
END $$;

-- Replace this source's ephemeral work packets with exactly one current
-- full-refresh packet per active identity. Other sources are untouched.
DELETE FROM credeals.cre_enrichment_queue q
WHERE q.source_key = {source_lit};

INSERT INTO credeals.cre_enrichment_queue (
  brokerage_id, source_key, external_id, url, reason, priority
)
SELECT brokerage_id, source_key, external_id, url, 'changed', 20
FROM _source_refresh_scope
ON CONFLICT (brokerage_id, external_id, reason) DO UPDATE SET
  source_key = EXCLUDED.source_key,
  url = EXCLUDED.url,
  priority = EXCLUDED.priority,
  enqueued_at = now(),
  claimed_at = NULL,
  done_at = NULL,
  attempts = 0,
  last_error = NULL;

DO $$
DECLARE
  queued bigint;
  missing bigint;
  extra bigint;
BEGIN
  SELECT count(*) INTO queued
  FROM credeals.cre_enrichment_queue
  WHERE source_key = {source_lit}
    AND done_at IS NULL
    AND attempts < 5;
  SELECT count(*) INTO missing
  FROM _source_refresh_scope s
  WHERE NOT EXISTS (
    SELECT 1
    FROM credeals.cre_enrichment_queue q
    WHERE q.source_key = {source_lit}
      AND q.brokerage_id = s.brokerage_id
      AND q.external_id = s.external_id
      AND q.done_at IS NULL
      AND q.attempts < 5
  );
  SELECT count(*) INTO extra
  FROM credeals.cre_enrichment_queue q
  WHERE q.source_key = {source_lit}
    AND NOT EXISTS (
      SELECT 1
      FROM _source_refresh_scope s
      WHERE s.brokerage_id = q.brokerage_id
        AND s.external_id = q.external_id
    );
  IF queued <> {expected} OR missing <> 0 OR extra <> 0 THEN
    RAISE EXCEPTION
      'source queue reconciliation failed: queued %, missing %, extra %',
      queued, missing, extra;
  END IF;
END $$;

SELECT
  (SELECT count(*) FROM _source_refresh_scope)::text AS active,
  count(*)::text AS pending,
  count(*) FILTER (WHERE claimed_at IS NOT NULL)::text AS claimed
FROM credeals.cre_enrichment_queue q
WHERE q.source_key = {source_lit}
  AND q.done_at IS NULL
  AND q.attempts < 5;
COMMIT;
"""


def run_psql(db_url: str, sql: str) -> list[list[str]]:
    proc = subprocess.run(
        [
            find_psql(),
            *psql_connection_args(db_url),
            "-q",
            "-tA",
            "-F",
            "\t",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        env=psql_connection_env(db_url),
        input=sql,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return [
        line.split("\t")
        for line in proc.stdout.splitlines()
        if line.strip()
    ]


def scope_report(db_url: str, source: str) -> dict[str, int | str]:
    rows = run_psql(db_url, source_scope_sql(source))
    if len(rows) != 1 or len(rows[0]) != 5:
        raise RuntimeError("source scope query returned an unexpected shape")
    active, invalid, pending, queued_total, claimed = (
        int(value) for value in rows[0]
    )
    return {
        "source": source,
        "active": active,
        "invalid": invalid,
        "pending": pending,
        "queued_total": queued_total,
        "claimed": claimed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--expected-db-target-sha256")
    parser.add_argument("--expected-active-count", type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="replace this source's queue with one full-refresh packet per active row",
    )
    args = parser.parse_args(argv)
    try:
        source = validate_source(args.source)
    except ValueError as exc:
        parser.error(str(exc))
    db_url, env_path = load_db_url(args.env_file)
    assert_expected_database_target(db_url, args.expected_db_target_sha256)
    if not args.apply:
        before = scope_report(db_url, source)
        print(json.dumps({"mode": "preflight", **before}, sort_keys=True))
        return 0
    if args.expected_db_target_sha256 is None:
        parser.error("--apply requires --expected-db-target-sha256")
    if args.expected_active_count is None:
        parser.error("--apply requires --expected-active-count")
    lock_path = canonical_shared_lock_dir()
    with SharedLock(lock_path):
        before = scope_report(db_url, source)
        print(json.dumps({"mode": "preflight", **before}, sort_keys=True))
        if before["invalid"] != 0:
            raise RuntimeError(
                "refusing to enqueue a scope with invalid identities or URLs"
            )
        if before["claimed"] != 0:
            raise RuntimeError("refusing to replace claimed source queue rows")
        rows = run_psql(
            db_url,
            enqueue_sql(source, args.expected_active_count),
        )
        if len(rows) != 1 or len(rows[0]) != 3:
            raise RuntimeError("enqueue transaction returned an unexpected shape")
        active, pending, claimed = (int(value) for value in rows[0])
        after = scope_report(db_url, source)
        if (
            active != args.expected_active_count
            or pending != active
            or claimed != 0
        ):
            raise RuntimeError(
                "enqueue readback did not match the exact active scope"
            )
        # The transaction proved exact membership before COMMIT. A direct
        # worker may legitimately claim or even complete one of the new packets
        # immediately afterward, so this post-commit observation is evidence,
        # not a second atomicity gate.
        if after["active"] != active or after["invalid"] != 0:
            raise RuntimeError("post-commit active source readback changed")
    print(
        json.dumps(
            {
                "mode": "applied",
                "env_file": env_path,
                "lock": str(lock_path),
                **after,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
