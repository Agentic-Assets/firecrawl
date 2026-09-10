#!/usr/bin/env python3
"""Repair one verified Colliers derived-price-per-SF regression, fail closed.

The 2026-09-10 strict Colliers refresh persisted $16,000,000 / 857 as
$18,669.78/SF. The provider's generic Size field is not safe to reinterpret,
so this repair preserves price and size and clears only the manufactured
``sale_price_per_sf`` value. Default mode is read-only. Mutation requires the
canonical CRE lock, the exact production target, immutable row predicates, and
an owner-only preimage. ``--verify-apply-rollback`` proves the transaction and
rolls it back before the real apply.

The SQL assigns only ``sale_price_per_sf``. The table's existing BEFORE UPDATE
trigger also advances ``updated_at``; the tool records that expected database
side effect and never disables the trigger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir
from cre_ingest import (
    assert_expected_database_target,
    find_psql,
    lifecycle_transaction_lock_sql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
    sql_lit,
)

EXPECTED_DB_TARGET_SHA256 = (
    "faf5d034d1f085ce09dd7afd0cc013dcbf474a81a73dc60fafa6c8884bfdf9ee"
)
EXPECTED_ID = "55b6c2a2-15d6-4908-b0de-263b29b7e6b0"
EXPECTED_BROKERAGE_ID = "fec64736-b1f0-4b5c-8e71-6661868be758"
EXPECTED_EXTERNAL_ID = "152946"
EXPECTED_TITLE = "Greenville, NC Multifamily Portfolio"
EXPECTED_GENERATION = "2026-09-10T043500Z"
EXPECTED_PRICE = 16_000_000
EXPECTED_SIZE = 857
EXPECTED_PSF = 18_669.78
ADVISORY_LOCK_KEY = 734_251_907_300_731_003
MAX_PREIMAGE_BYTES = 4 * 1024 * 1024


def generation_expr(alias: str) -> str:
    return f"""COALESCE(
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{secondary_pass,freshnessProvenance,generationId}}','')
    )"""


def state_sql() -> str:
    return f"""
SELECT jsonb_build_object(
  'row', to_jsonb(l),
  'brokerage_slug', b.slug,
  'source_key', l.raw_data->>'sourceKey',
  'generation', {generation_expr('l')}
)::text
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
WHERE l.id={sql_lit(EXPECTED_ID)}::uuid;
"""


def validate_state(state: dict, *, repaired: bool) -> None:
    row = state.get("row") if isinstance(state, dict) else None
    if not isinstance(row, dict):
        raise TypeError("exact Colliers repair target is absent")
    expected = {
        "id": EXPECTED_ID,
        "brokerage_id": EXPECTED_BROKERAGE_ID,
        "external_id": EXPECTED_EXTERNAL_ID,
        "title": EXPECTED_TITLE,
        "status": "active",
        "deleted_at": None,
        "sale_price_usd": EXPECTED_PRICE,
        "size_sf": EXPECTED_SIZE,
    }
    for key, value in expected.items():
        if row.get(key) != value:
            raise ValueError(f"repair target {key} drifted")
    if state.get("brokerage_slug") != "colliers":
        raise ValueError("repair target brokerage drifted")
    if state.get("source_key") != "colliers":
        raise ValueError("repair target source provenance drifted")
    if state.get("generation") != EXPECTED_GENERATION:
        raise ValueError("repair target generation drifted")
    psf = row.get("sale_price_per_sf")
    if repaired:
        if psf is not None:
            raise ValueError("repair target price per SF was not cleared")
    else:
        if not isinstance(psf, (int, float)):
            raise ValueError("repair target price per SF is not numeric")
        if float(psf) != EXPECTED_PSF:
            raise ValueError("repair target price per SF drifted")


def row_guard(
    state: dict, *, repaired: bool, guard_updated_at: bool = True
) -> str:
    row = state["row"]
    psf_guard = (
        "l.sale_price_per_sf IS NULL"
        if repaired
        else f"l.sale_price_per_sf={EXPECTED_PSF}::numeric"
    )
    updated_at_guard = (
        f"AND l.updated_at={sql_lit(row['updated_at'])}::timestamptz"
        if guard_updated_at
        else ""
    )
    raw_data = json.dumps(row["raw_data"], sort_keys=True, separators=(",", ":"))
    return f"""
    l.id={sql_lit(EXPECTED_ID)}::uuid
    AND l.brokerage_id={sql_lit(EXPECTED_BROKERAGE_ID)}::uuid
    AND l.external_id={sql_lit(EXPECTED_EXTERNAL_ID)}
    AND l.title={sql_lit(EXPECTED_TITLE)}
    AND l.status='active'
    AND l.deleted_at IS NULL
    AND l.sale_price_usd={EXPECTED_PRICE}::numeric
    AND l.size_sf={EXPECTED_SIZE}::numeric
    AND {psf_guard}
    {updated_at_guard}
    AND l.raw_data={sql_lit(raw_data)}::jsonb
    AND l.raw_data->>'sourceKey'='colliers'
    AND {generation_expr('l')}={sql_lit(EXPECTED_GENERATION)}
    AND EXISTS (
      SELECT 1 FROM credeals.cre_brokerages b
      WHERE b.id=l.brokerage_id AND b.slug='colliers'
    )
"""


def mutation_sql(
    state: dict,
    *,
    commit: bool,
    rollback: bool = False,
    guard_updated_at: bool = True,
) -> str:
    validate_state(state, repaired=rollback)
    assignment = (
        f"sale_price_per_sf={EXPECTED_PSF}::numeric"
        if rollback
        else "sale_price_per_sf=NULL"
    )
    expected_post = (
        f"l.sale_price_per_sf={EXPECTED_PSF}::numeric"
        if rollback
        else "l.sale_price_per_sf IS NULL"
    )
    mode = "rollback_applied" if rollback else ("applied" if commit else "verified_rollback")
    finish = "COMMIT;" if commit else "ROLLBACK;"
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
-- Serialize with every collector lifecycle transaction, including remote writers.
{lifecycle_transaction_lock_sql()}
-- Then serialize this exact repair independently of the broader lifecycle lock.
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
CREATE TEMP TABLE _colliers_psf_changed(id uuid PRIMARY KEY) ON COMMIT DROP;
WITH changed AS (
  UPDATE credeals.cre_listings l
  SET {assignment}
  WHERE {row_guard(state, repaired=rollback, guard_updated_at=guard_updated_at)}
  RETURNING l.id
)
INSERT INTO _colliers_psf_changed SELECT id FROM changed;
DO $check$
DECLARE changed_count integer; bad_count integer;
BEGIN
  SELECT count(*) INTO changed_count FROM _colliers_psf_changed;
  SELECT count(*) INTO bad_count
  FROM credeals.cre_listings l
  WHERE l.id={sql_lit(EXPECTED_ID)}::uuid
    AND NOT (
      l.brokerage_id={sql_lit(EXPECTED_BROKERAGE_ID)}::uuid
      AND l.external_id={sql_lit(EXPECTED_EXTERNAL_ID)}
      AND l.title={sql_lit(EXPECTED_TITLE)}
      AND l.status='active'
      AND l.deleted_at IS NULL
      AND l.sale_price_usd={EXPECTED_PRICE}::numeric
      AND l.size_sf={EXPECTED_SIZE}::numeric
      AND {expected_post}
      AND l.raw_data={sql_lit(json.dumps(state['row']['raw_data'], sort_keys=True, separators=(',', ':')))}::jsonb
      AND l.raw_data->>'sourceKey'='colliers'
      AND {generation_expr('l')}={sql_lit(EXPECTED_GENERATION)}
    );
  IF changed_count <> 1 OR bad_count <> 0 THEN
    RAISE EXCEPTION 'Colliers PSF repair failed closed: changed %, bad %',
      changed_count, bad_count;
  END IF;
END
$check$;
SELECT jsonb_build_object(
  'ok', true,
  'mode', {sql_lit(mode)},
  'changed', (SELECT count(*) FROM _colliers_psf_changed),
  'id', {sql_lit(EXPECTED_ID)},
  'field', 'sale_price_per_sf',
  'new_value', {'to_jsonb(' + str(EXPECTED_PSF) + '::numeric)' if rollback else 'NULL::numeric'},
  'updated_at_disposition', 'advanced_by_table_trigger'
)::text;
{finish}
"""


def run_psql(db_url: str, sql: str) -> dict:
    proc = subprocess.run(
        [
            find_psql(),
            *psql_connection_args(db_url),
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-P",
            "pager=off",
            "-P",
            "footer=off",
            "-A",
            "-t",
            "-f",
            "-",
        ],
        env=psql_connection_env(db_url),
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        raise RuntimeError(f"psql exited {proc.returncode}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("psql returned no JSON result")
    return json.loads(lines[-1])


def private_json_bytes(payload: dict) -> bytes:
    data = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    if len(data) > MAX_PREIMAGE_BYTES:
        raise ValueError("preimage exceeds size limit")
    return data


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("private evidence path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {".", ".."}:
            raise ValueError("private evidence path must not contain dot segments")
        current /= part
        if current.is_symlink():
            raise ValueError("private evidence path must not contain symlinks")


def write_private_bytes(path: Path, data: bytes) -> str:
    _reject_symlink_components(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise ValueError("private evidence parent must be an owned directory")
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        raise FileExistsError("private evidence path exists; refusing overwrite")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(data).hexdigest()


def write_private_preimage(path: Path, payload: dict) -> str:
    return write_private_bytes(path, private_json_bytes(payload))


def fsync_directory(path: Path) -> None:
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    parent_fd = os.open(path, dir_flags)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def replace_private_bytes(path: Path, data: bytes) -> str:
    """Atomically replace one already-reserved owner-only evidence file."""
    _reject_symlink_components(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("reserved evidence must be an owner-only regular file")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write_private_bytes(tmp, data)
    os.replace(tmp, path)
    fsync_directory(path.parent)
    return hashlib.sha256(data).hexdigest()


def cli_private_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError("private evidence CLI paths must be absolute")
    _reject_symlink_components(expanded)
    return expanded


def read_private_preimage(path: Path, expected_sha256: str) -> dict:
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("rollback requires an absolute path and lowercase SHA-256")
    _reject_symlink_components(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("rollback preimage must be an owner-only regular file")
    data = path.read_bytes()
    if len(data) > MAX_PREIMAGE_BYTES or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("rollback preimage size or SHA-256 does not match")
    payload = json.loads(data)
    if payload.get("db_target_sha256") != EXPECTED_DB_TARGET_SHA256:
        raise ValueError("rollback preimage database target does not match")
    validate_state(payload.get("state"), repaired=False)
    return payload


def public_summary(state: dict, mode: str) -> dict:
    row = state["row"]
    return {
        "ok": True,
        "mode": mode,
        "id": row["id"],
        "external_id": row["external_id"],
        "generation": state["generation"],
        "sale_price_usd": row["sale_price_usd"],
        "size_sf": row["size_sf"],
        "sale_price_per_sf": row["sale_price_per_sf"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-apply-rollback", action="store_true")
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--rollback-preimage", type=Path)
    parser.add_argument("--expected-preimage-sha256")
    args = parser.parse_args(argv)
    if sum(bool(x) for x in (args.apply, args.verify_apply_rollback, args.rollback_preimage)) > 1:
        parser.error("mutation modes are mutually exclusive")
    if args.apply != bool(args.preimage):
        parser.error("--apply and --preimage are required together")
    if bool(args.rollback_preimage) != bool(args.expected_preimage_sha256):
        parser.error("rollback preimage and expected SHA-256 are required together")

    db_url, _env_path = load_db_url(args.env_file)
    assert_expected_database_target(db_url, EXPECTED_DB_TARGET_SHA256)
    with SharedLock(canonical_shared_lock_dir()):
        if args.rollback_preimage:
            rollback_preimage = cli_private_path(args.rollback_preimage)
            read_private_preimage(
                rollback_preimage, args.expected_preimage_sha256
            )
            current = run_psql(db_url, state_sql())
            validate_state(current, repaired=True)
            result = run_psql(
                db_url, mutation_sql(current, commit=True, rollback=True)
            )
            print(json.dumps(result, sort_keys=True))
            return 0

        before = run_psql(db_url, state_sql())
        validate_state(before, repaired=False)
        if not (args.apply or args.verify_apply_rollback):
            print(json.dumps(public_summary(before, "preflight"), sort_keys=True))
            return 0
        if args.verify_apply_rollback:
            result = run_psql(db_url, mutation_sql(before, commit=False))
            after = run_psql(db_url, state_sql())
            if after != before:
                raise RuntimeError("rolled-back verification changed the target row")
            print(json.dumps(result, sort_keys=True))
            return 0

        preimage = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "db_target_sha256": EXPECTED_DB_TARGET_SHA256,
            "repair": "colliers-derived-psf-v1",
            "state": before,
        }
        preimage_path = cli_private_path(args.preimage)
        postimage_path = preimage_path.with_name("postimage.json")
        rollback_sql_path = preimage_path.with_name("rollback.sql")
        for evidence_path in (preimage_path, postimage_path, rollback_sql_path):
            _reject_symlink_components(evidence_path)
            if evidence_path.exists() or evidence_path.is_symlink():
                raise FileExistsError(
                    f"private evidence path exists; refusing apply: {evidence_path}"
                )
        predicted_after = json.loads(json.dumps(before))
        predicted_after["row"]["sale_price_per_sf"] = None
        rollback_sql = mutation_sql(
            predicted_after,
            commit=True,
            rollback=True,
            guard_updated_at=False,
        ).encode("utf-8")
        rollback_sql_sha256 = write_private_bytes(rollback_sql_path, rollback_sql)
        preimage["rollback_sql"] = str(rollback_sql_path)
        preimage["rollback_sql_sha256"] = rollback_sql_sha256
        sha256 = write_private_preimage(preimage_path, preimage)
        pending_postimage = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "db_target_sha256": EXPECTED_DB_TARGET_SHA256,
            "preimage": str(preimage_path),
            "preimage_sha256": sha256,
            "repair": "colliers-derived-psf-v1",
            "rollback_sql": str(rollback_sql_path),
            "rollback_sql_sha256": rollback_sql_sha256,
            "status": "pending_database_apply",
        }
        write_private_bytes(postimage_path, private_json_bytes(pending_postimage))
        # Persist all three recovery directory entries before the database can
        # commit. A host crash after COMMIT must still leave the preimage,
        # rollback SQL, and pending marker discoverable.
        fsync_directory(preimage_path.parent)
        result = run_psql(db_url, mutation_sql(before, commit=True))
        after = run_psql(db_url, state_sql())
        validate_state(after, repaired=True)
        postimage = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "db_target_sha256": EXPECTED_DB_TARGET_SHA256,
            "repair": "colliers-derived-psf-v1",
            "state": after,
            "updated_at_disposition": "advanced_by_table_trigger",
        }
        postimage_sha256 = replace_private_bytes(
            postimage_path, private_json_bytes(postimage)
        )
        result.update(
            {
                "preimage": str(preimage_path),
                "preimage_sha256": sha256,
                "postimage": str(postimage_path),
                "postimage_sha256": postimage_sha256,
                "rollback_sql": str(rollback_sql_path),
                "rollback_sql_sha256": rollback_sql_sha256,
                "updated_at_disposition": "advanced_by_table_trigger",
            }
        )
        print(json.dumps(result, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
