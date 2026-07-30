#!/usr/bin/env python3
"""Clear the two verified SVN Buildout missing-listing shell bodies fail-closed.

The detail page is not inventory authority. A successful HTTP response that
contains Buildout's missing-listing shell must never replace useful listing
Markdown. This one-time, SVN-only repair clears only an exact known shell in
``markdown`` and/or the root ``raw_data.markdown`` key on active SVN rows.

The default mode is read-only. Persistent apply requires the reviewed database
target, exact preflight count and digest, the canonical CRE lock, and a new
owner-only preimage. Rollback requires the exact preimage SHA-256 and refuses
if row ownership, lifecycle state, or either repaired field has drifted.
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
from uuid import UUID

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


EXPECTED_DB_TARGET_SHA256 = (
    "faf5d034d1f085ce09dd7afd0cc013dcbf474a81a73dc60fafa6c8884bfdf9ee"
)
ADVISORY_LOCK_KEY = 734_251_907_300_731_001
SUPPORTED_SOURCE = "svn"
EXACT_SHELL_MD5 = (
    "693210790ca2796fd505e7bea830d501",
    "e302508e4f976b50be2d4c13acfc6663",
)
MAX_PREIMAGE_BYTES = 256 * 1024 * 1024


def validate_source(source: str) -> str:
    value = source.strip()
    if value != SUPPORTED_SOURCE:
        raise ValueError("this bounded repair supports only source 'svn'")
    return value


def exact_shell_predicate(expression: str) -> str:
    hashes = ", ".join(sql_lit(value) for value in EXACT_SHELL_MD5)
    return f"md5(coalesce({expression}, '')) IN ({hashes})"


def broad_shell_predicate(expression: str) -> str:
    lowered = f"lower(coalesce({expression}, ''))"
    return (
        f"({lowered} LIKE '%listing_not_found%'"
        f" OR {lowered} LIKE '%listing not found%'"
        f" OR {lowered} LIKE '%sorry, we can''t find the listing%'"
        f" OR {lowered} LIKE '%sorry, we can&#39;t find the listing%')"
    )


def active_svn_where(alias: str = "l", brokerage_alias: str = "b") -> str:
    return f"""
{alias}.deleted_at IS NULL
AND {alias}.status = 'active'
AND {brokerage_alias}.slug = 'svn'
AND ({SOURCE_KEY_SQL}) = 'svn'
"""


def exact_scope_where(alias: str = "l", brokerage_alias: str = "b") -> str:
    return f"""
{active_svn_where(alias, brokerage_alias)}
AND (
  {exact_shell_predicate(f"{alias}.markdown")}
  OR {exact_shell_predicate(f"{alias}.raw_data->>'markdown'")}
)
"""


def digest_sql(alias: str) -> str:
    return f"""md5(coalesce(string_agg(
  {alias}.id::text || chr(31)
  || {alias}.brokerage_id::text || chr(31)
  || coalesce({alias}.status, '') || chr(31)
  || coalesce({alias}.deleted_at::text, '') || chr(31)
  || coalesce({alias}.markdown, '') || chr(31)
  || coalesce({alias}.raw_data::text, 'null')
  , '' ORDER BY {alias}.id
), ''))"""


def preflight_sql(*, include_rows: bool = False) -> str:
    rows = (
        """,
  'rows', coalesce((
    SELECT jsonb_agg(
      jsonb_build_object(
        'id', s.id,
        'brokerage_id', s.brokerage_id,
        'status', s.status,
        'deleted_at', s.deleted_at,
        'markdown', s.markdown,
        'raw_data', s.raw_data,
        'raw_data_is_sql_null', s.raw_data IS NULL
      ) ORDER BY s.id
    )
    FROM scope s
  ), '[]'::jsonb)"""
        if include_rows
        else ""
    )
    return f"""
BEGIN READ ONLY;
WITH base AS (
  SELECT
    l.id,
    l.brokerage_id,
    l.status,
    l.deleted_at,
    l.markdown,
    l.raw_data
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE {active_svn_where()}
),
scope AS (
  SELECT *
  FROM base l
  WHERE {exact_shell_predicate("l.markdown")}
     OR {exact_shell_predicate("l.raw_data->>'markdown'")}
)
SELECT jsonb_build_object(
  'source', 'svn',
  'count', (SELECT count(*) FROM scope),
  'digest', (SELECT {digest_sql("s")} FROM scope s),
  'column_shell_count', (
    SELECT count(*) FROM scope s
    WHERE {exact_shell_predicate("s.markdown")}
  ),
  'root_shell_count', (
    SELECT count(*) FROM scope s
    WHERE {exact_shell_predicate("s.raw_data->>'markdown'")}
  ),
  'broad_marker_count', (
    SELECT count(*) FROM base l
    WHERE {broad_shell_predicate("l.markdown")}
       OR {broad_shell_predicate("l.raw_data->>'markdown'")}
  ),
  'unexpected_marker_count', (
    SELECT
      count(*) FILTER (
        WHERE {broad_shell_predicate("l.markdown")}
          AND NOT {exact_shell_predicate("l.markdown")}
      )
      + count(*) FILTER (
        WHERE {broad_shell_predicate("l.raw_data->>'markdown'")}
          AND NOT {exact_shell_predicate("l.raw_data->>'markdown'")}
      )
    FROM base l
  ),
  'nested_marker_count', (
    SELECT count(*) FROM base l
    WHERE {broad_shell_predicate("(l.raw_data - 'markdown')::text")}
  ),
  'invalid_raw_data_count', (
    SELECT count(*) FROM scope s
    WHERE s.raw_data IS NOT NULL
      AND jsonb_typeof(s.raw_data) <> 'object'
  )
  {rows}
)::text;
ROLLBACK;
"""


def validate_audit_report(report: dict) -> None:
    if report.get("source") != SUPPORTED_SOURCE:
        raise ValueError("preflight source mismatch")
    numeric = (
        "count",
        "column_shell_count",
        "root_shell_count",
        "broad_marker_count",
        "unexpected_marker_count",
        "nested_marker_count",
        "invalid_raw_data_count",
    )
    if any(not isinstance(report.get(key), int) for key in numeric):
        raise ValueError("preflight audit counts are invalid")
    if report["unexpected_marker_count"] != 0:
        raise RuntimeError("unrecognized SVN shell variant found; repair refused")
    if report["nested_marker_count"] != 0:
        raise RuntimeError("SVN shell marker outside raw_data.markdown; repair refused")
    if report["invalid_raw_data_count"] != 0:
        raise RuntimeError("SVN shell row has non-object raw_data; repair refused")
    if report["broad_marker_count"] != report["count"]:
        raise RuntimeError("broad and exact SVN shell scopes disagree; repair refused")
    if report["count"] < 0:
        raise ValueError("preflight count is invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", str(report.get("digest", ""))):
        raise ValueError("preflight digest is invalid")


def transaction_audit_gate_sql() -> str:
    return f"""
DO $audit$
DECLARE
  exact_count bigint;
  broad_count bigint;
  unexpected_count bigint;
  nested_count bigint;
  invalid_raw_data_count bigint;
BEGIN
  SELECT
    count(*) FILTER (WHERE
      {exact_shell_predicate("l.markdown")}
      OR {exact_shell_predicate("l.raw_data->>'markdown'")}
    ),
    count(*) FILTER (WHERE
      {broad_shell_predicate("l.markdown")}
      OR {broad_shell_predicate("l.raw_data->>'markdown'")}
    ),
    count(*) FILTER (
      WHERE {broad_shell_predicate("l.markdown")}
        AND NOT {exact_shell_predicate("l.markdown")}
    )
    + count(*) FILTER (
      WHERE {broad_shell_predicate("l.raw_data->>'markdown'")}
        AND NOT {exact_shell_predicate("l.raw_data->>'markdown'")}
    ),
    count(*) FILTER (WHERE
      {broad_shell_predicate("(l.raw_data - 'markdown')::text")}
    ),
    count(*) FILTER (WHERE (
      {exact_shell_predicate("l.markdown")}
      OR {exact_shell_predicate("l.raw_data->>'markdown'")}
    ) AND l.raw_data IS NOT NULL
      AND jsonb_typeof(l.raw_data) <> 'object'
    )
  INTO
    exact_count,
    broad_count,
    unexpected_count,
    nested_count,
    invalid_raw_data_count
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE {active_svn_where()};

  IF broad_count <> exact_count
     OR unexpected_count <> 0
     OR nested_count <> 0
     OR invalid_raw_data_count <> 0 THEN
    RAISE EXCEPTION
      'SVN shell audit failed: exact %, broad %, unexpected %, nested %, invalid raw_data %',
      exact_count,
      broad_count,
      unexpected_count,
      nested_count,
      invalid_raw_data_count;
  END IF;
END
$audit$;
"""


def apply_sql(
    expected_count: int,
    expected_digest: str,
    *,
    commit: bool,
) -> str:
    if expected_count < 1:
        raise ValueError("expected count must be positive")
    if not re.fullmatch(r"[0-9a-f]{32}", expected_digest):
        raise ValueError("expected digest must be lowercase MD5")
    terminal = "COMMIT;" if commit else "ROLLBACK;"
    mode = "applied" if commit else "verify_apply_rollback"
    return f"""
\\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '2min';
SET LOCAL standard_conforming_strings = on;
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});

{transaction_audit_gate_sql()}

CREATE TEMP TABLE _shell_scope (
  id uuid PRIMARY KEY,
  brokerage_id uuid NOT NULL,
  status text NOT NULL,
  deleted_at timestamptz,
  markdown text,
  raw_data jsonb
) ON COMMIT DROP;

INSERT INTO _shell_scope
SELECT
  l.id,
  l.brokerage_id,
  l.status,
  l.deleted_at,
  l.markdown,
  l.raw_data
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
WHERE {exact_scope_where()};

SELECT 1
FROM credeals.cre_listings l
JOIN _shell_scope s ON s.id = l.id
FOR UPDATE;

DO $gate$
DECLARE
  actual_count bigint;
  actual_digest text;
BEGIN
  SELECT count(*), {digest_sql("s")}
  INTO actual_count, actual_digest
  FROM _shell_scope s;
  IF actual_count <> {expected_count} OR actual_digest <> {sql_lit(expected_digest)} THEN
    RAISE EXCEPTION
      'SVN shell scope drifted: expected count/digest %/%, got %/%',
      {expected_count}, {sql_lit(expected_digest)}, actual_count, actual_digest;
  END IF;
END
$gate$;

CREATE TEMP TABLE _updated_ids (
  id uuid PRIMARY KEY
) ON COMMIT DROP;

WITH updated AS (
  UPDATE credeals.cre_listings l
  SET
    markdown = CASE
      WHEN {exact_shell_predicate("l.markdown")} THEN NULL
      ELSE l.markdown
    END,
    raw_data = CASE
      WHEN {exact_shell_predicate("l.raw_data->>'markdown'")}
        THEN l.raw_data - 'markdown'
      ELSE l.raw_data
    END
  FROM _shell_scope s
  WHERE l.id = s.id
  RETURNING l.id
)
INSERT INTO _updated_ids
SELECT id FROM updated;

DO $post$
DECLARE
  updated_count bigint;
  mismatches bigint;
  remaining bigint;
  remaining_broad bigint;
  remaining_nested bigint;
BEGIN
  SELECT count(*) INTO updated_count FROM _updated_ids;
  SELECT count(*) INTO mismatches
  FROM _shell_scope s
  JOIN credeals.cre_listings l ON l.id = s.id
  WHERE l.brokerage_id IS DISTINCT FROM s.brokerage_id
     OR l.status IS DISTINCT FROM s.status
     OR l.deleted_at IS DISTINCT FROM s.deleted_at
     OR l.markdown IS DISTINCT FROM CASE
          WHEN {exact_shell_predicate("s.markdown")} THEN NULL ELSE s.markdown END
     OR l.raw_data IS DISTINCT FROM CASE
          WHEN {exact_shell_predicate("s.raw_data->>'markdown'")}
            THEN s.raw_data - 'markdown'
          ELSE s.raw_data
        END;
  SELECT count(*) INTO remaining
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE {exact_scope_where()};
  SELECT count(*) INTO remaining_broad
  FROM credeals.cre_listings l
  JOIN _shell_scope s ON s.id = l.id
  WHERE {broad_shell_predicate("l.markdown")}
     OR {broad_shell_predicate("l.raw_data->>'markdown'")};
  SELECT count(*) INTO remaining_nested
  FROM credeals.cre_listings l
  JOIN _shell_scope s ON s.id = l.id
  WHERE {broad_shell_predicate("(l.raw_data - 'markdown')::text")};
  IF updated_count <> {expected_count}
     OR mismatches <> 0
     OR remaining <> 0
     OR remaining_broad <> 0
     OR remaining_nested <> 0 THEN
    RAISE EXCEPTION
      'SVN shell repair readback failed: updated %, mismatches %, exact %, broad %, nested %',
      updated_count,
      mismatches,
      remaining,
      remaining_broad,
      remaining_nested;
  END IF;
END
$post$;

SELECT jsonb_build_object(
  'ok', true,
  'mode', {sql_lit(mode)},
  'source', 'svn',
  'repaired', (SELECT count(*) FROM _updated_ids),
  'updatedAtDisposition', 'advanced_by_table_trigger'
)::text;
{terminal}
"""


def rollback_sql(preimage: dict) -> str:
    validate_preimage(preimage)
    payload = sql_lit(json.dumps(preimage, separators=(",", ":")))
    return f"""
\\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '2min';
SET LOCAL standard_conforming_strings = on;
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});

CREATE TEMP TABLE _preimage (
  id uuid PRIMARY KEY,
  brokerage_id uuid NOT NULL,
  status text NOT NULL,
  deleted_at timestamptz,
  markdown text,
  raw_data jsonb
) ON COMMIT DROP;

INSERT INTO _preimage
SELECT
  id::uuid,
  brokerage_id::uuid,
  status,
  deleted_at,
  markdown,
  CASE WHEN raw_data_is_sql_null THEN NULL::jsonb ELSE raw_data END
FROM jsonb_to_recordset(({payload}::jsonb)->'rows')
  AS x(
    id text,
    brokerage_id text,
    status text,
    deleted_at timestamptz,
    markdown text,
    raw_data jsonb,
    raw_data_is_sql_null boolean
  );

SELECT 1
FROM credeals.cre_listings l
JOIN _preimage p ON p.id = l.id
FOR UPDATE;

DO $gate$
DECLARE
  actual_count bigint;
  actual_digest text;
  invalid_preimage bigint;
  missing bigint;
  drifted bigint;
BEGIN
  SELECT count(*), {digest_sql("p")}
  INTO actual_count, actual_digest
  FROM _preimage p;
  SELECT count(*) INTO invalid_preimage
  FROM _preimage p
  WHERE NOT (
    {exact_shell_predicate("p.markdown")}
    OR {exact_shell_predicate("p.raw_data->>'markdown'")}
  );
  SELECT count(*) INTO missing
  FROM _preimage p
  WHERE NOT EXISTS (
    SELECT 1 FROM credeals.cre_listings l WHERE l.id = p.id
  );
  SELECT count(*) INTO drifted
  FROM _preimage p
  JOIN credeals.cre_listings l ON l.id = p.id
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  WHERE b.slug <> 'svn'
     OR ({SOURCE_KEY_SQL}) <> 'svn'
     OR l.brokerage_id IS DISTINCT FROM p.brokerage_id
     OR l.status IS DISTINCT FROM p.status
     OR l.deleted_at IS DISTINCT FROM p.deleted_at
     OR l.markdown IS DISTINCT FROM CASE
          WHEN {exact_shell_predicate("p.markdown")} THEN NULL ELSE p.markdown END
     OR l.raw_data IS DISTINCT FROM CASE
          WHEN {exact_shell_predicate("p.raw_data->>'markdown'")}
            THEN p.raw_data - 'markdown'
          ELSE p.raw_data
        END;
  IF actual_count <> {int(preimage["count"])}
     OR actual_digest <> {sql_lit(preimage["digest"])}
     OR invalid_preimage <> 0
     OR missing <> 0
     OR drifted <> 0 THEN
    RAISE EXCEPTION
      'SVN shell rollback refused: count/digest %/%, invalid %, missing %, drifted %',
      actual_count, actual_digest, invalid_preimage, missing, drifted;
  END IF;
END
$gate$;

CREATE TEMP TABLE _restored_ids (
  id uuid PRIMARY KEY
) ON COMMIT DROP;

WITH restored AS (
  UPDATE credeals.cre_listings l
  SET markdown = p.markdown, raw_data = p.raw_data
  FROM _preimage p
  WHERE l.id = p.id
  RETURNING l.id
)
INSERT INTO _restored_ids
SELECT id FROM restored;

DO $post$
DECLARE
  mismatches bigint;
  restored_count bigint;
BEGIN
  SELECT count(*) INTO restored_count FROM _restored_ids;
  SELECT count(*) INTO mismatches
  FROM _preimage p
  JOIN credeals.cre_listings l ON l.id = p.id
  WHERE l.brokerage_id IS DISTINCT FROM p.brokerage_id
     OR l.status IS DISTINCT FROM p.status
     OR l.deleted_at IS DISTINCT FROM p.deleted_at
     OR l.markdown IS DISTINCT FROM p.markdown
     OR l.raw_data IS DISTINCT FROM p.raw_data;
  IF restored_count <> {int(preimage["count"])} OR mismatches <> 0 THEN
    RAISE EXCEPTION
      'SVN shell rollback readback failed: restored %, mismatches %',
      restored_count, mismatches;
  END IF;
END
$post$;

SELECT jsonb_build_object(
  'ok', true,
  'mode', 'rollback_applied',
  'source', 'svn',
  'restored', (SELECT count(*) FROM _restored_ids),
  'updatedAtDisposition', 'advanced_by_table_trigger'
)::text;
COMMIT;
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
    )
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        raise RuntimeError(f"psql exited {proc.returncode}")
    candidates = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not candidates:
        raise RuntimeError("psql returned no JSON result")
    try:
        return json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("psql did not return the expected JSON result") from exc


def _require_private_parent(parent: Path) -> None:
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    parent_stat = parent.stat()
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("preimage parent must be a directory")
    if parent_stat.st_uid != os.getuid():
        raise ValueError("preimage parent must be owned by the current user")
    if parent_stat.st_mode & 0o077:
        raise ValueError("preimage parent must be owner-only")


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("path must not contain symlink components")


def private_json_bytes(payload: dict) -> bytes:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_PREIMAGE_BYTES:
        raise ValueError("preimage exceeds size limit")
    return encoded


def atomic_private_json(path: Path, payload: dict) -> str:
    if not path.is_absolute():
        raise ValueError("preimage path must be absolute")
    _reject_symlink_components(path)
    encoded = private_json_bytes(payload)
    parent = path.parent
    _require_private_parent(parent)
    if path.exists() or path.is_symlink():
        raise FileExistsError("preimage path already exists; refusing overwrite")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    complete = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        complete = True
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        dir_fd = os.open(parent, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if not complete:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return hashlib.sha256(encoded).hexdigest()


def validate_preimage(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("preimage must be an object")
    if payload.get("db_target_sha256") != EXPECTED_DB_TARGET_SHA256:
        raise ValueError("preimage database target does not match")
    if payload.get("source") != SUPPORTED_SOURCE:
        raise ValueError("preimage source must be svn")
    if not isinstance(payload.get("captured_at"), str):
        raise ValueError("preimage capture time is missing")
    rows = payload.get("rows")
    if not isinstance(payload.get("count"), int):
        raise ValueError("preimage count is invalid")
    if not isinstance(rows, list) or len(rows) != payload["count"]:
        raise ValueError("preimage row count does not match")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("preimage row is invalid")
        try:
            ids.append(str(UUID(str(row["id"]))))
            UUID(str(row["brokerage_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("preimage row identity is invalid") from exc
        if row.get("status") != "active" or row.get("deleted_at") is not None:
            raise ValueError("preimage row lifecycle is outside repair scope")
        if "markdown" not in row or "raw_data" not in row:
            raise ValueError("preimage row is missing owned fields")
        raw_data_is_sql_null = row.get("raw_data_is_sql_null")
        if not isinstance(raw_data_is_sql_null, bool):
            raise ValueError("preimage row raw_data discriminator is invalid")
        if raw_data_is_sql_null:
            if row["raw_data"] is not None:
                raise ValueError("SQL-null raw_data discriminator is inconsistent")
        elif not isinstance(row["raw_data"], dict):
            raise ValueError("preimage raw_data must be an object or SQL null")
    if len(set(ids)) != len(ids):
        raise ValueError("preimage identities are duplicated")
    if not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("digest", ""))):
        raise ValueError("preimage digest is invalid")


def load_private_preimage(path: Path, expected_sha256: str) -> tuple[dict, str]:
    if not path.is_absolute():
        raise ValueError("rollback preimage path must be absolute")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected preimage SHA-256 must be lowercase hex")
    _reject_symlink_components(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("rollback preimage must be a regular file")
        if file_stat.st_uid != os.getuid():
            raise ValueError("rollback preimage must be owned by current user")
        if file_stat.st_mode & 0o077:
            raise ValueError("rollback preimage must be owner-only")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PREIMAGE_BYTES:
                raise ValueError("rollback preimage exceeds size limit")
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("rollback preimage SHA-256 does not match")
    payload = json.loads(raw)
    validate_preimage(payload)
    return payload, actual_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SUPPORTED_SOURCE)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-digest")
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-apply-rollback", action="store_true")
    parser.add_argument("--rollback-preimage", type=Path)
    parser.add_argument("--expected-preimage-sha256")
    args = parser.parse_args(argv)
    selected = sum(
        bool(value)
        for value in (
            args.apply,
            args.verify_apply_rollback,
            args.rollback_preimage,
        )
    )
    if selected > 1:
        parser.error("mutation and verification modes are mutually exclusive")
    validate_source(args.source)
    db_url, env_path = load_db_url(args.env_file)
    assert_expected_database_target(db_url, EXPECTED_DB_TARGET_SHA256)

    lock_path = canonical_shared_lock_dir()
    with SharedLock(lock_path):
        if args.rollback_preimage:
            if not args.expected_preimage_sha256:
                parser.error(
                    "--rollback-preimage requires --expected-preimage-sha256"
                )
            preimage, preimage_sha256 = load_private_preimage(
                args.rollback_preimage,
                args.expected_preimage_sha256,
            )
            result = run_psql(db_url, rollback_sql(preimage))
            result.update(
                {
                    "preimage": str(args.rollback_preimage),
                    "preimage_sha256": preimage_sha256,
                    "lock": str(lock_path),
                }
            )
            print(json.dumps(result, sort_keys=True))
            return 0

        if not selected:
            report = run_psql(db_url, preflight_sql())
            validate_audit_report(report)
            report.update(
                {
                    "mode": "preflight",
                    "env_file": env_path,
                    "lock": str(lock_path),
                }
            )
            print(json.dumps(report, sort_keys=True))
            return 0

        if args.expected_count is None or args.expected_digest is None:
            parser.error(
                "--apply/--verify-apply-rollback require "
                "--expected-count and --expected-digest"
            )
        if args.apply and args.preimage is None:
            parser.error("--apply requires --preimage")
        if args.preimage is not None and not args.apply:
            parser.error("--preimage is valid only with --apply")
        if args.expected_preimage_sha256:
            parser.error(
                "--expected-preimage-sha256 is valid only with "
                "--rollback-preimage"
            )

        captured = run_psql(db_url, preflight_sql(include_rows=True))
        validate_audit_report(captured)
        if (
            captured.get("count") != args.expected_count
            or captured.get("digest") != args.expected_digest
        ):
            raise RuntimeError("preimage scope does not match expected count/digest")
        captured["db_target_sha256"] = EXPECTED_DB_TARGET_SHA256
        captured["captured_at"] = datetime.now(timezone.utc).isoformat()
        validate_preimage(captured)
        preimage_sha256 = None
        if args.apply:
            preimage_sha256 = atomic_private_json(args.preimage, captured)
        result = run_psql(
            db_url,
            apply_sql(
                args.expected_count,
                args.expected_digest,
                commit=args.apply,
            ),
        )
        result["lock"] = str(lock_path)
        if args.apply:
            result.update(
                {
                    "preimage": str(args.preimage),
                    "preimage_sha256": preimage_sha256,
                }
            )
        print(json.dumps(result, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
