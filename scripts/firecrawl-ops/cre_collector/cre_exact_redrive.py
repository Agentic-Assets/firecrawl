"""Redrive exact exhausted SVN queue rows with a retained pre-change snapshot.

The private snapshot retains only the queue row's current count and last error;
older per-attempt history is not present in the queue schema.
"""
from __future__ import annotations

import json
import re
import uuid

from cre_ingest import sql_lit

MAX_ROWS = 10


def validate_selection(source, row_ids):
    if source != "svn" or not isinstance(row_ids, list) or not 1 <= len(row_ids) <= MAX_ROWS:
        raise ValueError("select one to ten exact SVN queue ids")
    try:
        parsed = [str(uuid.UUID(value)) for value in row_ids]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid queue id") from exc
    if len(set(parsed)) != len(parsed) or parsed != row_ids:
        raise ValueError("queue ids must be unique canonical UUIDs")
    return parsed


def build_snapshot_sql(source, row_ids):
    ids = validate_selection(source, row_ids)
    values = ", ".join(sql_lit(item) + "::uuid" for item in ids)
    return f"""\\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='10s'; SET LOCAL lock_timeout='1s';
SELECT COALESCE(jsonb_agg(to_jsonb(q) ORDER BY q.id), '[]'::jsonb)
FROM credeals.cre_enrichment_queue q WHERE q.source_key='svn' AND q.id IN ({values});
ROLLBACK;
"""


def build_apply_sql(snapshot, target_sha256):
    if not isinstance(target_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", target_sha256):
        raise ValueError("exact database target fingerprint required")
    if not isinstance(snapshot, list) or not snapshot or any(not isinstance(row, dict) for row in snapshot):
        raise ValueError("nonempty exact row snapshot required")
    validate_selection("svn", [row.get("id") for row in snapshot])
    required = {"id", "source_key", "brokerage_id", "external_id", "url", "reason", "priority",
                "enqueued_at", "claimed_at", "done_at", "attempts", "last_error"}
    if any(not required.issubset(row) or row["source_key"] != "svn" for row in snapshot):
        raise ValueError("snapshot lacks required SVN queue state")
    value = sql_lit(json.dumps(snapshot, separators=(",", ":"), allow_nan=False))
    return f"""\\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='15s';
SET LOCAL standard_conforming_strings=on;
SELECT pg_advisory_xact_lock(1687068469);
WITH expected AS (SELECT value AS snapshot FROM jsonb_array_elements({value}::jsonb)),
locked AS MATERIALIZED (
 SELECT q.id FROM credeals.cre_enrichment_queue q
 JOIN expected e ON q.id=(e.snapshot->>'id')::uuid
 JOIN credeals.cre_listings l ON l.brokerage_id=q.brokerage_id AND l.external_id=q.external_id
 WHERE q.source_key='svn' AND to_jsonb(q)=e.snapshot
   AND q.done_at IS NULL AND q.claimed_at IS NULL AND q.attempts >= 5
   AND q.external_id IS NOT NULL AND q.external_id <> '' AND q.url ~ '^https?://'
   AND l.status='active' AND l.deleted_at IS NULL AND l.source_url=q.url
 ORDER BY q.id FOR UPDATE OF q,l
), updated AS (
 UPDATE credeals.cre_enrichment_queue q SET attempts=0
 FROM locked WHERE q.id=locked.id
 RETURNING q.id::text
)
SELECT jsonb_build_object('applied', COALESCE(jsonb_agg(id ORDER BY id), '[]'::jsonb)) FROM updated;
COMMIT;
"""


def write_private_receipt(directory, name, payload):
    """Publish an exclusive private receipt and durably sync its directory."""
    import hashlib
    import os

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(dirfd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("receipt directory must be private and owned")
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
    return hashlib.sha256(raw).hexdigest()


import subprocess
import sys
from pathlib import Path

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir
from cre_guarded_tier import _create_run_dir
from cre_ingest import (
    assert_expected_database_target,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
)
from source_evidence.guarded_fence import require_clear

HERE = Path(__file__).resolve().parent


def allocate_directory(root):
    return _create_run_dir(root)


def verify_exact_checkout(expected_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("full expected SHA required")
    def git(*args):
        result = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            raise RuntimeError("checkout verification failed")
        return result.stdout.splitlines()
    if (git("rev-parse", "HEAD") != [expected_sha] or
            git("status", "--porcelain", "--untracked-files=normal") or
            expected_sha not in git("for-each-ref", "--format=%(objectname)", "refs/remotes/origin/")):
        raise ValueError("checkout must be clean and match a local origin tracking tip")


def query(db_url, statement):
    result = subprocess.run(
        [find_psql(), "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", *psql_connection_args(db_url)],
        env=psql_connection_env(db_url), input=statement, capture_output=True, text=True,
        timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("database operation failed; check recovery receipt")
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("database result unavailable; check recovery receipt") from exc


def run_redrive(row_ids, expected_sha, expected_target, env_file):
    ids = validate_selection("svn", row_ids)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_target):
        raise ValueError("expected database target fingerprint required")
    verify_exact_checkout(expected_sha)
    db_url, _ = load_db_url(env_file)
    assert_expected_database_target(db_url, expected_target)
    lock = SharedLock(canonical_shared_lock_dir(), recovery_required=True)
    with lock:
        dispatched = False
        settled = False
        try:
            require_clear(lock.path)
            verify_exact_checkout(expected_sha)
            run_dir = allocate_directory(HERE / "out" / "exact-redrive")
            rows = query(db_url, build_snapshot_sql("svn", ids))
            if not isinstance(rows, list):
                raise TypeError("snapshot result invalid")
            snapshot = {"schema": "cre-exact-redrive-snapshot/v1", "source": "svn",
                        "expected_sha": expected_sha, "database_target_sha256": expected_target,
                        "requested_ids": ids, "rows": rows}
            digest = write_private_receipt(run_dir, "snapshot.json", snapshot)
            print(str(run_dir), flush=True)
            if rows:
                statement = build_apply_sql(rows, expected_target)
                verify_exact_checkout(expected_sha)
                dispatched = True
                result = query(db_url, statement)
                if not isinstance(result, dict) or not isinstance(result.get("applied"), list):
                    raise RuntimeError("write result unavailable; check recovery receipt")
                applied = result["applied"]
                if len(set(applied)) != len(applied) or any(item not in ids for item in applied):
                    raise RuntimeError("write result invalid; check recovery receipt")
            else:
                applied = []
            write_private_receipt(run_dir, "result.json", {
                "schema": "cre-exact-redrive-result/v1", "snapshot_sha256": digest,
                "applied": applied, "held": [item for item in ids if item not in applied],
            })
            settled = True
            return run_dir
        finally:
            if not dispatched or settled:
                lock.clear_recovery_requirement()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-id", action="append", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-db-target-sha256", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required")
    run_redrive(args.queue_id, args.expected_sha, args.expected_db_target_sha256, args.env_file)


if __name__ == "__main__":
    try:
        main()
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"redrive failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
