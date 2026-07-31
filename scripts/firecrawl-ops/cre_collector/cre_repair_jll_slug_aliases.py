#!/usr/bin/env python3
"""Retire only the verified stale JLL slug aliases, fail closed.

JLL changed its public listing identity from URL slugs to numeric provider IDs.
The July 31 full-source checkpoint proved 135 active, childless legacy aliases
whose URL is now represented by a current numeric JLL record.  This is a
one-time *supersede* repair, not a merge: it never changes the numeric
survivor, moves children, deletes a row, or touches JLL Investor.  Default
mode is read-only.  Apply requires an artifact-pinned plan, the canonical CRE
lock, an owner-only immutable preimage, and a serializable transaction.
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

EXPECTED_ARTIFACT_SHA256 = (
    "ce8d3891a4efadf3583cafae2c2a0044b78650bd2fbcde5db08bd57b270f5f96"
)
EXPECTED_DB_TARGET_SHA256 = (
    "faf5d034d1f085ce09dd7afd0cc013dcbf474a81a73dc60fafa6c8884bfdf9ee"
)
EXPECTED_GENERATION = "2026-07-31T121327Z"
EXPECTED_ARTIFACT_ROWS = 11_003
EXPECTED_ARTIFACT_TARGETS = 10_411
EXPECTED_PAIRS = 135
ADVISORY_LOCK_KEY = 734_251_907_300_731_002
SOURCE_KEY = "jll"
REPAIR_TOKEN = "jll-slug-aliases-v1-20260731"
MAX_PREIMAGE_BYTES = 32 * 1024 * 1024
EXPECTED_FK_SURFACES = {
    "credeals.cre_listing_contacts",
    "credeals.cre_listing_documents",
    "credeals.cre_listing_events",
    "credeals.cre_listing_images",
    "credeals.cre_listing_links",
    "credeals.cre_listing_media",
    "credeals.cre_listing_om_facts",
    "credeals.cre_listing_price_history",
    "credeals.cre_scrape_log",
}
EXPECTED_SOFT_REFERENCE_SURFACES = {
    "credeals.cre_listing_contacts_archive",
    "credeals.cre_listing_documents_archive",
    "credeals.cre_listing_links_archive",
    "credeals.cre_listing_media_archive",
    "credeals.cre_listing_om_facts_archive",
}
REFERENCE_TABLES = (
    "cre_listing_contacts",
    "cre_listing_documents",
    "cre_listing_events",
    "cre_listing_images",
    "cre_listing_links",
    "cre_listing_media",
    "cre_listing_om_facts",
    "cre_listing_price_history",
    "cre_scrape_log",
)
SOFT_REFERENCE_TABLES = (
    "cre_listing_contacts_archive",
    "cre_listing_documents_archive",
    "cre_listing_links_archive",
    "cre_listing_media_archive",
    "cre_listing_om_facts_archive",
)
EXPECTED_REFERENCE_KEYS = {
    "sourceIndex",
    "queue",
    *REFERENCE_TABLES,
    *SOFT_REFERENCE_TABLES,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generation_expr(alias: str) -> str:
    return f"""COALESCE(
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{secondary_pass,freshnessProvenance,generationId}}','')
    )"""


def normalize_jll_url(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("JLL artifact URL is not a string")
    value = value.strip()
    if not re.fullmatch(
        r"https://property\.jll\.com/listings/[A-Za-z0-9][A-Za-z0-9._~:/?%#=&-]*", value
    ):
        raise ValueError("JLL artifact URL is outside the reviewed property host")
    return value.rstrip("/")


def load_artifact(path: Path) -> dict[str, str]:
    if sha256_file(path) != EXPECTED_ARTIFACT_SHA256:
        raise ValueError("JLL repair artifact SHA-256 does not match")
    payload = json.loads(path.read_text())
    freshness = (payload.get("runMeta") or {}).get("freshness") or {}
    if freshness.get("generationId") != EXPECTED_GENERATION:
        raise ValueError("JLL repair artifact generation does not match")
    listings = payload.get("listings")
    if not isinstance(listings, list) or len(listings) != EXPECTED_ARTIFACT_ROWS:
        raise ValueError("JLL repair artifact row count drifted")
    targets: dict[str, str] = {}
    for row in listings:
        if not isinstance(row, dict) or row.get("sourceKey") != SOURCE_KEY:
            raise ValueError("JLL repair artifact source scope drifted")
        provider_id = row.get("id")
        url = normalize_jll_url(row.get("url"))
        if not isinstance(provider_id, str) or not re.fullmatch(
            r"[1-9][0-9]*", provider_id
        ):
            raise ValueError("JLL artifact contains a nonnumeric provider ID")
        previous = targets.setdefault(url, provider_id)
        if previous != provider_id:
            raise ValueError("JLL artifact URL maps to multiple provider IDs")
    if len(targets) != EXPECTED_ARTIFACT_TARGETS:
        raise ValueError("JLL repair artifact target count drifted")
    return targets


def artifact_geometry_sha256(targets: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(targets.items()), separators=(",", ":")).encode()
    ).hexdigest()


def state_sql() -> str:
    return f"""
BEGIN READ ONLY;
SET LOCAL statement_timeout='2min';
WITH jll AS (
  SELECT l.id,l.brokerage_id,l.external_id,l.source_url,l.status,l.deleted_at,
         l.updated_at,l.raw_data,{generation_expr("l")} AS generation,to_jsonb(l) AS parent
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
  WHERE b.slug='jll' AND ({SOURCE_KEY_SQL})={sql_lit(SOURCE_KEY)}
    AND l.deleted_at IS NULL AND l.status='active'
    AND l.source_url ~* '^https://property\\.jll\\.com/listings/'
), duplicate_urls AS (
  SELECT source_url FROM jll GROUP BY source_url HAVING count(*) > 1
)
SELECT jsonb_build_object(
  'rows',coalesce((SELECT jsonb_agg(jsonb_build_object(
    'id',j.id,'brokerage_id',j.brokerage_id,'external_id',j.external_id,
    'source_url',j.source_url,'status',j.status,'deleted_at',j.deleted_at,
    'updated_at',j.updated_at,'raw_data',j.raw_data,'generation',j.generation,
    'references',{reference_counts_sql("j")},
    'parent',j.parent) ORDER BY j.source_url,j.id) FROM jll j
    JOIN duplicate_urls d USING(source_url)),'[]'::jsonb),
  'duplicate_url_groups',(SELECT count(*) FROM duplicate_urls),
  'fk_surfaces',coalesce((SELECT jsonb_agg(DISTINCT n.nspname||'.'||c.relname ORDER BY n.nspname||'.'||c.relname)
    FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE k.contype='f' AND k.confrelid='credeals.cre_listings'::regclass),'[]'::jsonb),
  'soft_reference_surfaces',coalesce((SELECT jsonb_agg(n.nspname||'.'||c.relname ORDER BY n.nspname||'.'||c.relname)
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='credeals' AND a.attname='source_listing_id' AND a.attnum>0 AND NOT a.attisdropped),'[]'::jsonb)
)::text;
ROLLBACK;
"""


def _validate_uuid(value: object, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"JLL state {field} is not a UUID") from exc


def build_plan(state: dict, targets: dict[str, str]) -> list[dict]:
    if not isinstance(state, dict) or not isinstance(state.get("rows"), list):
        raise TypeError("JLL state result is malformed")
    if set(state.get("fk_surfaces") or []) != EXPECTED_FK_SURFACES:
        raise ValueError("JLL FK surface drifted; repair refused")
    if (
        set(state.get("soft_reference_surfaces") or [])
        != EXPECTED_SOFT_REFERENCE_SURFACES
    ):
        raise ValueError("JLL soft-reference surface drifted; repair refused")
    rows_by_url: dict[str, list[dict]] = {}
    for raw in state["rows"]:
        if not isinstance(raw, dict):
            raise TypeError("JLL state row is malformed")
        url = normalize_jll_url(raw.get("source_url"))
        _validate_uuid(raw.get("id"), "id")
        _validate_uuid(raw.get("brokerage_id"), "brokerage_id")
        if raw.get("status") != "active" or raw.get("deleted_at") is not None:
            raise ValueError("JLL candidate is not active")
        if not isinstance(raw.get("raw_data"), dict):
            raise TypeError("JLL candidate raw_data is not an object")
        references = raw.get("references")
        if (
            not isinstance(references, dict)
            or set(references) != EXPECTED_REFERENCE_KEYS
            or any(
                not isinstance(value, int) or value != 0
                for value in references.values()
            )
        ):
            raise ValueError("JLL candidate has dependent references")
        rows_by_url.setdefault(url, []).append(raw)
    if int(state.get("duplicate_url_groups", -1)) != len(rows_by_url):
        raise ValueError("JLL duplicate URL scope is incomplete")
    plan: list[dict] = []
    for url, rows in sorted(rows_by_url.items()):
        numeric_id = targets.get(url)
        if numeric_id is None or len(rows) != 2:
            raise ValueError("JLL duplicate URL is outside the reviewed artifact plan")
        survivors = [r for r in rows if r.get("external_id") == numeric_id]
        aliases = [r for r in rows if r.get("external_id") != numeric_id]
        if len(survivors) != 1 or len(aliases) != 1:
            raise ValueError("JLL duplicate identity shape is unsafe")
        survivor, alias = survivors[0], aliases[0]
        if survivor.get("generation") != EXPECTED_GENERATION:
            raise ValueError("JLL numeric survivor is not from the reviewed generation")
        if alias.get("external_id", "").startswith("jll-superseded:"):
            raise ValueError("JLL alias appears already repaired; state drifted")
        plan.append(
            {
                "legacy_id": _validate_uuid(alias["id"], "legacy id"),
                "legacy_brokerage_id": _validate_uuid(
                    alias["brokerage_id"], "legacy brokerage id"
                ),
                "legacy_external_id": alias.get("external_id"),
                "numeric_id": _validate_uuid(survivor["id"], "numeric id"),
                "numeric_brokerage_id": _validate_uuid(
                    survivor["brokerage_id"], "numeric brokerage id"
                ),
                "numeric_external_id": numeric_id,
                "numeric_generation": survivor["generation"],
                "source_url": url,
                "legacy_parent": alias["parent"],
                "legacy_raw_data": alias["raw_data"],
                "legacy_status": alias["status"],
                "legacy_deleted_at": alias["deleted_at"],
                "legacy_updated_at": alias["updated_at"],
            }
        )
    if len(plan) != EXPECTED_PAIRS:
        raise ValueError(
            f"JLL plan pair count drifted: expected {EXPECTED_PAIRS}, got {len(plan)}"
        )
    return plan


def plan_sha256(plan: list[dict]) -> str:
    geometry = [
        (
            p["legacy_id"],
            p["legacy_brokerage_id"],
            p["legacy_external_id"],
            p["numeric_id"],
            p["numeric_brokerage_id"],
            p["numeric_external_id"],
            p["numeric_generation"],
            p["source_url"],
        )
        for p in plan
    ]
    return hashlib.sha256(
        json.dumps(geometry, separators=(",", ":")).encode()
    ).hexdigest()


def reference_counts_sql(alias: str) -> str:
    parts = [
        f"'sourceIndex',(SELECT count(*) FROM credeals.cre_source_index si WHERE si.brokerage_id={alias}.brokerage_id AND si.external_id={alias}.external_id)",
        f"'queue',(SELECT count(*) FROM credeals.cre_enrichment_queue q WHERE q.brokerage_id={alias}.brokerage_id AND q.external_id={alias}.external_id)",
    ]
    for table in REFERENCE_TABLES:
        parts.append(
            f"'{table}',(SELECT count(*) FROM credeals.{table} x WHERE x.listing_id={alias}.id)"
        )
    for table in SOFT_REFERENCE_TABLES:
        parts.append(
            f"'{table}',(SELECT count(*) FROM credeals.{table} x WHERE x.source_listing_id={alias}.id)"
        )
    return "jsonb_build_object(" + ",".join(parts) + ")"


def preimage_from_plan(plan: list[dict], *, artifact_geometry: str) -> dict:
    return {
        "schema_version": 1,
        "source": SOURCE_KEY,
        "repair_token": REPAIR_TOKEN,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "db_target_sha256": EXPECTED_DB_TARGET_SHA256,
        "artifact_sha256": EXPECTED_ARTIFACT_SHA256,
        "artifact_geometry_sha256": artifact_geometry,
        "generation": EXPECTED_GENERATION,
        "count": len(plan),
        "plan_sha256": plan_sha256(plan),
        "rows": plan,
    }


def validate_preimage(payload: dict) -> None:
    required = {
        "schema_version",
        "source",
        "repair_token",
        "captured_at",
        "db_target_sha256",
        "artifact_sha256",
        "artifact_geometry_sha256",
        "generation",
        "count",
        "plan_sha256",
        "rows",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("JLL preimage schema is invalid")
    if (
        payload["schema_version"],
        payload["source"],
        payload["repair_token"],
        payload["db_target_sha256"],
        payload["artifact_sha256"],
        payload["generation"],
        payload["count"],
    ) != (
        1,
        SOURCE_KEY,
        REPAIR_TOKEN,
        EXPECTED_DB_TARGET_SHA256,
        EXPECTED_ARTIFACT_SHA256,
        EXPECTED_GENERATION,
        EXPECTED_PAIRS,
    ):
        raise ValueError("JLL preimage reviewed identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload["artifact_geometry_sha256"])):
        raise ValueError("JLL preimage artifact geometry is invalid")
    rows = payload["rows"]
    if (
        not isinstance(rows, list)
        or len(rows) != EXPECTED_PAIRS
        or plan_sha256(rows) != payload["plan_sha256"]
    ):
        raise ValueError("JLL preimage plan digest is invalid")
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("legacy_parent"), dict)
            or not isinstance(row.get("legacy_raw_data"), dict)
        ):
            raise TypeError("JLL preimage parent snapshot is incomplete")
        _validate_uuid(row.get("legacy_id"), "preimage legacy id")
        _validate_uuid(row.get("legacy_brokerage_id"), "preimage legacy brokerage id")
        _validate_uuid(row.get("numeric_id"), "preimage numeric id")
        _validate_uuid(row.get("numeric_brokerage_id"), "preimage numeric brokerage id")
        if not isinstance(row.get("legacy_external_id"), str) or not re.fullmatch(
            r"[1-9][0-9]*", str(row.get("numeric_external_id"))
        ):
            raise ValueError("JLL preimage external identity is invalid")
        if row.get("numeric_generation") != EXPECTED_GENERATION:
            raise ValueError("JLL preimage numeric generation is invalid")
        normalize_jll_url(row.get("source_url"))


def private_json_bytes(payload: dict) -> bytes:
    value = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    if len(value) > MAX_PREIMAGE_BYTES:
        raise ValueError("JLL preimage exceeds size limit")
    return value


def _reject_symlinks(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("preimage path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("preimage path must not contain symlinks")


def atomic_private_json(path: Path, payload: dict) -> str:
    _reject_symlinks(path)
    encoded = private_json_bytes(payload)
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent_stat = parent.stat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o077
    ):
        raise ValueError("preimage parent must be owner-only")
    if path.exists() or path.is_symlink():
        raise FileExistsError("preimage exists; refusing overwrite")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        dfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return hashlib.sha256(encoded).hexdigest()


def load_private_preimage(path: Path, expected_sha256: str) -> tuple[dict, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected preimage SHA-256 must be lowercase hex")
    _reject_symlinks(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > MAX_PREIMAGE_BYTES
        ):
            raise ValueError("rollback preimage is not a private regular file")
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
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError("rollback preimage SHA-256 does not match")
    payload = json.loads(raw)
    validate_preimage(payload)
    return payload, actual


def staged_plan_sql(plan: list[dict]) -> str:
    stripped = [
        {
            key: row[key]
            for key in (
                "legacy_id",
                "legacy_brokerage_id",
                "legacy_external_id",
                "numeric_id",
                "numeric_brokerage_id",
                "numeric_external_id",
                "numeric_generation",
                "source_url",
                "legacy_raw_data",
                "legacy_status",
                "legacy_deleted_at",
                "legacy_updated_at",
            )
        }
        for row in plan
    ]
    return f"""
CREATE TEMP TABLE _jll_plan(legacy_id uuid PRIMARY KEY,legacy_brokerage_id uuid NOT NULL,legacy_external_id text NOT NULL,numeric_id uuid NOT NULL,numeric_brokerage_id uuid NOT NULL,numeric_external_id text NOT NULL,numeric_generation text NOT NULL,source_url text NOT NULL UNIQUE,legacy_raw_data jsonb NOT NULL,legacy_status text NOT NULL,legacy_deleted_at timestamptz,legacy_updated_at timestamptz) ON COMMIT DROP;
INSERT INTO _jll_plan SELECT * FROM jsonb_to_recordset({sql_lit(json.dumps(stripped, separators=(",", ":")))}::jsonb) AS x(legacy_id uuid,legacy_brokerage_id uuid,legacy_external_id text,numeric_id uuid,numeric_brokerage_id uuid,numeric_external_id text,numeric_generation text,source_url text,legacy_raw_data jsonb,legacy_status text,legacy_deleted_at timestamptz,legacy_updated_at timestamptz);
"""


def mutation_sql(plan: list[dict], *, commit: bool) -> str:
    terminal = "COMMIT;" if commit else "ROLLBACK;"
    mode = "applied" if commit else "verify_apply_rollback"
    return f"""
\\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout='15s'; SET LOCAL statement_timeout='3min';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{staged_plan_sql(plan)}
SELECT 1 FROM credeals.cre_listings l JOIN _jll_plan p ON l.id IN (p.legacy_id,p.numeric_id) FOR UPDATE;
DO $guard$
DECLARE bad bigint; refs bigint; surfaces text[];
BEGIN
 SELECT array_agg(DISTINCT n.nspname||'.'||c.relname ORDER BY n.nspname||'.'||c.relname) INTO surfaces FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE k.contype='f' AND k.confrelid='credeals.cre_listings'::regclass;
 IF coalesce(surfaces,'{{}}') <> ARRAY[{",".join(sql_lit(x) for x in sorted(EXPECTED_FK_SURFACES))}] THEN RAISE EXCEPTION 'JLL FK surface drifted'; END IF;
 SELECT count(*) INTO bad FROM _jll_plan p LEFT JOIN credeals.cre_listings a ON a.id=p.legacy_id LEFT JOIN credeals.cre_listings s ON s.id=p.numeric_id LEFT JOIN credeals.cre_brokerages b ON b.id=a.brokerage_id WHERE a.id IS NULL OR s.id IS NULL OR b.id IS NULL OR a.brokerage_id<>p.legacy_brokerage_id OR s.brokerage_id<>p.numeric_brokerage_id OR a.brokerage_id<>s.brokerage_id OR b.slug<>'jll' OR ({SOURCE_KEY_SQL.replace("l.", "a.")})<>'jll' OR a.status<>'active' OR a.deleted_at IS NOT NULL OR s.status<>'active' OR s.deleted_at IS NOT NULL OR a.external_id<>p.legacy_external_id OR s.external_id<>p.numeric_external_id OR a.source_url<>p.source_url OR s.source_url<>p.source_url OR {generation_expr("s")}<>p.numeric_generation OR a.raw_data IS DISTINCT FROM p.legacy_raw_data OR a.updated_at IS DISTINCT FROM p.legacy_updated_at;
 IF bad<>0 THEN RAISE EXCEPTION 'JLL candidate snapshot drifted: %',bad; END IF;
 SELECT count(*) INTO bad FROM (SELECT l.source_url FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id JOIN _jll_plan p ON p.source_url=l.source_url WHERE b.slug='jll' AND ({SOURCE_KEY_SQL})='jll' AND l.deleted_at IS NULL AND l.status='active' GROUP BY l.source_url HAVING count(*)<>2) x;
 IF bad<>0 THEN RAISE EXCEPTION 'JLL source URL group shape drifted'; END IF;
 SELECT coalesce(sum(ref.value::bigint),0) INTO refs
 FROM credeals.cre_listings a JOIN _jll_plan p ON p.legacy_id=a.id
 CROSS JOIN LATERAL jsonb_each_text({reference_counts_sql("a")}) ref;
 IF refs<>0 THEN RAISE EXCEPTION 'JLL legacy alias has dependent references: %',refs; END IF;
END $guard$;
CREATE TEMP TABLE _jll_updated(id uuid PRIMARY KEY) ON COMMIT DROP;
WITH changed AS (UPDATE credeals.cre_listings a SET external_id='jll-superseded:v1:'||md5(a.id::text),status='inactive',deleted_at=transaction_timestamp(),raw_data=jsonb_set(a.raw_data,'{{jllIdentityRepair}}',jsonb_build_object('token',{sql_lit(REPAIR_TOKEN)},'survivorId',p.numeric_id,'survivorExternalId',p.numeric_external_id,'artifactSha256',{sql_lit(EXPECTED_ARTIFACT_SHA256)},'generation',{sql_lit(EXPECTED_GENERATION)},'disposition','superseded','appliedAt',transaction_timestamp()),true) FROM _jll_plan p WHERE a.id=p.legacy_id RETURNING a.id) INSERT INTO _jll_updated SELECT id FROM changed;
DO $post$
DECLARE n bigint; bad bigint;
BEGIN
 SELECT count(*) INTO n FROM _jll_updated; IF n<>{EXPECTED_PAIRS} THEN RAISE EXCEPTION 'JLL updated count drifted'; END IF;
 SELECT count(*) INTO bad FROM _jll_plan p JOIN credeals.cre_listings a ON a.id=p.legacy_id JOIN credeals.cre_listings s ON s.id=p.numeric_id WHERE a.status<>'inactive' OR a.deleted_at IS NULL OR a.external_id<>'jll-superseded:v1:'||md5(a.id::text) OR a.raw_data #>> '{{jllIdentityRepair,token}}'<>{sql_lit(REPAIR_TOKEN)} OR s.status<>'active' OR s.deleted_at IS NOT NULL OR s.external_id<>p.numeric_external_id OR s.source_url<>p.source_url;
 IF bad<>0 THEN RAISE EXCEPTION 'JLL postcondition drifted: %',bad; END IF;
 SELECT count(*) INTO bad FROM (SELECT l.source_url FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id JOIN _jll_plan p ON p.source_url=l.source_url WHERE b.slug='jll' AND ({SOURCE_KEY_SQL})='jll' AND l.deleted_at IS NULL AND l.status='active' GROUP BY l.source_url HAVING count(*)<>1) x;
 IF bad<>0 THEN RAISE EXCEPTION 'JLL duplicate URL remains'; END IF;
END $post$;
SELECT jsonb_build_object('ok',true,'mode',{sql_lit(mode)},'source','jll','superseded',(SELECT count(*) FROM _jll_updated),'planSha256',{sql_lit(plan_sha256(plan))})::text;
{terminal}
"""


def rollback_sql(preimage: dict) -> str:
    """Restore only this repair's aliases after exact post-state verification."""
    validate_preimage(preimage)
    plan = preimage["rows"]
    return f"""
\\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout='15s'; SET LOCAL statement_timeout='3min';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{staged_plan_sql(plan)}
SELECT 1 FROM credeals.cre_listings l JOIN _jll_plan p ON l.id IN (p.legacy_id,p.numeric_id) FOR UPDATE;
DO $guard$
DECLARE bad bigint; refs bigint; legacy_soft_refs bigint;
BEGIN
 SELECT count(*) INTO bad FROM _jll_plan p LEFT JOIN credeals.cre_listings a ON a.id=p.legacy_id LEFT JOIN credeals.cre_listings s ON s.id=p.numeric_id LEFT JOIN credeals.cre_brokerages b ON b.id=a.brokerage_id WHERE a.id IS NULL OR s.id IS NULL OR b.id IS NULL OR a.brokerage_id<>p.legacy_brokerage_id OR s.brokerage_id<>p.numeric_brokerage_id OR a.source_url<>p.source_url OR b.slug<>'jll' OR ({SOURCE_KEY_SQL.replace("l.", "a.")})<>'jll' OR a.status<>'inactive' OR a.deleted_at IS NULL OR a.external_id<>'jll-superseded:v1:'||md5(a.id::text) OR a.raw_data #>> '{{jllIdentityRepair,token}}'<>{sql_lit(REPAIR_TOKEN)} OR (a.raw_data-'jllIdentityRepair') IS DISTINCT FROM p.legacy_raw_data OR s.status<>'active' OR s.deleted_at IS NOT NULL OR s.external_id<>p.numeric_external_id OR s.source_url<>p.source_url OR {generation_expr("s")}<>p.numeric_generation;
 IF bad<>0 THEN RAISE EXCEPTION 'JLL rollback refused: post-repair state drifted: %',bad; END IF;
 SELECT coalesce(sum(ref.value::bigint),0) INTO refs FROM credeals.cre_listings a JOIN _jll_plan p ON p.legacy_id=a.id CROSS JOIN LATERAL jsonb_each_text({reference_counts_sql("a")}) ref;
 IF refs<>0 THEN RAISE EXCEPTION 'JLL rollback refused: dependent references: %',refs; END IF;
 SELECT (SELECT count(*) FROM credeals.cre_source_index si JOIN _jll_plan p ON p.legacy_brokerage_id=si.brokerage_id AND p.legacy_external_id=si.external_id) + (SELECT count(*) FROM credeals.cre_enrichment_queue q JOIN _jll_plan p ON p.legacy_brokerage_id=q.brokerage_id AND p.legacy_external_id=q.external_id) INTO legacy_soft_refs;
 IF legacy_soft_refs<>0 THEN RAISE EXCEPTION 'JLL rollback refused: legacy soft references: %',legacy_soft_refs; END IF;
END $guard$;
CREATE TEMP TABLE _jll_restored(id uuid PRIMARY KEY) ON COMMIT DROP;
WITH restored AS (UPDATE credeals.cre_listings a SET external_id=p.legacy_external_id,status=p.legacy_status,deleted_at=p.legacy_deleted_at,raw_data=p.legacy_raw_data FROM _jll_plan p WHERE a.id=p.legacy_id RETURNING a.id) INSERT INTO _jll_restored SELECT id FROM restored;
DO $post$
DECLARE restored_count bigint; bad bigint;
BEGIN
 SELECT count(*) INTO restored_count FROM _jll_restored; IF restored_count<>{EXPECTED_PAIRS} THEN RAISE EXCEPTION 'JLL rollback restored count drifted'; END IF;
 SELECT count(*) INTO bad FROM _jll_plan p JOIN credeals.cre_listings a ON a.id=p.legacy_id WHERE a.external_id IS DISTINCT FROM p.legacy_external_id OR a.status IS DISTINCT FROM p.legacy_status OR a.deleted_at IS DISTINCT FROM p.legacy_deleted_at OR a.raw_data IS DISTINCT FROM p.legacy_raw_data OR a.updated_at IS DISTINCT FROM transaction_timestamp();
 IF bad<>0 THEN RAISE EXCEPTION 'JLL rollback readback failed: %',bad; END IF;
END $post$;
SELECT jsonb_build_object('ok',true,'mode','rollback_applied','source','jll','restored',(SELECT count(*) FROM _jll_restored),'planSha256',{sql_lit(preimage["plan_sha256"])})::text;
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-apply-rollback", action="store_true")
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--rollback-preimage", type=Path)
    parser.add_argument("--expected-preimage-sha256")
    args = parser.parse_args(argv)
    if (
        sum(
            bool(x)
            for x in (args.apply, args.verify_apply_rollback, args.rollback_preimage)
        )
        > 1
    ):
        parser.error("mutation modes are mutually exclusive")
    if args.apply and not args.preimage:
        parser.error("--apply requires --preimage")
    if args.preimage and not args.apply:
        parser.error("--preimage is valid only with --apply")
    if args.rollback_preimage and not args.expected_preimage_sha256:
        parser.error("--rollback-preimage requires --expected-preimage-sha256")
    if not args.rollback_preimage and args.artifact is None:
        parser.error("--artifact is required unless --rollback-preimage is used")
    db_url, env_path = load_db_url(args.env_file)
    assert_expected_database_target(db_url, EXPECTED_DB_TARGET_SHA256)
    with SharedLock(canonical_shared_lock_dir()):
        if args.rollback_preimage:
            preimage, preimage_sha256 = load_private_preimage(
                args.rollback_preimage, args.expected_preimage_sha256
            )
            result = run_psql(db_url, rollback_sql(preimage))
            result.update(
                {
                    "preimage": str(args.rollback_preimage),
                    "preimage_sha256": preimage_sha256,
                    "lock": str(canonical_shared_lock_dir()),
                }
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        targets = load_artifact(args.artifact)
        geometry = artifact_geometry_sha256(targets)
        state = run_psql(db_url, state_sql())
        plan = build_plan(state, targets)
        if not (args.apply or args.verify_apply_rollback):
            print(
                json.dumps(
                    {
                        "ok": True,
                        "mode": "preflight",
                        "pairs": len(plan),
                        "plan_sha256": plan_sha256(plan),
                        "artifact_geometry_sha256": geometry,
                        "env_file": env_path,
                    },
                    sort_keys=True,
                )
            )
            return 0
        preimage = preimage_from_plan(plan, artifact_geometry=geometry)
        validate_preimage(preimage)
        sha = None
        if args.apply:
            sha = atomic_private_json(args.preimage, preimage)
        result = run_psql(db_url, mutation_sql(plan, commit=args.apply))
        result.update(
            {
                "preimage": str(args.preimage) if args.apply else None,
                "preimage_sha256": sha,
                "lock": str(canonical_shared_lock_dir()),
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
