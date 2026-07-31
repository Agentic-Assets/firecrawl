#!/usr/bin/env python3
"""Consolidate the bounded 2026-07-30 Cushman provider-ID split.

This is a one-time, artifact-pinned repair.  It preserves every parent UUID,
chooses one active URL-v1 survivor per normalized public property URL, and
supersedes aliases instead of deleting them.  Unique child/history rows move to
the survivor.  Older conflicting image and OM rows remain attached to the
superseded alias, so no extracted fact value is invented, edited, or deleted.

The default mode is read-only.  Persistent apply requires a new owner-only
preimage path.  Every mode holds the canonical CRE lock and a database advisory
lock; any drift from the reviewed shape aborts before mutation.

Preimage schema v6 keeps a compact clear validation/staging envelope and stores
the full exact rollback state once inside a reversible pgcrypto PGP compression
envelope. The passphrase is a format constant, not a secret; owner-only preimage
permissions remain the confidentiality boundary.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir
from cre_ingest import (
    canonical_cushman_identity_url,
    cushman_canonical_external_id,
    database_target_fingerprint_from_url,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
    sql_lit,
)

EXPECTED_ARTIFACT_SHA256 = (
    "cce2ace6d12a7488c00cf431fde4cc8bd90187557a1dfd99b1b9d925a50b6aba"
)
EXPECTED_DB_TARGET_SHA256 = (
    "faf5d034d1f085ce09dd7afd0cc013dcbf474a81a73dc60fafa6c8884bfdf9ee"
)
EXPECTED_GENERATION = "2026-07-30T082113Z"
EXPECTED_ARTIFACT_ROWS = 11_661
EXPECTED_ARTIFACT_TARGETS = 11_651
EXPECTED_TOTAL_ROWS = 13_460
EXPECTED_ACTIVE_ROWS = 13_436
EXPECTED_DELETED_ROWS = 24
EXPECTED_TARGETS = 12_898
EXPECTED_ACTIVE_ALIASES = 538
EXPECTED_ALL_ALIASES = 562
EXPECTED_SOURCE_INDEX_ROWS = 13_011
EXPECTED_SOURCE_INDEX_TARGETS = 12_738
EXPECTED_QUEUE_ROWS = 235
EXPECTED_OM_FACTS = 49_689
EXPECTED_OM_UNIQUE = 49_629
EXPECTED_OM_CONFLICTS = 60
EXPECTED_OM_DIVERGENT = 37
EXPECTED_OM_CONFLICT_TARGETS = 3
EXPECTED_IMAGE_ROWS = 11_441
EXPECTED_IMAGE_CONFLICTS = 37
EXPECTED_CONTACT_ROWS = 2_671
EXPECTED_DOCUMENT_ROWS = 1_675
EXPECTED_EVENT_ROWS = 1_534
EXPECTED_GEOMETRY_SHA256 = (
    "349cd49ee9efc9d79a48971c0abef6f600e94d1b2fec5394087c05167018fe02"
)
EXPECTED_ARTIFACT_GEOMETRY_SHA256 = (
    "688d9a51fe98306b6fc431948890aa1a989d8793ae5aada6325267386e0df4b6"
)
EXPECTED_GROUP_SHAPES = {
    "active1_deleted0_current0_old1": 1242,
    "active1_deleted0_current1_old0": 11098,
    "active1_deleted1_current0_old2": 2,
    "active1_deleted1_current1_old1": 20,
    "active2_deleted0_current0_old2": 3,
    "active2_deleted0_current1_old1": 523,
    "active2_deleted0_current2_old0": 7,
    "active2_deleted1_current1_old2": 2,
    "active4_deleted0_current4_old0": 1,
}
EXPECTED_FK_TABLES = [
    "credeals.cre_listing_contacts",
    "credeals.cre_listing_documents",
    "credeals.cre_listing_events",
    "credeals.cre_listing_images",
    "credeals.cre_listing_links",
    "credeals.cre_listing_media",
    "credeals.cre_listing_om_facts",
    "credeals.cre_listing_price_history",
    "credeals.cre_scrape_log",
]
DEFAULT_ARTIFACT = Path(__file__).resolve().parent / (
    "out/checkpoint-refresh/2026-07-30T082113Z/sources/cushman-wakefield.json"
)
DEFAULT_LOCK = canonical_shared_lock_dir()
ADVISORY_LOCK_KEY = 734_251_907_300_821_130
# Bound both the compact outer file and the exact decrypted rollback document.
# Live schema-v5 diagnostics measured 96,244,620 inner bytes before whole-
# document compression, so 128 MiB leaves bounded headroom for the exact state.
MAX_PREIMAGE_BYTES = 64 * 1024 * 1024
MAX_INNER_PREIMAGE_BYTES = 128 * 1024 * 1024
# Client-side wall clocks for nonpersistent verification. PostgreSQL's
# statement_timeout is per statement, while exact preimage capture contains
# several independently bounded full-state statements. Live evidence showed
# those statements can cumulatively exceed 30 minutes without any one statement
# stalling, so capture gets a separate one-hour orchestration bound. Persistent
# apply/rollback paths deliberately do not use client timeouts because losing a
# commit response would require separate state reconciliation.
PREIMAGE_CAPTURE_TIMEOUT_SECONDS = 60 * 60
ROLLBACK_VERIFICATION_TIMEOUT_SECONDS = 30 * 60
# Keep every psql simple-query message comfortably below hosted proxy limits.
# json.dumps(..., ensure_ascii=True) makes one Python character exactly one byte;
# SQL quote escaping can at most double apostrophes, so a 1 MiB emitted-statement
# ceiling leaves ample headroom above each 256 KiB raw chunk.
PREIMAGE_SQL_CHUNK_BYTES = 256 * 1024
PREIMAGE_SQL_STATEMENT_CEILING_BYTES = 1024 * 1024
PREIMAGE_OUTPUT_PROTOCOL = "cushman-preimage-chunks-v4"
PREIMAGE_OUTPUT_ENCODING = "base64"
PREIMAGE_OUTPUT_CHUNK_CHARS = 256 * 1024
PREIMAGE_OUTPUT_ROW_CEILING_BYTES = 512 * 1024
PREIMAGE_OUTPUT_MAX_CHUNKS = (
    4 * ((MAX_PREIMAGE_BYTES + 2) // 3)
    + PREIMAGE_OUTPUT_CHUNK_CHARS
    - 1
) // PREIMAGE_OUTPUT_CHUNK_CHARS
PREIMAGE_OUTPUT_KEYS = frozenset(
    {
        "protocol",
        "seq",
        "count",
        "payloadBytes",
        "payloadMd5",
        "encoding",
        "chunk",
    }
)
PREIMAGE_INNER_SCHEMA_VERSION = 1
PREIMAGE_INNER_ENCODING = "pgcrypto-pgp-zlib-base64-v1"
# This domain-separated constant enables self-contained reversible compression.
# It is not a confidentiality secret; the owner-only preimage file is the
# confidentiality boundary.
PREIMAGE_COMPRESSION_PASSPHRASE = (
    "cushman-identity-preimage-v6:compression-envelope:not-a-secret"
)
# Level 9 held the pgcrypto zlib call for more than 15 minutes on the reviewed
# 96 MiB inner document without honoring statement_timeout. Level 1 is still
# lossless; the decrypted byte/SHA guards and 64 MiB outer bound prove that the
# faster self-describing PGP envelope remains exact and transportable.
PREIMAGE_COMPRESSION_PGP_OPTIONS = (
    "cipher-algo=aes256,compress-algo=2,compress-level=1,s2k-count=1024"
)
PREIMAGE_INNER_COUNTS = {
    "repairPlan": EXPECTED_TOTAL_ROWS,
    "listings": EXPECTED_TOTAL_ROWS,
    "contacts": EXPECTED_CONTACT_ROWS,
    "documents": EXPECTED_DOCUMENT_ROWS,
    "images": EXPECTED_IMAGE_ROWS,
    "media": 0,
    "links": 0,
    "omFacts": EXPECTED_OM_FACTS,
    "events": EXPECTED_EVENT_ROWS,
    "priceHistory": 0,
    "scrapeLogs": 0,
    "sourceIndex": EXPECTED_SOURCE_INDEX_ROWS,
    "queue": EXPECTED_QUEUE_ROWS,
}
PREIMAGE_INNER_SECTION_KEYS = ("schemaVersion", *PREIMAGE_INNER_COUNTS)
REPAIR_TOKEN = hashlib.sha256(
    (
        EXPECTED_ARTIFACT_SHA256
        + EXPECTED_DB_TARGET_SHA256
        + EXPECTED_GENERATION
    ).encode()
).hexdigest()


def expected_inner_counts() -> dict[str, int]:
    """Return a copy of the immutable schema-v6 inner count contract."""
    return dict(PREIMAGE_INNER_COUNTS)


def inner_section_values_sql() -> str:
    """Return the exact relational key set for schema-v6 inner validation."""
    return ",\n       ".join(
        f"({sql_lit(key)})" for key in PREIMAGE_INNER_SECTION_KEYS
    )


SOURCE_INDEX_DONOR_ORDER_SQL = """si.last_enumerated_at DESC NULLS LAST,
            si.last_seen DESC NULLS LAST,si.soft_deleted,si.id"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_source_index_donor(rows: list[dict]) -> dict:
    """Return the single coherent donor selected by the SQL repair contract."""
    if not rows:
        raise ValueError("source-index donor group is empty")

    def descending_time(value: object) -> float:
        if value is None:
            return float("inf")
        if not isinstance(value, str):
            raise ValueError("source-index donor timestamp is invalid")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("source-index donor timestamp lacks timezone")
        return -parsed.timestamp()

    return min(
        rows,
        key=lambda row: (
            descending_time(row.get("last_enumerated_at")),
            descending_time(row.get("last_seen")),
            bool(row.get("soft_deleted")),
            str(row.get("id")),
        ),
    )


@dataclass(frozen=True)
class ArtifactRow:
    provider_id: str
    source_url: str
    target_id: str
    transaction_mode: str

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "source_url": self.source_url,
            "target_id": self.target_id,
            "transaction_mode": self.transaction_mode,
        }


def load_artifact(path: Path) -> list[ArtifactRow]:
    if sha256_file(path) != EXPECTED_ARTIFACT_SHA256:
        raise ValueError("Cushman repair artifact SHA-256 does not match")
    payload = json.loads(path.read_text())
    if (payload.get("runMeta") or {}).get("freshness", {}).get(
        "generationId"
    ) != EXPECTED_GENERATION:
        raise ValueError("Cushman repair artifact generation does not match")
    listings = payload.get("listings")
    if not isinstance(listings, list) or len(listings) != EXPECTED_ARTIFACT_ROWS:
        raise ValueError("Cushman repair artifact row count drifted")
    rows: list[ArtifactRow] = []
    for item in listings:
        provider_id = item.get("id")
        source_url = item.get("url")
        mode = item.get("transactionMode")
        target_id = cushman_canonical_external_id(source_url)
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or not isinstance(source_url, str)
            or target_id is None
            or mode not in {"sale", "lease"}
        ):
            raise ValueError("Cushman artifact contains an unsafe identity row")
        rows.append(ArtifactRow(provider_id, source_url, target_id, mode))
    if len({row.provider_id for row in rows}) != EXPECTED_ARTIFACT_ROWS:
        raise ValueError("Cushman artifact provider IDs are not unique")
    if len({row.target_id for row in rows}) != EXPECTED_ARTIFACT_TARGETS:
        raise ValueError("Cushman artifact target count drifted")
    geometry = hashlib.sha256(
        json.dumps(
            sorted((row.provider_id, row.source_url) for row in rows),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if geometry != EXPECTED_ARTIFACT_GEOMETRY_SHA256:
        raise ValueError("Cushman artifact identity geometry drifted")
    return rows


def generation_expr(alias: str) -> str:
    return f"""COALESCE(
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{secondary_pass,freshnessProvenance,generationId}}','')
    )"""


def run_psql(
    db_url: str,
    sql: str,
    timeout_seconds: float | None = None,
    result_mode: str = "json",
) -> object:
    if result_mode not in {"json", "preimage_chunks"}:
        raise ValueError("unsupported psql result mode")
    try:
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
            ],
            env=psql_connection_env(db_url),
            input=sql,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stderr = exc.stderr
        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode("utf-8", errors="replace")
        if partial_stderr:
            sys.stderr.write(partial_stderr)
        raise RuntimeError(
            "rollback-only verification exceeded its "
            f"{timeout_seconds:g}s client wall-clock timeout; "
            "the database result was not accepted"
        ) from exc
    if proc.returncode:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        raise RuntimeError(f"psql exited {proc.returncode}")
    if result_mode == "preimage_chunks":
        return parse_preimage_chunk_output(proc.stdout)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("psql returned no JSON result")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("psql returned an unexpected result") from exc


def parse_preimage_chunk_output(stdout: str) -> dict:
    """Strictly reassemble and validate the versioned preimage row protocol."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("psql returned no preimage chunks")
    envelopes = []
    for line in lines:
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("psql returned an invalid preimage chunk") from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != PREIMAGE_OUTPUT_KEYS
            or envelope.get("protocol") != PREIMAGE_OUTPUT_PROTOCOL
            or envelope.get("encoding") != PREIMAGE_OUTPUT_ENCODING
            or type(envelope.get("seq")) is not int
            or type(envelope.get("count")) is not int
            or type(envelope.get("payloadBytes")) is not int
            or not isinstance(envelope.get("payloadMd5"), str)
            or not isinstance(envelope.get("chunk"), str)
        ):
            raise RuntimeError("psql returned an invalid preimage chunk envelope")
        envelopes.append(envelope)

    first = envelopes[0]
    count = first["count"]
    payload_bytes = first["payloadBytes"]
    payload_md5 = first["payloadMd5"]
    max_encoded_chars = 4 * ((MAX_PREIMAGE_BYTES + 2) // 3)
    max_chunks = (
        max_encoded_chars + PREIMAGE_OUTPUT_CHUNK_CHARS - 1
    ) // PREIMAGE_OUTPUT_CHUNK_CHARS
    if (
        count <= 0
        or count > max_chunks
        or len(envelopes) != count + 1
        or payload_bytes <= 0
        or payload_bytes > MAX_PREIMAGE_BYTES
        or not re.fullmatch(r"[0-9a-f]{32}", payload_md5)
    ):
        raise RuntimeError("psql returned invalid preimage chunk geometry")

    terminal = envelopes[-1]
    if (
        terminal["seq"] != count
        or terminal["count"] != count
        or terminal["payloadBytes"] != payload_bytes
        or terminal["payloadMd5"] != payload_md5
        or terminal["chunk"] != ""
    ):
        raise RuntimeError("psql returned invalid preimage completion marker")

    encoded_chunks = []
    for seq, envelope in enumerate(envelopes[:-1]):
        if (
            envelope["seq"] != seq
            or envelope["count"] != count
            or envelope["payloadBytes"] != payload_bytes
            or envelope["payloadMd5"] != payload_md5
        ):
            raise RuntimeError("psql returned inconsistent preimage chunks")
        chunk = envelope["chunk"]
        try:
            chunk.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RuntimeError("psql returned a non-ASCII preimage chunk") from exc
        if (
            (seq < count - 1 and len(chunk) != PREIMAGE_OUTPUT_CHUNK_CHARS)
            or (
                seq == count - 1
                and not 1 <= len(chunk) <= PREIMAGE_OUTPUT_CHUNK_CHARS
            )
        ):
            raise RuntimeError("psql returned invalid preimage chunk geometry")
        encoded_chunks.append(chunk)

    encoded = "".join(encoded_chunks)
    recomputed_count = (
        len(encoded) + PREIMAGE_OUTPUT_CHUNK_CHARS - 1
    ) // PREIMAGE_OUTPUT_CHUNK_CHARS
    if recomputed_count != count or len(encoded) % 4:
        raise RuntimeError("psql returned invalid preimage chunk geometry")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("psql returned invalid preimage base64") from exc
    if len(raw) != payload_bytes:
        raise RuntimeError("psql returned a preimage byte-count mismatch")
    if (
        hashlib.md5(raw, usedforsecurity=False).hexdigest()
        != payload_md5
    ):
        raise RuntimeError("psql returned a preimage digest mismatch")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("psql returned invalid preimage UTF-8") from exc
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise RuntimeError("psql returned invalid preimage JSON") from exc
    try:
        return validate_preimage(payload)
    except ValueError as exc:
        raise RuntimeError("psql returned invalid preimage schema") from exc


def state_read_sql() -> str:
    return f"""
BEGIN READ ONLY;
SET LOCAL statement_timeout='2min';
WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='cushman-wakefield'
)
SELECT jsonb_build_object(
  'listings', (
    SELECT jsonb_agg(jsonb_build_object(
      'id',l.id,
      'external_id',l.external_id,
      'source_url',l.source_url,
      'deleted',l.deleted_at IS NOT NULL,
      'generation',{generation_expr("l")},
      'updated_at',l.updated_at
    ) ORDER BY l.id)
    FROM credeals.cre_listings l CROSS JOIN b
    WHERE l.brokerage_id=b.id
  ),
  'sourceIndex', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id',si.id,'external_id',si.external_id,'url',si.url,
      'last_seen',si.last_seen,'last_enumerated_at',si.last_enumerated_at
    ) ORDER BY si.id),'[]'::jsonb)
    FROM credeals.cre_source_index si CROSS JOIN b
    WHERE si.brokerage_id=b.id
  ),
  'queue', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id',q.id,'external_id',q.external_id,'url',q.url,'reason',q.reason,
      'claimed',q.claimed_at IS NOT NULL AND q.done_at IS NULL,
      'enqueued_at',q.enqueued_at,'claimed_at',q.claimed_at,
      'done_at',q.done_at,'attempts',q.attempts
    ) ORDER BY q.id),'[]'::jsonb)
    FROM credeals.cre_enrichment_queue q CROSS JOIN b
    WHERE q.brokerage_id=b.id
  )
)::text;
ROLLBACK;
"""


def _target_for_url(url: object) -> tuple[str, str]:
    target = cushman_canonical_external_id(url)
    identity_url = canonical_cushman_identity_url(url)
    if target is None or identity_url is None:
        raise ValueError("live Cushman row has an unsafe source URL")
    return target, identity_url


def load_live_state(db_url: str) -> dict:
    raw = run_psql(db_url, state_read_sql())
    if not isinstance(raw, dict):
        raise ValueError("live Cushman state is not an object")
    listings = raw.get("listings")
    source_index = raw.get("sourceIndex")
    queue = raw.get("queue")
    if not all(isinstance(value, list) for value in (listings, source_index, queue)):
        raise ValueError("live Cushman state is incomplete")
    for row in listings:
        target, identity_url = _target_for_url(row.get("source_url"))
        row["target_id"] = target
        row["identity_url"] = identity_url
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in listings:
        groups[row["target_id"]].append(row)
    shapes = Counter()
    for group in groups.values():
        active = sum(not row["deleted"] for row in group)
        deleted = len(group) - active
        current = sum(row.get("generation") == EXPECTED_GENERATION for row in group)
        key = (
            f"active{active}_deleted{deleted}_current{current}_"
            f"old{len(group)-current}"
        )
        shapes[key] += 1
    geometry_rows = [
        (
            target,
            sorted(
                (row["id"], row["external_id"], row["deleted"])
                for row in group
            ),
        )
        for target, group in sorted(groups.items())
    ]
    geometry = hashlib.sha256(
        json.dumps(geometry_rows, separators=(",", ":")).encode()
    ).hexdigest()
    checks = {
        "total": len(listings) == EXPECTED_TOTAL_ROWS,
        "active": sum(not row["deleted"] for row in listings)
        == EXPECTED_ACTIVE_ROWS,
        "deleted": sum(row["deleted"] for row in listings)
        == EXPECTED_DELETED_ROWS,
        "targets": len(groups) == EXPECTED_TARGETS,
        "geometry": geometry == EXPECTED_GEOMETRY_SHA256,
        "shapes": dict(shapes) == EXPECTED_GROUP_SHAPES,
        "all_legacy": all(
            not re.fullmatch(r"url:v1:[0-9a-f]{32}", row["external_id"] or "")
            for row in listings
        ),
        "source_index": len(source_index) == EXPECTED_SOURCE_INDEX_ROWS,
        "queue": len(queue) == EXPECTED_QUEUE_ROWS,
        "queue_unclaimed": not any(row.get("claimed") for row in queue),
    }
    if not all(checks.values()):
        failed = sorted(key for key, ok in checks.items() if not ok)
        raise ValueError("live Cushman repair shape drifted: " + ", ".join(failed))
    for row in source_index:
        row["target_id"], _ = _target_for_url(row.get("url"))
    if len({row["target_id"] for row in source_index}) != EXPECTED_SOURCE_INDEX_TARGETS:
        raise ValueError("Cushman source-index target count drifted")
    seen_queue: set[tuple[str, str]] = set()
    for row in queue:
        row["target_id"], _ = _target_for_url(row.get("url"))
        key = (row["target_id"], row.get("reason"))
        if key in seen_queue:
            raise ValueError("Cushman queue target/reason collision drifted")
        seen_queue.add(key)
    return {
        "listings": listings,
        "sourceIndex": source_index,
        "queue": queue,
        "geometrySha256": geometry,
    }


def _generation_from_raw_data(raw_data: object) -> str | None:
    if not isinstance(raw_data, dict):
        return None
    candidates = (
        ("latestInventoryObservation", "freshnessProvenance", "generationId"),
        (
            "latestInventoryObservation",
            "primary",
            "freshnessProvenance",
            "generationId",
        ),
        ("freshnessProvenance", "generationId"),
        ("primary", "freshnessProvenance", "generationId"),
        ("secondary_pass", "freshnessProvenance", "generationId"),
    )
    for path in candidates:
        value: object = raw_data
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def state_from_preimage(preimage: dict) -> dict:
    """Reconstruct the reviewed pre-repair plan for an explicit rollback."""
    validate_preimage(preimage)
    listings = []
    for original in preimage["stateListings"]:
        row = {
            "id": original["id"],
            "external_id": original["external_id"],
            "source_url": original["source_url"],
            "deleted": original["deleted"],
            "generation": original["generation"],
            "updated_at": original["updated_at"],
        }
        row["target_id"], row["identity_url"] = _target_for_url(
            row["source_url"]
        )
        listings.append(row)
    source_index = []
    for original in preimage["stateSourceIndex"]:
        row = {
            "id": original["id"],
            "external_id": original["external_id"],
            "url": original["url"],
            "last_seen": original.get("last_seen"),
            "last_enumerated_at": original.get("last_enumerated_at"),
        }
        row["target_id"], _ = _target_for_url(row["url"])
        source_index.append(row)
    queue = []
    for original in preimage["stateQueue"]:
        row = {
            "id": original["id"],
            "external_id": original["external_id"],
            "url": original["url"],
            "reason": original["reason"],
            "claimed": False,
            "enqueued_at": original.get("enqueued_at"),
            "claimed_at": original.get("claimed_at"),
            "done_at": original.get("done_at"),
            "attempts": original.get("attempts"),
        }
        row["target_id"], _ = _target_for_url(row["url"])
        queue.append(row)
    return {
        "listings": listings,
        "sourceIndex": source_index,
        "queue": queue,
        "geometrySha256": preimage["geometrySha256"],
    }


def named_json_payload_chunks(payload: list[object]) -> tuple[str, ...]:
    """Serialize one reviewed stage array into bounded ASCII JSON chunks."""
    if not isinstance(payload, list):
        raise TypeError("staged JSON payload must be an array")
    encoded = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    )
    encoded.encode("ascii")
    return tuple(
        encoded[start : start + PREIMAGE_SQL_CHUNK_BYTES]
        for start in range(0, len(encoded), PREIMAGE_SQL_CHUNK_BYTES)
    )


def named_json_payload_transport_sql(
    name: str, payload: list[object]
) -> str:
    """Build bounded, integrity-checked SQL transport for one named array."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("staged JSON payload name is unsafe")
    chunks = named_json_payload_chunks(payload)
    encoded = "".join(chunks)
    expected_bytes = len(encoded.encode("ascii"))
    expected_md5 = hashlib.md5(
        encoded.encode("ascii"), usedforsecurity=False
    ).hexdigest()
    chunks_table = f"_cw_stage_{name}_chunks"
    assembled_table = f"_cw_stage_{name}_assembled"
    payload_table = f"_cw_stage_{name}_payload"
    inserts = []
    for seq, chunk in enumerate(chunks):
        statement = (
            f"INSERT INTO {chunks_table}(seq,payload) VALUES "
            f"({seq},{sql_lit(chunk)});"
        )
        if (
            len(statement.encode("utf-8"))
            > PREIMAGE_SQL_STATEMENT_CEILING_BYTES
        ):
            raise ValueError("staged JSON SQL chunk exceeds statement ceiling")
        inserts.append(statement)
    return f"""
CREATE TEMP TABLE {chunks_table}(
 seq integer PRIMARY KEY CHECK (seq>=0),
 payload text NOT NULL
) ON COMMIT DROP;
{chr(10).join(inserts)}
CREATE TEMP TABLE {assembled_table}(
 payload text NOT NULL
) ON COMMIT DROP;
INSERT INTO {assembled_table}(payload)
SELECT string_agg(payload,'' ORDER BY seq)
FROM {chunks_table}
HAVING count(*)={len(chunks)}
   AND min(seq)=0
   AND max(seq)={len(chunks) - 1};
DO $cw_stage_{name}$
BEGIN
 IF (SELECT count(*) FROM {assembled_table})<>1 THEN
   RAISE EXCEPTION 'Cushman stage {name} chunk geometry mismatch';
 END IF;
 IF (
   SELECT octet_length(payload)<>{expected_bytes}
       OR md5(payload)<>{sql_lit(expected_md5)}
   FROM {assembled_table}
 ) THEN
   RAISE EXCEPTION 'Cushman stage {name} payload integrity mismatch';
 END IF;
END
$cw_stage_{name}$;
CREATE TEMP TABLE {payload_table}(payload jsonb NOT NULL) ON COMMIT DROP;
INSERT INTO {payload_table}(payload)
SELECT payload::jsonb FROM {assembled_table};
DO $cw_stage_{name}_array$
BEGIN
 IF (
   SELECT count(*)<>1
       OR min(jsonb_typeof(payload))<>'array'
       OR min(jsonb_array_length(payload))<>{len(payload)}
   FROM {payload_table}
 ) THEN
   RAISE EXCEPTION 'Cushman stage {name} array/count mismatch';
 END IF;
END
$cw_stage_{name}_array$;
DROP TABLE {chunks_table},{assembled_table};
"""


def stage_sql(artifact: list[ArtifactRow], state: dict) -> str:
    artifact_payload = [row.as_dict() for row in artifact]
    payloads = {
        "artifact": artifact_payload,
        "listings": state["listings"],
        "source_index": state["sourceIndex"],
        "queue": state["queue"],
    }
    if not all(isinstance(payload, list) for payload in payloads.values()):
        raise ValueError("Cushman stage payload is not an array")
    transport = "\n".join(
        named_json_payload_transport_sql(name, payload)
        for name, payload in payloads.items()
    )
    return f"""
{transport}
CREATE TEMP TABLE _cw_artifact ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT payload FROM _cw_stage_artifact_payload)
) AS x(
  provider_id text, source_url text, target_id text, transaction_mode text
);
CREATE UNIQUE INDEX ON _cw_artifact(provider_id);

CREATE TEMP TABLE _cw_rows ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT payload FROM _cw_stage_listings_payload)
) AS x(
  id uuid, external_id text, source_url text, deleted boolean,
  generation text, updated_at timestamptz, target_id text, identity_url text
);
CREATE UNIQUE INDEX ON _cw_rows(id);

CREATE TEMP TABLE _cw_si_plan ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT payload FROM _cw_stage_source_index_payload)
) AS x(
  id uuid, external_id text, url text, last_seen timestamptz,
  last_enumerated_at timestamptz, target_id text
);
CREATE UNIQUE INDEX ON _cw_si_plan(id);

CREATE TEMP TABLE _cw_queue_plan ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT payload FROM _cw_stage_queue_payload)
) AS x(
  id uuid, external_id text, url text, reason text, claimed boolean,
  enqueued_at timestamptz, claimed_at timestamptz, done_at timestamptz,
  attempts integer, target_id text
);
CREATE UNIQUE INDEX ON _cw_queue_plan(id);

DO $cw_stage_counts$
BEGIN
 IF (SELECT count(*) FROM _cw_artifact)<>{len(artifact_payload)} THEN
   RAISE EXCEPTION 'Cushman staged artifact row count mismatch';
 END IF;
 IF (SELECT count(*) FROM _cw_rows)<>{len(payloads["listings"])} THEN
   RAISE EXCEPTION 'Cushman staged listings row count mismatch';
 END IF;
 IF (SELECT count(*) FROM _cw_si_plan)<>{len(payloads["source_index"])} THEN
   RAISE EXCEPTION 'Cushman staged source-index row count mismatch';
 END IF;
 IF (SELECT count(*) FROM _cw_queue_plan)<>{len(payloads["queue"])} THEN
   RAISE EXCEPTION 'Cushman staged queue row count mismatch';
 END IF;
END
$cw_stage_counts$;
DROP TABLE _cw_stage_artifact_payload,_cw_stage_listings_payload,
 _cw_stage_source_index_payload,_cw_stage_queue_payload;
"""


def pgcrypto_preflight_sql() -> str:
    """Fail closed unless the exact reversible-compression surface works."""
    probe = '{"cushman":"preimage-v6","unicode":"λ"}'
    probe_sha256 = hashlib.sha256(probe.encode()).hexdigest()
    return f"""
DO $cw_pgcrypto$
DECLARE
 probe text:={sql_lit(probe)};
 packed bytea;
 unpacked text;
 probe_sha256 text;
BEGIN
 IF to_regprocedure('pgp_sym_encrypt(text,text,text)') IS NULL
    OR to_regprocedure('pgp_sym_decrypt(bytea,text)') IS NULL
    OR to_regprocedure('digest(bytea,text)') IS NULL THEN
   RAISE EXCEPTION
     'Cushman repair requires pgcrypto encrypt/decrypt/digest functions';
 END IF;
 packed:=pgp_sym_encrypt(
   probe,
   {sql_lit(PREIMAGE_COMPRESSION_PASSPHRASE)},
   {sql_lit(PREIMAGE_COMPRESSION_PGP_OPTIONS)}
 );
 unpacked:=pgp_sym_decrypt(
   packed,{sql_lit(PREIMAGE_COMPRESSION_PASSPHRASE)}
 );
 probe_sha256:=encode(
   digest(convert_to(probe,'UTF8'),'sha256'),'hex'
 );
 IF unpacked IS DISTINCT FROM probe
    OR probe_sha256 IS DISTINCT FROM {sql_lit(probe_sha256)} THEN
   RAISE EXCEPTION 'Cushman pgcrypto preflight roundtrip failed';
 END IF;
END
$cw_pgcrypto$;
"""


def invariant_sql() -> str:
    fk_array = "ARRAY[" + ",".join(sql_lit(value) for value in EXPECTED_FK_TABLES) + "]::text[]"
    return f"""
DO $guard$
DECLARE
  brokerage uuid;
  mismatches integer;
  claimed integer;
  fk_tables text[];
BEGIN
  SELECT id INTO brokerage FROM credeals.cre_brokerages
  WHERE slug='cushman-wakefield';
  IF brokerage IS NULL THEN RAISE EXCEPTION 'Cushman brokerage is absent'; END IF;

  SELECT count(*) INTO mismatches
  FROM _cw_rows p
  FULL JOIN (
    SELECT * FROM credeals.cre_listings WHERE brokerage_id=brokerage
  ) l ON l.id=p.id
  WHERE p.id IS NULL OR l.id IS NULL
     OR l.external_id IS DISTINCT FROM p.external_id
     OR l.source_url IS DISTINCT FROM p.source_url
     OR (l.deleted_at IS NOT NULL) IS DISTINCT FROM p.deleted
     OR {generation_expr("l")} IS DISTINCT FROM p.generation
     OR l.updated_at IS DISTINCT FROM p.updated_at;
  IF mismatches<>0 THEN
    RAISE EXCEPTION 'Cushman listing snapshot drift: %',mismatches;
  END IF;

  SELECT count(*) INTO mismatches
  FROM _cw_artifact a
  LEFT JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=a.provider_id
   AND l.source_url=a.source_url
   AND {generation_expr("l")}={sql_lit(EXPECTED_GENERATION)}
   AND l.deleted_at IS NULL
  WHERE l.id IS NULL;
  IF mismatches<>0 THEN
    RAISE EXCEPTION 'Cushman artifact/current-generation mismatch: %',mismatches;
  END IF;

  SELECT count(*) INTO mismatches
  FROM _cw_si_plan p
  FULL JOIN (
    SELECT * FROM credeals.cre_source_index WHERE brokerage_id=brokerage
  ) si ON si.id=p.id
  WHERE p.id IS NULL OR si.id IS NULL
     OR si.external_id IS DISTINCT FROM p.external_id
     OR si.url IS DISTINCT FROM p.url
     OR si.last_seen IS DISTINCT FROM p.last_seen
     OR si.last_enumerated_at IS DISTINCT FROM p.last_enumerated_at;
  IF mismatches<>0 THEN
    RAISE EXCEPTION 'Cushman source-index snapshot drift: %',mismatches;
  END IF;

  SELECT count(*) INTO mismatches
  FROM _cw_queue_plan p
  FULL JOIN (
    SELECT * FROM credeals.cre_enrichment_queue WHERE brokerage_id=brokerage
  ) q ON q.id=p.id
  WHERE p.id IS NULL OR q.id IS NULL
     OR q.external_id IS DISTINCT FROM p.external_id
     OR q.url IS DISTINCT FROM p.url
     OR q.reason IS DISTINCT FROM p.reason
     OR q.enqueued_at IS DISTINCT FROM p.enqueued_at
     OR q.claimed_at IS DISTINCT FROM p.claimed_at
     OR q.done_at IS DISTINCT FROM p.done_at
     OR q.attempts IS DISTINCT FROM p.attempts;
  IF mismatches<>0 THEN
    RAISE EXCEPTION 'Cushman queue snapshot drift: %',mismatches;
  END IF;

  SELECT count(*) INTO claimed
  FROM credeals.cre_enrichment_queue q
  JOIN _cw_queue_plan p ON p.id=q.id
  WHERE q.claimed_at IS NOT NULL AND q.done_at IS NULL;
  IF claimed<>0 THEN RAISE EXCEPTION 'Cushman queue has claimed rows: %',claimed; END IF;

  SELECT array_agg(format('%I.%I',n.nspname,c.relname)
                   ORDER BY format('%I.%I',n.nspname,c.relname))
  INTO fk_tables
  FROM pg_constraint con
  JOIN pg_class c ON c.oid=con.conrelid
  JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE con.contype='f'
    AND con.confrelid='credeals.cre_listings'::regclass;
  IF fk_tables IS DISTINCT FROM {fk_array} THEN
    RAISE EXCEPTION 'unreviewed cre_listings FK surface: %',fk_tables;
  END IF;
END
$guard$;

CREATE TEMP TABLE _cw_relationship_score ON COMMIT DROP AS
SELECT r.id,
  (SELECT count(*) FROM credeals.cre_listing_contacts x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_documents x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_images x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_media x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_links x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_om_facts x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_events x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_listing_price_history x WHERE x.listing_id=r.id)+
  (SELECT count(*) FROM credeals.cre_scrape_log x WHERE x.listing_id=r.id) AS score
FROM _cw_rows r;

CREATE TEMP TABLE _cw_om_ranked ON COMMIT DROP AS
SELECT r.target_id,f.id,f.listing_id,f.fact_group,f.fact_key,f.source_doc_url,
       f.parser_version,f.fact_value_text,f.fact_value_num,f.unit_count,
       f.confidence,f.parsed_at,
       row_number() OVER (
         PARTITION BY r.target_id,f.fact_group,f.fact_key,f.source_doc_url,
                      f.parser_version
         ORDER BY f.parsed_at DESC NULLS LAST,f.id
       ) AS fact_rank,
       count(*) OVER (
         PARTITION BY r.target_id,f.fact_group,f.fact_key,f.source_doc_url,
                      f.parser_version
       ) AS fact_count
FROM credeals.cre_listing_om_facts f
JOIN _cw_rows r ON r.id=f.listing_id;

CREATE TEMP TABLE _cw_om_owner ON COMMIT DROP AS
SELECT target_id,(array_agg(listing_id ORDER BY listing_id))[1] AS owner_id
FROM _cw_om_ranked
WHERE fact_count>1 AND fact_rank=1
GROUP BY target_id
HAVING count(DISTINCT listing_id)=1;

CREATE TEMP TABLE _cw_survivors ON COMMIT DROP AS
SELECT r.target_id,(array_agg(r.id ORDER BY
  CASE WHEN r.generation IS DISTINCT FROM {sql_lit(EXPECTED_GENERATION)}
       THEN 0 ELSE 1 END,
  CASE WHEN r.id=o.owner_id THEN 0 ELSE 1 END,
  s.score DESC,l.created_at,r.id
))[1] AS survivor_id
FROM _cw_rows r
JOIN credeals.cre_listings l ON l.id=r.id
JOIN _cw_relationship_score s ON s.id=r.id
LEFT JOIN _cw_om_owner o ON o.target_id=r.target_id
WHERE NOT r.deleted
GROUP BY r.target_id;
CREATE UNIQUE INDEX ON _cw_survivors(target_id);

CREATE TEMP TABLE _cw_current ON COMMIT DROP AS
SELECT DISTINCT ON (r.target_id) r.target_id,l.*
FROM _cw_rows r
JOIN credeals.cre_listings l ON l.id=r.id
LEFT JOIN _cw_om_owner o ON o.target_id=r.target_id
JOIN _cw_relationship_score score ON score.id=r.id
WHERE r.generation={sql_lit(EXPECTED_GENERATION)}
ORDER BY r.target_id,
         CASE WHEN r.id=o.owner_id THEN 0 ELSE 1 END,
         score.score DESC,l.updated_at DESC,l.id;
CREATE UNIQUE INDEX ON _cw_current(target_id);

CREATE TEMP TABLE _cw_aliases ON COMMIT DROP AS
SELECT r.*,s.survivor_id,
  'cushman-migration:v1:'||md5(r.id::text) AS temp_id,
  'cushman-superseded:v1:'||md5(r.id::text) AS superseded_id
FROM _cw_rows r JOIN _cw_survivors s USING(target_id)
WHERE r.id<>s.survivor_id;

DO $shape$
DECLARE
  om_conflicts integer;
  om_divergent integer;
  om_targets integer;
  image_conflicts integer;
BEGIN
  IF (SELECT count(*) FROM _cw_survivors)<>{EXPECTED_TARGETS}
     OR (SELECT count(*) FROM _cw_aliases)<>{EXPECTED_ALL_ALIASES}
     OR (SELECT count(*) FROM _cw_aliases WHERE NOT deleted)<>{EXPECTED_ACTIVE_ALIASES}
  THEN RAISE EXCEPTION 'Cushman survivor/alias shape drift'; END IF;

  SELECT count(*),count(*) FILTER (
    WHERE variants>1
  ),count(DISTINCT target_id)
  INTO om_conflicts,om_divergent,om_targets
  FROM (
    SELECT target_id,fact_group,fact_key,source_doc_url,parser_version,
           count(*) n,
           count(DISTINCT (fact_value_text,fact_value_num,unit_count,confidence))
             AS variants
    FROM _cw_om_ranked
    GROUP BY target_id,fact_group,fact_key,source_doc_url,parser_version
    HAVING count(*)>1
  ) q;
  IF om_conflicts<>{EXPECTED_OM_CONFLICTS}
     OR om_divergent<>{EXPECTED_OM_DIVERGENT}
     OR om_targets<>{EXPECTED_OM_CONFLICT_TARGETS}
     OR EXISTS (
       SELECT 1 FROM _cw_om_owner o
       JOIN _cw_survivors s USING(target_id)
       WHERE o.owner_id<>s.survivor_id
     )
  THEN RAISE EXCEPTION 'Cushman OM conflict shape/owner drift'; END IF;

  SELECT count(*) INTO image_conflicts
  FROM (
    SELECT r.target_id,i.url
    FROM credeals.cre_listing_images i JOIN _cw_rows r ON r.id=i.listing_id
    GROUP BY r.target_id,i.url HAVING count(*)>1
  ) q;
  IF image_conflicts<>{EXPECTED_IMAGE_CONFLICTS}
     OR (SELECT count(*) FROM credeals.cre_listing_images i
         JOIN _cw_rows r ON r.id=i.listing_id)<>{EXPECTED_IMAGE_ROWS}
     OR (SELECT count(*) FROM credeals.cre_listing_om_facts f
         JOIN _cw_rows r ON r.id=f.listing_id)<>{EXPECTED_OM_FACTS}
     OR (SELECT count(*) FROM credeals.cre_listing_contacts c
         JOIN _cw_rows r ON r.id=c.listing_id)<>{EXPECTED_CONTACT_ROWS}
     OR (SELECT count(*) FROM credeals.cre_listing_documents d
         JOIN _cw_rows r ON r.id=d.listing_id)<>{EXPECTED_DOCUMENT_ROWS}
     OR (SELECT count(*) FROM credeals.cre_listing_events e
         JOIN _cw_rows r ON r.id=e.listing_id)<>{EXPECTED_EVENT_ROWS}
     OR EXISTS (
       SELECT 1
       FROM credeals.cre_listing_events e
       JOIN _cw_rows r ON r.id=e.listing_id
       GROUP BY r.target_id,e.event_type,COALESCE(e.field,''),
                COALESCE(e.new_value,''),e.scrape_job_id
       HAVING count(*)>1
     )
     OR EXISTS (SELECT 1 FROM credeals.cre_listing_media m
                JOIN _cw_rows r ON r.id=m.listing_id)
     OR EXISTS (SELECT 1 FROM credeals.cre_listing_links k
                JOIN _cw_rows r ON r.id=k.listing_id)
     OR EXISTS (SELECT 1 FROM credeals.cre_listing_price_history h
                JOIN _cw_rows r ON r.id=h.listing_id)
     OR EXISTS (SELECT 1 FROM credeals.cre_scrape_log g
                JOIN _cw_rows r ON r.id=g.listing_id)
  THEN RAISE EXCEPTION 'Cushman child/history shape drift'; END IF;
END
$shape$;
"""


def preflight_sql(artifact: list[ArtifactRow], state: dict) -> str:
    return f"""
BEGIN ISOLATION LEVEL REPEATABLE READ;
SET LOCAL statement_timeout='3min';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{pgcrypto_preflight_sql()}
{stage_sql(artifact,state)}
{invariant_sql()}
CREATE TEMP TABLE _cw_expected_parents ON COMMIT DROP AS
SELECT
  r.id,r.target_id,s.survivor_id,current.id IS NOT NULL AS has_current,
  CASE WHEN r.id=s.survivor_id THEN r.target_id
    ELSE 'cushman-superseded:v1:'||md5(r.id::text) END AS post_external_id,
  CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
    THEN current.source_url ELSE r.source_url END AS post_source_url,
  r.id<>s.survivor_id AS post_deleted,
  CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
    THEN {sql_lit(EXPECTED_GENERATION)} ELSE r.generation END
    AS post_generation,
  jsonb_build_object(
    'external_id',CASE WHEN r.id=s.survivor_id THEN r.target_id
      ELSE 'cushman-superseded:v1:'||md5(r.id::text) END,
    'source_url',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN current.source_url ELSE original.source_url END,
    'canonical_url',CASE
      WHEN r.id<>s.survivor_id THEN original.canonical_url
      WHEN current.id IS NOT NULL THEN COALESCE(
        current.canonical_url,current.source_url,original.canonical_url
      )
      ELSE COALESCE(original.canonical_url,original.source_url)
    END,
    'status',CASE
      WHEN r.id<>s.survivor_id OR current.id IS NULL THEN original.status
      WHEN original.deleted_at IS NOT NULL AND original.status='inactive'
        THEN 'active'
      WHEN current.status IN (
        'sold','leased','off_market','under_contract','pending'
      ) AND NOT (
        original.status IN ('sold','leased','off_market')
        AND current.status IN ('under_contract','pending')
      ) THEN current.status
      ELSE original.status
    END,
    'transaction_type',CASE
      WHEN r.id<>s.survivor_id OR current.id IS NULL
        THEN original.transaction_type
      WHEN original.transaction_type='sale_or_lease'
        OR current.transaction_type='sale_or_lease' THEN 'sale_or_lease'
      WHEN original.transaction_type IS NULL THEN current.transaction_type
      WHEN current.transaction_type IS NOT NULL
        AND current.transaction_type IS DISTINCT FROM original.transaction_type
        THEN 'sale_or_lease'
      ELSE original.transaction_type
    END,
    'property_type',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.property_type,original.property_type)
      ELSE original.property_type END,
    'title',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.title,original.title) ELSE original.title END,
    'address',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.address,original.address) ELSE original.address END,
    'city',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.city,original.city) ELSE original.city END,
    'state',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.state,original.state) ELSE original.state END,
    'zip',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.zip,original.zip) ELSE original.zip END,
    'country',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.country,original.country) ELSE original.country END,
    'lat',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.lat,original.lat) ELSE original.lat END,
    'lng',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.lng,original.lng) ELSE original.lng END,
    'scraped_at',original.scraped_at,
    'raw_data_base',(
      CASE
        WHEN r.id<>s.survivor_id OR current.id IS NULL
          THEN COALESCE(original.raw_data,'{{}}'::jsonb)
        ELSE (
          CASE WHEN jsonb_path_exists(
            original.raw_data,
            '$.**.preserveChildCollections ? (@ == true || @ == "true")'
          ) THEN '{{}}'::jsonb
          ELSE COALESCE(original.raw_data,'{{}}'::jsonb) END
        ) || jsonb_build_object(
          'sourceKey',COALESCE(
            current.raw_data->'sourceKey',
            current.raw_data#>'{{latestInventoryObservation,sourceKey}}',
            original.raw_data->'sourceKey'
          ),
          'latestInventoryObservation',COALESCE(
            current.raw_data->'latestInventoryObservation',current.raw_data
          ),
          'inventoryObservedAt',COALESCE(
            current.raw_data->'inventoryObservedAt',
            current.raw_data#>'{{latestInventoryObservation,inventoryObservedAt}}'
          )
        )
      END
    ) || jsonb_build_object(
      'cushmanIdentityRepair',jsonb_build_object(
        'generationId',{sql_lit(EXPECTED_GENERATION)},
        'canonicalExternalId',r.target_id,
        'canonicalListingId',s.survivor_id,
        'disposition',CASE WHEN r.id=s.survivor_id
          THEN 'canonical_survivor' ELSE 'superseded_duplicate' END,
        'repairToken',{sql_lit(REPAIR_TOKEN)}
      )
    ),
    'source_lastmod',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.source_lastmod,original.source_lastmod)
      ELSE original.source_lastmod END,
    'canonical_key',CASE WHEN r.id=s.survivor_id AND current.id IS NOT NULL
      THEN COALESCE(current.canonical_key,original.canonical_key)
      ELSE original.canonical_key END,
    'deleted_at_static',CASE WHEN r.id<>s.survivor_id
      THEN original.deleted_at ELSE NULL END,
    'deleted_at_uses_apply_timestamp',
      r.id<>s.survivor_id AND original.deleted_at IS NULL
  ) AS post_state
FROM _cw_rows r
JOIN _cw_survivors s USING(target_id)
JOIN credeals.cre_listings original ON original.id=r.id
LEFT JOIN _cw_current current USING(target_id);
SELECT jsonb_build_object(
  'ok',true,'mode','rollback_only_preflight',
  'artifactSha256',{sql_lit(EXPECTED_ARTIFACT_SHA256)},
  'generation',{sql_lit(EXPECTED_GENERATION)},
  'activeBefore',{EXPECTED_ACTIVE_ROWS},
  'canonicalTargets',{EXPECTED_TARGETS},
  'activeAliases',{EXPECTED_ACTIVE_ALIASES},
  'allAliases',{EXPECTED_ALL_ALIASES},
  'omFacts',{EXPECTED_OM_FACTS},
  'omUniqueAfter',{EXPECTED_OM_UNIQUE}
)::text;
ROLLBACK;
"""


def expected_parent_sql(artifact: list[ArtifactRow], state: dict) -> str:
    """Reuse the reviewed expected-parent projection embedded in preflight."""
    rendered = preflight_sql(artifact, state)
    start_marker = "CREATE TEMP TABLE _cw_expected_parents ON COMMIT DROP AS"
    end_marker = (
        "SELECT jsonb_build_object(\n"
        "  'ok',true,'mode','rollback_only_preflight'"
    )
    start = rendered.index(start_marker)
    end = rendered.index(end_marker, start)
    return rendered[start:end]


def preimage_output_select_statements() -> tuple[str, ...]:
    """Return one bounded frontend SELECT per materialized preimage chunk."""
    return tuple(
        f"""SELECT jsonb_build_object(
 'protocol',{sql_lit(PREIMAGE_OUTPUT_PROTOCOL)},
 'seq',{seq},
 'count',o.chunk_count,
 'payloadBytes',o.payload_bytes,
 'payloadMd5',o.payload_md5,
 'encoding',{sql_lit(PREIMAGE_OUTPUT_ENCODING)},
 'chunk',c.chunk
)::text
FROM _cw_preimage_meta o
JOIN _cw_preimage_chunks c ON c.seq={seq}
WHERE {seq}<o.chunk_count;"""
        for seq in range(PREIMAGE_OUTPUT_MAX_CHUNKS)
    )


def preimage_sql(artifact: list[ArtifactRow], state: dict) -> str:
    output_selects = "\n".join(preimage_output_select_statements())

    def moved_child_map(table: str, alias: str) -> str:
        return f"""(
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id',{alias}.id,'listing_id',{alias}.listing_id,
            'post_listing_id',s.survivor_id
          ) ORDER BY {alias}.id),'[]'::jsonb)
          FROM credeals.{table} {alias}
          JOIN _cw_rows r ON r.id={alias}.listing_id
          JOIN _cw_survivors s USING(target_id)
        )"""

    def unchanged_child_map(table: str, alias: str) -> str:
        return f"""(
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id',{alias}.id,'listing_id',{alias}.listing_id,
            'post_listing_id',{alias}.listing_id
          ) ORDER BY {alias}.id),'[]'::jsonb)
          FROM credeals.{table} {alias}
          JOIN _cw_rows r ON r.id={alias}.listing_id
        )"""

    image_map = """(
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id',ranked.id,'listing_id',ranked.listing_id,
        'url',ranked.url,
        'post_listing_id',CASE WHEN ranked.child_rank=1
          THEN ranked.survivor_id ELSE ranked.listing_id END
      ) ORDER BY ranked.id),'[]'::jsonb)
      FROM (
        SELECT i.id,i.listing_id,i.url,s.survivor_id,
          row_number() OVER (
            PARTITION BY r.target_id,i.url
            ORDER BY CASE WHEN i.listing_id=s.survivor_id THEN 0 ELSE 1 END,i.id
          ) AS child_rank
        FROM credeals.cre_listing_images i
        JOIN _cw_rows r ON r.id=i.listing_id
        JOIN _cw_survivors s USING(target_id)
      ) ranked
    )"""
    om_map = """(
      SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id',ranked.id,'listing_id',ranked.listing_id,
        'fact_group',ranked.fact_group,'fact_key',ranked.fact_key,
        'source_doc_url',ranked.source_doc_url,
        'parser_version',ranked.parser_version,'parsed_at',ranked.parsed_at,
        'post_listing_id',CASE WHEN ranked.fact_rank=1
          THEN s.survivor_id ELSE ranked.listing_id END
      ) ORDER BY ranked.id),'[]'::jsonb)
      FROM _cw_om_ranked ranked
      JOIN _cw_survivors s USING(target_id)
    )"""
    return f"""
BEGIN ISOLATION LEVEL REPEATABLE READ;
SET LOCAL statement_timeout='5min';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{pgcrypto_preflight_sql()}
{stage_sql(artifact,state)}
{invariant_sql()}
{expected_parent_sql(artifact,state)}
\\warn cre-cushman-phase: staged-reviewed-state
CREATE TEMP TABLE _cw_preimage_inner ON COMMIT DROP AS
WITH inner_source AS MATERIALIZED (
 SELECT jsonb_build_object(
  'schemaVersion',{PREIMAGE_INNER_SCHEMA_VERSION},
  'repairPlan',(
    SELECT jsonb_agg(
      (to_jsonb(p)-'post_state')||jsonb_build_object(
        'post_state',p.post_state-'raw_data_base',
        'post_raw_data_bytes',octet_length(
          convert_to((p.post_state->'raw_data_base')::text,'UTF8')
        ),
        'post_raw_data_sha256',encode(
          digest(
            convert_to((p.post_state->'raw_data_base')::text,'UTF8'),
            'sha256'
          ),
          'hex'
        )
      )
      ORDER BY p.id
    )
    FROM _cw_expected_parents p
  ),
  'listings',(
    SELECT jsonb_agg(jsonb_build_object(
      'id',l.id,'external_id',l.external_id,'source_url',l.source_url,
      'canonical_url',l.canonical_url,'status',l.status,
      'transaction_type',l.transaction_type,'property_type',l.property_type,
      'title',l.title,'address',l.address,'city',l.city,'state',l.state,
      'zip',l.zip,'country',l.country,'lat',l.lat,'lng',l.lng,
      'scraped_at',l.scraped_at,'raw_data',l.raw_data,
      'source_lastmod',l.source_lastmod,'canonical_key',l.canonical_key,
      'deleted_at',l.deleted_at,'updated_at',l.updated_at
    ) ORDER BY l.id)
    FROM credeals.cre_listings l JOIN _cw_rows r ON r.id=l.id
  ),
  'contacts',{moved_child_map("cre_listing_contacts","c")},
  'documents',{moved_child_map("cre_listing_documents","d")},
  'images',{image_map},
  'media',{unchanged_child_map("cre_listing_media","m")},
  'links',{unchanged_child_map("cre_listing_links","k")},
  'omFacts',{om_map},
  'events',{moved_child_map("cre_listing_events","e")},
  'priceHistory',{unchanged_child_map("cre_listing_price_history","h")},
  'scrapeLogs',{unchanged_child_map("cre_scrape_log","g")},
  'sourceIndex',(
    SELECT jsonb_agg(to_jsonb(si) ORDER BY si.id)
    FROM credeals.cre_source_index si JOIN _cw_si_plan p ON p.id=si.id
  ),
  'queue',(
    SELECT jsonb_agg(to_jsonb(q) ORDER BY q.id)
    FROM credeals.cre_enrichment_queue q JOIN _cw_queue_plan p ON p.id=q.id
  )
 )::text AS plaintext
)
SELECT
 plaintext,
 octet_length(plaintext)::bigint AS plaintext_bytes,
 encode(digest(convert_to(plaintext,'UTF8'),'sha256'),'hex')
   AS plaintext_sha256,
 pgp_sym_encrypt(
   plaintext,
   {sql_lit(PREIMAGE_COMPRESSION_PASSPHRASE)},
   {sql_lit(PREIMAGE_COMPRESSION_PGP_OPTIONS)}
 ) AS packed
FROM inner_source;
\\warn cre-cushman-phase: built-encrypted-inner
CREATE TEMP TABLE _cw_preimage_inner_readback ON COMMIT DROP AS
SELECT pgp_sym_decrypt(
  packed,{sql_lit(PREIMAGE_COMPRESSION_PASSPHRASE)}
) AS plaintext
FROM _cw_preimage_inner;
\\warn cre-cushman-phase: decrypted-inner-readback
CREATE TEMP TABLE _cw_preimage_inner_json ON COMMIT DROP AS
SELECT plaintext::jsonb AS payload FROM _cw_preimage_inner_readback;
\\warn cre-cushman-phase: parsed-inner-json
CREATE TEMP TABLE _cw_preimage_inner_sections ON COMMIT DROP AS
SELECT section.key,section.value
FROM _cw_preimage_inner_json i
CROSS JOIN LATERAL jsonb_each(i.payload) section;
CREATE UNIQUE INDEX ON _cw_preimage_inner_sections(key);
\\warn cre-cushman-phase: materialized-inner-sections
DO $cw_preimage_inner$
DECLARE
 mismatch integer;
BEGIN
 IF (SELECT count(*) FROM _cw_preimage_inner)<>1
    OR (SELECT count(*) FROM _cw_preimage_inner_readback)<>1
    OR (SELECT count(*) FROM _cw_preimage_inner_json)<>1
    OR (SELECT count(*) FROM _cw_preimage_inner_sections)
      <>{len(PREIMAGE_INNER_SECTION_KEYS)} THEN
   RAISE EXCEPTION 'Cushman inner preimage row count mismatch';
 END IF;
 IF EXISTS (
   SELECT 1
   FROM (
     VALUES
       {inner_section_values_sql()}
   ) expected(key)
   FULL JOIN _cw_preimage_inner_sections actual USING(key)
   WHERE expected.key IS NULL OR actual.key IS NULL
 ) THEN
   RAISE EXCEPTION 'Cushman inner preimage schema/count mismatch';
 END IF;
 IF (
   SELECT plaintext_bytes<=0
       OR plaintext_bytes>{MAX_INNER_PREIMAGE_BYTES}
       OR plaintext_sha256!~'^[0-9a-f]{{64}}$'
       OR octet_length(packed)<=0
       OR plaintext IS DISTINCT FROM (
         SELECT plaintext FROM _cw_preimage_inner_readback
       )
       OR octet_length(convert_to(
         (SELECT plaintext FROM _cw_preimage_inner_readback),'UTF8'
       )) IS DISTINCT FROM plaintext_bytes
       OR encode(digest(convert_to(
         (SELECT plaintext FROM _cw_preimage_inner_readback),'UTF8'
       ),'sha256'),'hex') IS DISTINCT FROM plaintext_sha256
       OR plaintext IS DISTINCT FROM (
         SELECT payload::text FROM _cw_preimage_inner_json
       )
   FROM _cw_preimage_inner
 ) THEN
   RAISE EXCEPTION 'Cushman inner preimage compression/integrity mismatch';
 END IF;
 IF ((SELECT value FROM _cw_preimage_inner_sections
      WHERE key='schemaVersion')#>>'{{}}')::integer
      <>{PREIMAGE_INNER_SCHEMA_VERSION}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='repairPlan'))
      <>{EXPECTED_TOTAL_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='listings'))
      <>{EXPECTED_TOTAL_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='contacts'))
      <>{EXPECTED_CONTACT_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='documents'))
      <>{EXPECTED_DOCUMENT_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='images'))
      <>{EXPECTED_IMAGE_ROWS}
    OR (SELECT value FROM _cw_preimage_inner_sections WHERE key='media')
      <>'[]'::jsonb
    OR (SELECT value FROM _cw_preimage_inner_sections WHERE key='links')
      <>'[]'::jsonb
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='omFacts'))
      <>{EXPECTED_OM_FACTS}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='events'))
      <>{EXPECTED_EVENT_ROWS}
    OR (SELECT value FROM _cw_preimage_inner_sections
        WHERE key='priceHistory')<>'[]'::jsonb
    OR (SELECT value FROM _cw_preimage_inner_sections
        WHERE key='scrapeLogs')<>'[]'::jsonb
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='sourceIndex'))
      <>{EXPECTED_SOURCE_INDEX_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_preimage_inner_sections WHERE key='queue'))
      <>{EXPECTED_QUEUE_ROWS}
 THEN
   RAISE EXCEPTION 'Cushman inner preimage schema/count mismatch';
 END IF;
 SELECT count(*) INTO mismatch
 FROM jsonb_to_recordset((SELECT value
      FROM _cw_preimage_inner_sections WHERE key='listings')) x(id uuid)
 FULL JOIN _cw_rows r USING(id)
 WHERE x.id IS NULL OR r.id IS NULL;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman inner listing identity mismatch: %',mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM jsonb_to_recordset((SELECT value
      FROM _cw_preimage_inner_sections WHERE key='repairPlan')) x(id uuid)
 FULL JOIN _cw_expected_parents p USING(id)
 WHERE x.id IS NULL OR p.id IS NULL;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman inner repair-plan identity mismatch: %',mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM jsonb_to_recordset((SELECT value
      FROM _cw_preimage_inner_sections WHERE key='sourceIndex')) x(id uuid)
 FULL JOIN _cw_si_plan p USING(id)
 WHERE x.id IS NULL OR p.id IS NULL;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman inner source-index identity mismatch: %',mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM jsonb_to_recordset((SELECT value
      FROM _cw_preimage_inner_sections WHERE key='queue')) x(id uuid)
 FULL JOIN _cw_queue_plan p USING(id)
 WHERE x.id IS NULL OR p.id IS NULL;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman inner queue identity mismatch: %',mismatch;
 END IF;
END
$cw_preimage_inner$;
\\warn cre-cushman-phase: validated-inner
DROP TABLE _cw_preimage_inner_sections,_cw_preimage_inner_json,
 _cw_preimage_inner_readback;
CREATE TEMP TABLE _cw_preimage_output ON COMMIT DROP AS
WITH source_payload AS MATERIALIZED (
 SELECT jsonb_build_object(
  'schemaVersion',6,
  'capturedAt',clock_timestamp(),
  'applyTimestampBinding',
    'updated_at=raw_data.cushmanIdentityRepair.appliedAt=transaction_timestamp',
  'innerSchemaVersion',{PREIMAGE_INNER_SCHEMA_VERSION},
  'innerEncoding',{sql_lit(PREIMAGE_INNER_ENCODING)},
  'artifactSha256',{sql_lit(EXPECTED_ARTIFACT_SHA256)},
  'databaseTargetSha256',{sql_lit(EXPECTED_DB_TARGET_SHA256)},
  'generation',{sql_lit(EXPECTED_GENERATION)},
  'geometrySha256',{sql_lit(EXPECTED_GEOMETRY_SHA256)},
  'innerPayloadBytes',i.plaintext_bytes,
  'innerPayloadSha256',i.plaintext_sha256,
  'innerPayloadPgpBase64',replace(encode(i.packed,'base64'),chr(10),''),
  'innerCounts',jsonb_build_object(
    'repairPlan',{EXPECTED_TOTAL_ROWS},'listings',{EXPECTED_TOTAL_ROWS},
    'contacts',{EXPECTED_CONTACT_ROWS},'documents',{EXPECTED_DOCUMENT_ROWS},
    'images',{EXPECTED_IMAGE_ROWS},'media',0,'links',0,
    'omFacts',{EXPECTED_OM_FACTS},'events',{EXPECTED_EVENT_ROWS},
    'priceHistory',0,'scrapeLogs',0,
    'sourceIndex',{EXPECTED_SOURCE_INDEX_ROWS},'queue',{EXPECTED_QUEUE_ROWS}
  ),
  'artifactPlan',(
    SELECT jsonb_agg(to_jsonb(a) ORDER BY a.provider_id) FROM _cw_artifact a
  ),
  'repairTopology',(
    SELECT jsonb_agg(jsonb_build_object(
      'id',p.id,'target_id',p.target_id,'survivor_id',p.survivor_id,
      'has_current',p.has_current,'post_external_id',p.post_external_id,
      'post_source_url',p.post_source_url,'post_deleted',p.post_deleted,
      'post_generation',p.post_generation
    ) ORDER BY p.id)
    FROM _cw_expected_parents p
  ),
  'stateListings',(
    SELECT jsonb_agg(jsonb_build_object(
      'id',r.id,'external_id',r.external_id,'source_url',r.source_url,
      'deleted',r.deleted,'generation',r.generation,
      'updated_at',r.updated_at
    ) ORDER BY r.id) FROM _cw_rows r
  ),
  'stateSourceIndex',(
    SELECT jsonb_agg(jsonb_build_object(
      'id',p.id,'external_id',p.external_id,'url',p.url,
      'last_seen',p.last_seen,'last_enumerated_at',p.last_enumerated_at
    ) ORDER BY p.id) FROM _cw_si_plan p
  ),
  'stateQueue',(
    SELECT jsonb_agg(jsonb_build_object(
      'id',p.id,'external_id',p.external_id,'url',p.url,
      'reason',p.reason,'claimed',p.claimed,'enqueued_at',p.enqueued_at,
      'claimed_at',p.claimed_at,'done_at',p.done_at,'attempts',p.attempts
    ) ORDER BY p.id) FROM _cw_queue_plan p
  )
)::text AS payload
 FROM _cw_preimage_inner i
),
encoded_payload AS MATERIALIZED (
 SELECT payload,
        replace(
          encode(convert_to(payload,'UTF8'),'base64'),
          chr(10),
          ''
        ) AS encoded
 FROM source_payload
)
SELECT octet_length(payload)::bigint AS payload_bytes,
       md5(payload) AS payload_md5,
       encoded,
       (
         length(encoded)+{PREIMAGE_OUTPUT_CHUNK_CHARS - 1}
       )/{PREIMAGE_OUTPUT_CHUNK_CHARS} AS chunk_count
FROM encoded_payload;
\\warn cre-cushman-phase: built-outer-output
DROP TABLE _cw_preimage_inner;
DO $cw_preimage_output$
DECLARE
 row_count integer;
 payload_bytes bigint;
 payload_md5 text;
 encoded_text text;
 chunk_count bigint;
BEGIN
 SELECT count(*) INTO row_count FROM _cw_preimage_output;
 IF row_count<>1 THEN
   RAISE EXCEPTION 'Cushman preimage output row count mismatch';
 END IF;
 SELECT o.payload_bytes,o.payload_md5,o.encoded,o.chunk_count
 INTO payload_bytes,payload_md5,encoded_text,chunk_count
 FROM _cw_preimage_output o;
 IF payload_bytes<=0 OR payload_bytes>{MAX_PREIMAGE_BYTES} THEN
   RAISE EXCEPTION 'Cushman preimage output byte bound exceeded';
 END IF;
 IF payload_md5 !~ '^[0-9a-f]{{32}}$'
    OR length(encoded_text)<=0
    OR length(encoded_text)%4<>0
    OR chunk_count<=0
    OR chunk_count>{PREIMAGE_OUTPUT_MAX_CHUNKS} THEN
   RAISE EXCEPTION 'Cushman preimage output geometry invalid';
 END IF;
 IF octet_length(decode(encoded_text,'base64'))<>payload_bytes
    OR md5(convert_from(decode(encoded_text,'base64'),'UTF8'))<>payload_md5 THEN
   RAISE EXCEPTION 'Cushman preimage output integrity mismatch';
 END IF;
END
$cw_preimage_output$;
\\warn cre-cushman-phase: validated-outer-output
CREATE TEMP TABLE _cw_preimage_chunks(
 seq integer PRIMARY KEY
   CHECK(seq>=0 AND seq<{PREIMAGE_OUTPUT_MAX_CHUNKS}),
 chunk text NOT NULL
   CHECK(octet_length(chunk) BETWEEN 1 AND {PREIMAGE_OUTPUT_CHUNK_CHARS})
) ON COMMIT DROP;
DO $cw_preimage_chunks$
DECLARE
 encoded_bytes bytea;
 chunk_count integer;
 chunk_seq integer;
 chunk_text text;
BEGIN
 SELECT convert_to(o.encoded,'UTF8'),o.chunk_count
 INTO encoded_bytes,chunk_count
 FROM _cw_preimage_output o;
 IF encoded_bytes IS NULL
    OR chunk_count<=0
    OR chunk_count>{PREIMAGE_OUTPUT_MAX_CHUNKS} THEN
   RAISE EXCEPTION 'Cushman preimage chunk materialization invalid';
 END IF;
 FOR chunk_seq IN 0..chunk_count-1 LOOP
   chunk_text:=convert_from(
     substring(
       encoded_bytes
       FROM chunk_seq*{PREIMAGE_OUTPUT_CHUNK_CHARS}+1
       FOR {PREIMAGE_OUTPUT_CHUNK_CHARS}
     ),
     'UTF8'
   );
   INSERT INTO _cw_preimage_chunks(seq,chunk)
   VALUES(chunk_seq,chunk_text);
 END LOOP;
 IF (
   SELECT count(*) IS DISTINCT FROM chunk_count
       OR min(c.seq) IS DISTINCT FROM 0
       OR max(c.seq) IS DISTINCT FROM chunk_count-1
       OR bool_or(
         CASE
           WHEN c.seq<chunk_count-1
             THEN octet_length(c.chunk)<>{PREIMAGE_OUTPUT_CHUNK_CHARS}
           ELSE NOT (
             octet_length(c.chunk)
               BETWEEN 1 AND {PREIMAGE_OUTPUT_CHUNK_CHARS}
           )
         END
       )
   FROM _cw_preimage_chunks c
 ) THEN
   RAISE EXCEPTION 'Cushman preimage chunk materialization invalid';
 END IF;
END
$cw_preimage_chunks$;
\\warn cre-cushman-phase: materialized-output-chunks
CREATE TEMP TABLE _cw_preimage_meta ON COMMIT DROP AS
SELECT payload_bytes,payload_md5,chunk_count FROM _cw_preimage_output;
DROP TABLE _cw_preimage_output;
{output_selects}
DO $cw_preimage_complete$
BEGIN
 IF (SELECT count(*) FROM _cw_preimage_chunks)
      IS DISTINCT FROM
    (SELECT chunk_count FROM _cw_preimage_meta) THEN
   RAISE EXCEPTION 'Cushman preimage output completion guard failed';
 END IF;
END
$cw_preimage_complete$;
SELECT jsonb_build_object(
 'protocol',{sql_lit(PREIMAGE_OUTPUT_PROTOCOL)},
 'seq',o.chunk_count,
 'count',o.chunk_count,
 'payloadBytes',o.payload_bytes,
 'payloadMd5',o.payload_md5,
 'encoding',{sql_lit(PREIMAGE_OUTPUT_ENCODING)},
 'chunk',''
)::text
FROM _cw_preimage_meta o;
ROLLBACK;
"""


def apply_body(artifact: list[ArtifactRow], state: dict) -> str:
    return f"""
SET LOCAL statement_timeout='8min';
SET LOCAL lock_timeout='15s';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{pgcrypto_preflight_sql()}
{stage_sql(artifact,state)}
{invariant_sql()}
{expected_parent_sql(artifact,state)}

SELECT 1 FROM credeals.cre_listings l JOIN _cw_rows r ON r.id=l.id FOR UPDATE;

UPDATE credeals.cre_listings l
SET external_id='cushman-migration:v1:'||md5(l.id::text)
FROM _cw_rows r WHERE r.id=l.id;

UPDATE credeals.cre_listings survivor
SET source_url=current.source_url,
    canonical_url=COALESCE(current.canonical_url,current.source_url,
                           survivor.canonical_url),
    status=CASE
      WHEN survivor.deleted_at IS NOT NULL AND survivor.status='inactive'
        THEN 'active'
      WHEN current.status IN ('sold','leased','off_market',
                              'under_contract','pending')
       AND NOT (survivor.status IN ('sold','leased','off_market')
                AND current.status IN ('under_contract','pending'))
        THEN current.status
      ELSE survivor.status
    END,
    transaction_type=CASE
      WHEN survivor.transaction_type='sale_or_lease'
        OR current.transaction_type='sale_or_lease' THEN 'sale_or_lease'
      WHEN survivor.transaction_type IS NULL THEN current.transaction_type
      WHEN current.transaction_type IS NOT NULL
       AND current.transaction_type IS DISTINCT FROM survivor.transaction_type
        THEN 'sale_or_lease'
      ELSE survivor.transaction_type
    END,
    property_type=COALESCE(current.property_type,survivor.property_type),
    title=COALESCE(current.title,survivor.title),
    address=COALESCE(current.address,survivor.address),
    city=COALESCE(current.city,survivor.city),
    state=COALESCE(current.state,survivor.state),
    zip=COALESCE(current.zip,survivor.zip),
    country=COALESCE(current.country,survivor.country),
    lat=COALESCE(current.lat,survivor.lat),
    lng=COALESCE(current.lng,survivor.lng),
    raw_data=(
      CASE
        WHEN jsonb_path_exists(
          survivor.raw_data,
          '$.**.preserveChildCollections ? (@ == true || @ == "true")'
        ) THEN '{{}}'::jsonb
        ELSE COALESCE(survivor.raw_data,'{{}}'::jsonb)
      END
    ) || jsonb_build_object(
      'sourceKey',COALESCE(
        current.raw_data->'sourceKey',
        current.raw_data#>'{{latestInventoryObservation,sourceKey}}',
        survivor.raw_data->'sourceKey'
      ),
      'latestInventoryObservation',COALESCE(
        current.raw_data->'latestInventoryObservation',current.raw_data
      ),
      'inventoryObservedAt',COALESCE(
        current.raw_data->'inventoryObservedAt',
        current.raw_data#>'{{latestInventoryObservation,inventoryObservedAt}}'
      )
    ),
    source_lastmod=COALESCE(current.source_lastmod,survivor.source_lastmod),
    canonical_key=COALESCE(current.canonical_key,survivor.canonical_key),
    deleted_at=NULL,
    updated_at=clock_timestamp()
FROM _cw_survivors s
JOIN _cw_current current USING(target_id)
WHERE survivor.id=s.survivor_id;

UPDATE credeals.cre_listings survivor
SET external_id=s.target_id,
    canonical_url=COALESCE(survivor.canonical_url,survivor.source_url),
    raw_data=COALESCE(survivor.raw_data,'{{}}'::jsonb)||jsonb_build_object(
      'cushmanIdentityRepair',jsonb_build_object(
        'generationId',{sql_lit(EXPECTED_GENERATION)},
        'canonicalExternalId',s.target_id,
        'canonicalListingId',s.survivor_id,
        'disposition','canonical_survivor',
        'repairToken',{sql_lit(REPAIR_TOKEN)},
        'appliedAt',transaction_timestamp()
      )
    ),
    deleted_at=NULL,
    updated_at=transaction_timestamp()
FROM _cw_survivors s WHERE survivor.id=s.survivor_id;

UPDATE credeals.cre_listings alias
SET external_id=a.superseded_id,
    deleted_at=COALESCE(alias.deleted_at,transaction_timestamp()),
    raw_data=COALESCE(alias.raw_data,'{{}}'::jsonb)||jsonb_build_object(
      'cushmanIdentityRepair',jsonb_build_object(
        'generationId',{sql_lit(EXPECTED_GENERATION)},
        'canonicalExternalId',a.target_id,
        'canonicalListingId',a.survivor_id,
        'disposition','superseded_duplicate',
        'repairToken',{sql_lit(REPAIR_TOKEN)},
        'appliedAt',transaction_timestamp()
      )
    ),
    updated_at=transaction_timestamp()
FROM _cw_aliases a WHERE alias.id=a.id;

UPDATE credeals.cre_listing_contacts x SET listing_id=s.survivor_id
FROM _cw_rows r JOIN _cw_survivors s USING(target_id)
WHERE x.listing_id=r.id AND x.listing_id<>s.survivor_id;
UPDATE credeals.cre_listing_documents x SET listing_id=s.survivor_id
FROM _cw_rows r JOIN _cw_survivors s USING(target_id)
WHERE x.listing_id=r.id AND x.listing_id<>s.survivor_id;
UPDATE credeals.cre_listing_events x SET listing_id=s.survivor_id
FROM _cw_rows r JOIN _cw_survivors s USING(target_id)
WHERE x.listing_id=r.id AND x.listing_id<>s.survivor_id;

CREATE TEMP TABLE _cw_image_rank ON COMMIT DROP AS
SELECT i.id,i.listing_id,s.survivor_id,
 row_number() OVER (
   PARTITION BY r.target_id,i.url
   ORDER BY CASE WHEN i.listing_id=s.survivor_id THEN 0 ELSE 1 END,i.id
 ) AS child_rank
FROM credeals.cre_listing_images i
JOIN _cw_rows r ON r.id=i.listing_id
JOIN _cw_survivors s USING(target_id);
UPDATE credeals.cre_listing_images i SET listing_id=p.survivor_id
FROM _cw_image_rank p
WHERE i.id=p.id AND p.child_rank=1 AND i.listing_id<>p.survivor_id;

UPDATE credeals.cre_listing_om_facts f SET listing_id=s.survivor_id
FROM _cw_om_ranked p JOIN _cw_survivors s USING(target_id)
WHERE f.id=p.id AND p.fact_rank=1 AND f.listing_id<>s.survivor_id;

UPDATE credeals.cre_source_index si
SET external_id='cushman-si-migration:v1:'||md5(si.id::text)
FROM _cw_si_plan p WHERE p.id=si.id;
CREATE TEMP TABLE _cw_si_rank ON COMMIT DROP AS
SELECT p.target_id,si.*,
 row_number() OVER (
   PARTITION BY p.target_id
   ORDER BY {SOURCE_INDEX_DONOR_ORDER_SQL}
 ) AS source_rank
FROM _cw_si_plan p JOIN credeals.cre_source_index si ON si.id=p.id;
UPDATE credeals.cre_source_index winner
SET external_id=r.target_id,
    source_key=r.source_key,
    url=r.url,
    source_lastmod=r.source_lastmod,
    fingerprint=r.fingerprint,
    soft_deleted=r.soft_deleted,
    observed_status=r.observed_status,
    first_seen=r.first_seen,
    last_seen=r.last_seen,
    last_enumerated_at=r.last_enumerated_at,
    prior_sale_price=r.prior_sale_price,
    prior_lease_rate=r.prior_lease_rate,
    prior_status=r.prior_status
FROM _cw_si_rank r
WHERE r.source_rank=1 AND winner.id=r.id;
DELETE FROM credeals.cre_source_index loser
USING _cw_si_rank r WHERE r.source_rank>1 AND loser.id=r.id;

UPDATE credeals.cre_enrichment_queue q
SET external_id='cushman-q-migration:v1:'||md5(q.id::text)
FROM _cw_queue_plan p WHERE p.id=q.id;
UPDATE credeals.cre_enrichment_queue q
SET external_id=p.target_id,url=survivor.source_url
FROM _cw_queue_plan p
JOIN _cw_survivors s ON s.target_id=p.target_id
JOIN credeals.cre_listings survivor ON survivor.id=s.survivor_id
WHERE q.id=p.id;

DO $post$
DECLARE
  brokerage uuid;
BEGIN
  SELECT id INTO brokerage FROM credeals.cre_brokerages
  WHERE slug='cushman-wakefield';
  IF (SELECT count(*) FROM credeals.cre_listings
      WHERE brokerage_id=brokerage AND deleted_at IS NULL)<>{EXPECTED_TARGETS}
     OR EXISTS (
       SELECT 1 FROM credeals.cre_listings l
       WHERE l.brokerage_id=brokerage AND l.deleted_at IS NULL
         AND (
           l.external_id!~'^url:v1:[0-9a-f]{{32}}$'
           OR NOT EXISTS (
             SELECT 1 FROM _cw_survivors s
             WHERE s.target_id=l.external_id AND s.survivor_id=l.id
           )
         )
     )
     OR (SELECT count(*) FROM credeals.cre_source_index
         WHERE brokerage_id=brokerage)<>{EXPECTED_SOURCE_INDEX_TARGETS}
     OR (SELECT count(*) FROM credeals.cre_enrichment_queue
         WHERE brokerage_id=brokerage)<>{EXPECTED_QUEUE_ROWS}
     OR (SELECT count(*) FROM credeals.cre_listing_om_facts f
         JOIN credeals.cre_listings l ON l.id=f.listing_id
         WHERE l.brokerage_id=brokerage AND l.deleted_at IS NULL)
        <>{EXPECTED_OM_UNIQUE}
     OR EXISTS (
       SELECT 1
       FROM _cw_expected_parents p
       FULL JOIN (
         SELECT * FROM credeals.cre_listings WHERE brokerage_id=brokerage
       ) l ON l.id=p.id
       WHERE p.id IS NULL OR l.id IS NULL
          OR l.raw_data #>> '{{cushmanIdentityRepair,repairToken}}'
            IS DISTINCT FROM {sql_lit(REPAIR_TOKEN)}
          OR l.updated_at IS DISTINCT FROM (
            l.raw_data #>> '{{cushmanIdentityRepair,appliedAt}}'
          )::timestamptz
          OR (
            COALESCE(
              (p.post_state->>'deleted_at_uses_apply_timestamp')::boolean,
              false
            )
            AND l.deleted_at IS DISTINCT FROM l.updated_at
          )
          OR jsonb_build_object(
            'external_id',l.external_id,
            'source_url',l.source_url,
            'canonical_url',l.canonical_url,
            'status',l.status,
            'transaction_type',l.transaction_type,
            'property_type',l.property_type,
            'title',l.title,
            'address',l.address,
            'city',l.city,
            'state',l.state,
            'zip',l.zip,
            'country',l.country,
            'lat',l.lat,
            'lng',l.lng,
            'scraped_at',l.scraped_at,
            'raw_data_base',
              l.raw_data #- '{{cushmanIdentityRepair,appliedAt}}',
            'source_lastmod',l.source_lastmod,
            'canonical_key',l.canonical_key,
            'deleted_at_static',CASE WHEN COALESCE(
              (p.post_state->>'deleted_at_uses_apply_timestamp')::boolean,
              false
            ) THEN NULL ELSE l.deleted_at END,
            'deleted_at_uses_apply_timestamp',COALESCE(
              (p.post_state->>'deleted_at_uses_apply_timestamp')::boolean,
              false
            )
          ) IS DISTINCT FROM p.post_state
     )
  THEN RAISE EXCEPTION 'Cushman repair postcondition failed'; END IF;
END
$post$;

SELECT jsonb_build_object(
 'ok',true,'mode','applied','generation',{sql_lit(EXPECTED_GENERATION)},
 'activeAfter',{EXPECTED_TARGETS},'newlySuperseded',{EXPECTED_ACTIVE_ALIASES},
 'totalParentsPreserved',{EXPECTED_TOTAL_ROWS},
 'activeOmFacts',{EXPECTED_OM_UNIQUE},
 'sourceIndexAfter',{EXPECTED_SOURCE_INDEX_TARGETS},
 'queueAfter',{EXPECTED_QUEUE_ROWS}
)::text;
"""


def build_apply_sql(artifact: list[ArtifactRow], state: dict) -> str:
    return "BEGIN ISOLATION LEVEL SERIALIZABLE;\n" + apply_body(artifact, state) + "\nCOMMIT;\n"


def validate_child_dispositions(
    payload: dict,
    listing_targets: dict[str, str],
    survivor_by_target: dict[str, str],
) -> None:
    direct_to_survivor = {"contacts", "documents", "events"}
    unchanged = {"media", "links", "priceHistory", "scrapeLogs"}
    child_keys = (
        "contacts",
        "documents",
        "images",
        "media",
        "links",
        "omFacts",
        "events",
        "priceHistory",
        "scrapeLogs",
    )
    for key in child_keys:
        rows = payload[key]
        row_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not {
                "id",
                "listing_id",
                "post_listing_id",
            }.issubset(row):
                raise ValueError(f"rollback preimage {key} row is invalid")
            row_id = row["id"]
            listing_id = row["listing_id"]
            post_listing_id = row["post_listing_id"]
            if (
                not isinstance(row_id, str)
                or not row_id
                or row_id in row_ids
                or not isinstance(listing_id, str)
                or not isinstance(post_listing_id, str)
                or listing_id not in listing_targets
                or post_listing_id not in listing_targets
                or listing_targets[listing_id]
                != listing_targets[post_listing_id]
            ):
                raise ValueError(f"rollback preimage {key} identity is invalid")
            target_id = listing_targets[listing_id]
            if key in direct_to_survivor and (
                post_listing_id != survivor_by_target[target_id]
            ):
                raise ValueError(
                    f"rollback preimage {key} disposition is invalid"
                )
            if key in unchanged and post_listing_id != listing_id:
                raise ValueError(
                    f"rollback preimage {key} disposition is invalid"
                )
            row_ids.add(row_id)

    image_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in payload["images"]:
        url = row.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("rollback preimage images evidence is invalid")
        target_id = listing_targets[row["listing_id"]]
        image_groups[(target_id, url)].append(row)
    for (target_id, _), rows in image_groups.items():
        survivor_id = survivor_by_target[target_id]
        winner = min(
            rows,
            key=lambda row: (
                row["listing_id"] != survivor_id,
                row["id"],
            ),
        )
        for row in rows:
            expected = (
                survivor_id if row["id"] == winner["id"] else row["listing_id"]
            )
            if row["post_listing_id"] != expected:
                raise ValueError(
                    "rollback preimage images disposition is invalid"
                )

    om_groups: dict[tuple[object, ...], list[dict]] = defaultdict(list)
    for row in payload["omFacts"]:
        required = {
            "fact_group",
            "fact_key",
            "source_doc_url",
            "parser_version",
            "parsed_at",
        }
        if not required.issubset(row) or any(
            value is not None and not isinstance(value, str)
            for value in (
                row["fact_group"],
                row["fact_key"],
                row["source_doc_url"],
                row["parser_version"],
                row["parsed_at"],
            )
        ):
            raise ValueError("rollback preimage omFacts evidence is invalid")
        target_id = listing_targets[row["listing_id"]]
        identity = (
            target_id,
            row["fact_group"],
            row["fact_key"],
            row["source_doc_url"],
            row["parser_version"],
        )
        om_groups[identity].append(row)

    def parsed_rank(value: str | None) -> tuple[int, float]:
        if value is None:
            return (1, 0.0)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("rollback preimage omFacts parsed_at lacks timezone")
        return (0, -parsed.timestamp())

    for identity, rows in om_groups.items():
        survivor_id = survivor_by_target[str(identity[0])]
        winner = min(
            rows,
            key=lambda row: (parsed_rank(row["parsed_at"]), row["id"]),
        )
        for row in rows:
            expected = (
                survivor_id if row["id"] == winner["id"] else row["listing_id"]
            )
            if row["post_listing_id"] != expected:
                raise ValueError(
                    "rollback preimage omFacts disposition is invalid"
                )


def validate_preimage(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("rollback preimage is not an object")
    expected = {
        "schemaVersion": 6,
        "applyTimestampBinding": (
            "updated_at=raw_data.cushmanIdentityRepair.appliedAt="
            "transaction_timestamp"
        ),
        "innerSchemaVersion": PREIMAGE_INNER_SCHEMA_VERSION,
        "innerEncoding": PREIMAGE_INNER_ENCODING,
        "artifactSha256": EXPECTED_ARTIFACT_SHA256,
        "databaseTargetSha256": EXPECTED_DB_TARGET_SHA256,
        "generation": EXPECTED_GENERATION,
        "geometrySha256": EXPECTED_GEOMETRY_SHA256,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"rollback preimage has unexpected {key}")
    required_keys = {
        "schemaVersion",
        "capturedAt",
        "applyTimestampBinding",
        "innerSchemaVersion",
        "innerEncoding",
        "artifactSha256",
        "databaseTargetSha256",
        "generation",
        "geometrySha256",
        "innerPayloadBytes",
        "innerPayloadSha256",
        "innerPayloadPgpBase64",
        "innerCounts",
        "artifactPlan",
        "repairTopology",
        "stateListings",
        "stateSourceIndex",
        "stateQueue",
    }
    if set(payload) != required_keys:
        raise ValueError("rollback preimage outer schema drifted")
    required = {
        "artifactPlan": EXPECTED_ARTIFACT_ROWS,
        "repairTopology": EXPECTED_TOTAL_ROWS,
        "stateListings": EXPECTED_TOTAL_ROWS,
        "stateSourceIndex": EXPECTED_SOURCE_INDEX_ROWS,
        "stateQueue": EXPECTED_QUEUE_ROWS,
    }
    for key, count in required.items():
        if not isinstance(payload.get(key), list) or len(payload[key]) != count:
            raise ValueError(f"rollback preimage {key} count drifted")
    inner_counts = payload.get("innerCounts")
    if (
        inner_counts != expected_inner_counts()
        or not isinstance(inner_counts, dict)
        or any(type(value) is not int for value in inner_counts.values())
    ):
        raise ValueError("rollback preimage inner counts drifted")
    inner_bytes = payload.get("innerPayloadBytes")
    inner_sha256 = payload.get("innerPayloadSha256")
    inner_base64 = payload.get("innerPayloadPgpBase64")
    try:
        packed = base64.b64decode(inner_base64, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise ValueError("rollback preimage inner envelope is invalid") from exc
    if (
        type(inner_bytes) is not int
        or not 0 < inner_bytes <= MAX_INNER_PREIMAGE_BYTES
        or not isinstance(inner_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", inner_sha256)
        or not packed
        or len(packed) > MAX_PREIMAGE_BYTES
    ):
        raise ValueError("rollback preimage inner envelope is invalid")
    captured = payload.get("capturedAt")
    if not isinstance(captured, str):
        raise ValueError("rollback preimage lacks capturedAt")
    parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("rollback preimage capturedAt lacks timezone")

    def require_records(key: str, required_keys: set[str]) -> list[dict]:
        rows = payload[key]
        for row in rows:
            if not isinstance(row, dict) or set(row) != required_keys:
                raise ValueError(f"rollback preimage {key} row is invalid")
        return rows

    artifact_plan = require_records(
        "artifactPlan",
        {"provider_id", "source_url", "target_id", "transaction_mode"},
    )
    providers: set[str] = set()
    targets: set[str] = set()
    for row in artifact_plan:
        provider_id = row["provider_id"]
        source_url = row["source_url"]
        target_id = row["target_id"]
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or provider_id in providers
            or not isinstance(source_url, str)
            or cushman_canonical_external_id(source_url) != target_id
            or row["transaction_mode"] not in {"sale", "lease"}
        ):
            raise ValueError("rollback preimage artifactPlan identity is invalid")
        providers.add(provider_id)
        targets.add(target_id)
    if EXPECTED_ARTIFACT_ROWS and len(targets) != EXPECTED_ARTIFACT_TARGETS:
        raise ValueError("rollback preimage artifactPlan target count drifted")

    listings = require_records(
        "stateListings",
        {
            "id",
            "external_id",
            "source_url",
            "deleted",
            "generation",
            "updated_at",
        },
    )
    listing_ids: set[str] = set()
    listing_targets: dict[str, str] = {}
    for row in listings:
        listing_id = row["id"]
        if (
            not isinstance(listing_id, str)
            or not listing_id
            or listing_id in listing_ids
            or not isinstance(row["external_id"], str)
            or not isinstance(row["source_url"], str)
            or cushman_canonical_external_id(row["source_url"]) is None
            or not isinstance(row["updated_at"], str)
            or not isinstance(row["deleted"], bool)
            or not (
                row["generation"] is None
                or (
                    isinstance(row["generation"], str)
                    and bool(row["generation"])
                )
            )
        ):
            raise ValueError("rollback preimage listing identity is invalid")
        listing_ids.add(listing_id)
        listing_target = cushman_canonical_external_id(row["source_url"])
        assert listing_target is not None
        listing_targets[listing_id] = listing_target

    repair_plan = require_records(
        "repairTopology",
        {
            "id",
            "target_id",
            "survivor_id",
            "has_current",
            "post_external_id",
            "post_source_url",
            "post_deleted",
            "post_generation",
        },
    )
    repair_ids: set[str] = set()
    plan_by_target: dict[str, list[dict]] = defaultdict(list)
    listings_by_id = {row["id"]: row for row in listings}
    target_has_current: dict[str, bool] = defaultdict(bool)
    for row in listings:
        target_id = cushman_canonical_external_id(row["source_url"])
        assert target_id is not None
        if row["generation"] == EXPECTED_GENERATION:
            target_has_current[target_id] = True
    for row in repair_plan:
        row_id = row["id"]
        target_id = row["target_id"]
        survivor_id = row["survivor_id"]
        if not all(
            isinstance(value, str) and value
            for value in (row_id, target_id, survivor_id)
        ):
            raise ValueError("rollback preimage repairPlan identity is invalid")
        is_survivor = row_id == survivor_id
        original = listings_by_id.get(row_id)
        has_current = target_has_current[target_id]
        expected_generation = (
            EXPECTED_GENERATION
            if is_survivor and has_current
            else original["generation"]
            if original is not None
            else None
        )
        expected_external_id = (
            target_id
            if is_survivor
            else "cushman-superseded:v1:"
            + hashlib.md5(row_id.encode(), usedforsecurity=False).hexdigest()
        )
        if (
            row_id not in listing_ids
            or row_id in repair_ids
            or survivor_id not in listing_ids
            or listings_by_id.get(survivor_id, {}).get("deleted") is not False
            or target_id != listing_targets.get(row_id)
            or target_id != listing_targets.get(survivor_id)
            or not isinstance(row["has_current"], bool)
            or row["has_current"] is not has_current
            or not isinstance(target_id, str)
            or not re.fullmatch(r"url:v1:[0-9a-f]{32}", target_id)
            or row["post_external_id"] != expected_external_id
            or not isinstance(row["post_source_url"], str)
            or cushman_canonical_external_id(row["post_source_url"]) != target_id
            or row["post_deleted"] is not (not is_survivor)
            or row["post_generation"] != expected_generation
            or (
                is_survivor
                and not has_current
                and original is not None
                and row["post_source_url"] != original["source_url"]
            )
        ):
            raise ValueError("rollback preimage repairPlan identity is invalid")
        repair_ids.add(row_id)
        plan_by_target[target_id].append(row)
    if repair_ids != listing_ids:
        raise ValueError("rollback preimage repairPlan coverage drifted")
    for target_id, group in plan_by_target.items():
        survivor_ids = {row["survivor_id"] for row in group}
        if (
            len(survivor_ids) != 1
            or sum(row["id"] == row["survivor_id"] for row in group) != 1
            or next(iter(survivor_ids)) not in {
                row["id"] for row in group
            }
            or any(row["target_id"] != target_id for row in group)
        ):
            raise ValueError("rollback preimage repairPlan survivor topology is invalid")

    for key, required_keys in (
        (
            "stateSourceIndex",
            {
                "id",
                "external_id",
                "url",
                "last_seen",
                "last_enumerated_at",
            },
        ),
        (
            "stateQueue",
            {
                "id",
                "external_id",
                "url",
                "reason",
                "claimed",
                "enqueued_at",
                "claimed_at",
                "done_at",
                "attempts",
            },
        ),
    ):
        rows = require_records(key, required_keys)
        row_ids: set[str] = set()
        target_ids: set[str] = set()
        queue_keys: set[tuple[str, str]] = set()
        for row in rows:
            row_id = row["id"]
            if (
                not isinstance(row_id, str)
                or not row_id
                or row_id in row_ids
                or not isinstance(row["external_id"], str)
                or not isinstance(row["url"], str)
                or cushman_canonical_external_id(row["url"]) is None
            ):
                raise ValueError(f"rollback preimage {key} identity is invalid")
            target_id = cushman_canonical_external_id(row["url"])
            assert target_id is not None
            if key == "stateQueue" and (
                not isinstance(row["reason"], str)
                or row["claimed"] is not False
                or row["claimed_at"] is not None
                or row["done_at"] is not None
            ):
                raise ValueError("rollback preimage queue lifecycle is invalid")
            if key == "stateQueue":
                queue_key = (target_id, row["reason"])
                if queue_key in queue_keys:
                    raise ValueError(
                        "rollback preimage queue identity is invalid"
                    )
                queue_keys.add(queue_key)
            target_ids.add(target_id)
            row_ids.add(row_id)
        if (
            key == "stateSourceIndex"
            and EXPECTED_SOURCE_INDEX_ROWS
            and len(target_ids) != EXPECTED_SOURCE_INDEX_TARGETS
        ):
            raise ValueError(
                "rollback preimage source-index target count drifted"
            )
    try:
        outer_bytes = len(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("rollback preimage outer JSON is invalid") from exc
    if not 0 < outer_bytes <= MAX_PREIMAGE_BYTES:
        raise ValueError("rollback preimage outer size limit exceeded")
    return payload


def artifact_from_preimage(preimage: dict) -> list[ArtifactRow]:
    validate_preimage(preimage)
    return [
        ArtifactRow(
            provider_id=row["provider_id"],
            source_url=row["source_url"],
            target_id=row["target_id"],
            transaction_mode=row["transaction_mode"],
        )
        for row in preimage["artifactPlan"]
    ]


def preimage_json_chunks(preimage: dict) -> tuple[str, ...]:
    """Return validated, byte-bounded ASCII JSON chunks for psql transport."""
    validate_preimage(preimage)
    payload = json.dumps(
        preimage, separators=(",", ":"), ensure_ascii=True
    )
    payload.encode("ascii")
    return tuple(
        payload[start : start + PREIMAGE_SQL_CHUNK_BYTES]
        for start in range(0, len(payload), PREIMAGE_SQL_CHUNK_BYTES)
    )


def preimage_chunk_transport_sql(preimage: dict) -> str:
    """Build fail-closed, bounded statements that assemble `_cw_preimage`."""
    chunks = preimage_json_chunks(preimage)
    payload = "".join(chunks)
    expected_bytes = len(payload.encode("ascii"))
    expected_md5 = hashlib.md5(
        payload.encode("ascii"), usedforsecurity=False
    ).hexdigest()
    inserts = []
    for seq, chunk in enumerate(chunks):
        statement = (
            "INSERT INTO _cw_preimage_chunks(seq,payload) VALUES "
            f"({seq},{sql_lit(chunk)});"
        )
        if (
            len(statement.encode("utf-8"))
            > PREIMAGE_SQL_STATEMENT_CEILING_BYTES
        ):
            raise ValueError("rollback preimage SQL chunk exceeds statement ceiling")
        inserts.append(statement)
    return f"""
CREATE TEMP TABLE _cw_preimage_chunks(
 seq integer PRIMARY KEY CHECK (seq>=0),
 payload text NOT NULL
) ON COMMIT DROP;
{chr(10).join(inserts)}
CREATE TEMP TABLE _cw_preimage_assembled(
 payload text NOT NULL
) ON COMMIT DROP;
INSERT INTO _cw_preimage_assembled(payload)
SELECT string_agg(payload,'' ORDER BY seq)
FROM _cw_preimage_chunks
HAVING count(*)={len(chunks)}
   AND min(seq)=0
   AND max(seq)={len(chunks) - 1};
DO $cw_preimage_transport$
BEGIN
 IF (SELECT count(*) FROM _cw_preimage_assembled)<>1 THEN
   RAISE EXCEPTION 'Cushman rollback preimage chunk geometry mismatch';
 END IF;
 IF (
   SELECT octet_length(payload)<>{expected_bytes}
       OR md5(payload)<>{sql_lit(expected_md5)}
   FROM _cw_preimage_assembled
 ) THEN
   RAISE EXCEPTION 'Cushman rollback preimage payload integrity mismatch';
 END IF;
END
$cw_preimage_transport$;
CREATE TEMP TABLE _cw_preimage(payload jsonb) ON COMMIT DROP;
INSERT INTO _cw_preimage(payload)
SELECT payload::jsonb FROM _cw_preimage_assembled;
DO $cw_preimage_row$
BEGIN
 IF (SELECT count(*) FROM _cw_preimage)<>1 THEN
   RAISE EXCEPTION 'Cushman rollback preimage assembled row count mismatch';
 END IF;
END
$cw_preimage_row$;
DROP TABLE _cw_preimage_chunks,_cw_preimage_assembled;
"""


def rollback_body(preimage: dict, artifact: list[ArtifactRow], state: dict) -> str:
    preimage_transport = preimage_chunk_transport_sql(preimage)
    child_tables = {
        "contacts": "cre_listing_contacts",
        "documents": "cre_listing_documents",
        "images": "cre_listing_images",
        "media": "cre_listing_media",
        "links": "cre_listing_links",
        "omFacts": "cre_listing_om_facts",
        "events": "cre_listing_events",
        "priceHistory": "cre_listing_price_history",
        "scrapeLogs": "cre_scrape_log",
    }
    child_stage = "\n".join(
        f"""
CREATE TEMP TABLE _pre_{key} ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT value FROM _cw_rollback_sections WHERE key={sql_lit(key)})
) AS x(id uuid,listing_id uuid,post_listing_id uuid);
CREATE UNIQUE INDEX ON _pre_{key}(id);
"""
        for key in child_tables
    )
    child_precheck = "\n".join(
        f"""
 SELECT count(*) INTO mismatch
 FROM _pre_{key} p
 FULL JOIN (
   SELECT x.id,x.listing_id
   FROM credeals.{table} x
   WHERE x.listing_id IN (SELECT id FROM _pre_listings)
 ) live ON live.id=p.id
 WHERE p.id IS NULL OR live.id IS NULL
    OR live.listing_id IS DISTINCT FROM p.post_listing_id;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'rollback refused: {key} post-repair mapping drift: %',
     mismatch;
 END IF;
"""
        for key, table in child_tables.items()
    )
    child_restore = "\n".join(
        f"""
UPDATE credeals.{table} target SET listing_id=p.listing_id
FROM _pre_{key} p WHERE target.id=p.id;
"""
        for key, table in child_tables.items()
    )
    child_postcheck = "\n".join(
        f"""
 SELECT count(*) INTO mismatch
 FROM _pre_{key} p
 FULL JOIN (
   SELECT x.id,x.listing_id
   FROM credeals.{table} x
   WHERE x.listing_id IN (SELECT id FROM _pre_listings)
 ) live ON live.id=p.id
 WHERE p.id IS NULL OR live.id IS NULL
    OR live.listing_id IS DISTINCT FROM p.listing_id;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman rollback {key} readback failed: %',mismatch;
 END IF;
"""
        for key, table in child_tables.items()
    )
    return f"""
SET LOCAL statement_timeout='8min';
SET LOCAL lock_timeout='15s';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{pgcrypto_preflight_sql()}
{preimage_transport}
CREATE TEMP TABLE _cw_rollback_clock ON COMMIT DROP AS
SELECT transaction_timestamp() AS trigger_updated_at;
{stage_sql(artifact,state)}

CREATE TEMP TABLE _cw_rollback_inner_text ON COMMIT DROP AS
SELECT
 pgp_sym_decrypt(
   decode(payload->>'innerPayloadPgpBase64','base64'),
   {sql_lit(PREIMAGE_COMPRESSION_PASSPHRASE)}
 ) AS plaintext,
 (payload->>'innerPayloadBytes')::bigint AS expected_bytes,
 payload->>'innerPayloadSha256' AS expected_sha256
FROM _cw_preimage;
CREATE TEMP TABLE _cw_rollback_inner ON COMMIT DROP AS
SELECT plaintext::jsonb AS payload FROM _cw_rollback_inner_text;
CREATE TEMP TABLE _cw_rollback_sections ON COMMIT DROP AS
SELECT section.key,section.value
FROM _cw_rollback_inner i
CROSS JOIN LATERAL jsonb_each(i.payload) section;
CREATE UNIQUE INDEX ON _cw_rollback_sections(key);
DO $cw_rollback_inner$
BEGIN
 IF (SELECT count(*) FROM _cw_rollback_inner_text)<>1
    OR (SELECT count(*) FROM _cw_rollback_inner)<>1
    OR (SELECT count(*) FROM _cw_rollback_sections)
      <>{len(PREIMAGE_INNER_SECTION_KEYS)} THEN
   RAISE EXCEPTION 'Cushman rollback inner payload row count mismatch';
 END IF;
 IF EXISTS (
   SELECT 1
   FROM (
     VALUES
       {inner_section_values_sql()}
   ) expected(key)
   FULL JOIN _cw_rollback_sections actual USING(key)
   WHERE expected.key IS NULL OR actual.key IS NULL
 ) THEN
   RAISE EXCEPTION 'Cushman rollback inner payload schema/count mismatch';
 END IF;
 IF (
   SELECT expected_bytes<=0
       OR expected_bytes>{MAX_INNER_PREIMAGE_BYTES}
       OR expected_sha256!~'^[0-9a-f]{{64}}$'
       OR octet_length(convert_to(plaintext,'UTF8'))
          IS DISTINCT FROM expected_bytes
       OR encode(
         digest(convert_to(plaintext,'UTF8'),'sha256'),'hex'
       ) IS DISTINCT FROM expected_sha256
       OR plaintext IS DISTINCT FROM (
         SELECT payload::text FROM _cw_rollback_inner
       )
   FROM _cw_rollback_inner_text
 ) THEN
   RAISE EXCEPTION 'Cushman rollback inner payload integrity mismatch';
 END IF;
 IF ((SELECT value FROM _cw_rollback_sections
      WHERE key='schemaVersion')#>>'{{}}')::integer
      <>{PREIMAGE_INNER_SCHEMA_VERSION}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='repairPlan'))
      <>{EXPECTED_TOTAL_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='listings'))
      <>{EXPECTED_TOTAL_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='contacts'))
      <>{EXPECTED_CONTACT_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='documents'))
      <>{EXPECTED_DOCUMENT_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='images'))
      <>{EXPECTED_IMAGE_ROWS}
    OR (SELECT value FROM _cw_rollback_sections WHERE key='media')
      <>'[]'::jsonb
    OR (SELECT value FROM _cw_rollback_sections WHERE key='links')
      <>'[]'::jsonb
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='omFacts'))
      <>{EXPECTED_OM_FACTS}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='events'))
      <>{EXPECTED_EVENT_ROWS}
    OR (SELECT value FROM _cw_rollback_sections
        WHERE key='priceHistory')<>'[]'::jsonb
    OR (SELECT value FROM _cw_rollback_sections
        WHERE key='scrapeLogs')<>'[]'::jsonb
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='sourceIndex'))
      <>{EXPECTED_SOURCE_INDEX_ROWS}
    OR jsonb_array_length((SELECT value
          FROM _cw_rollback_sections WHERE key='queue'))
      <>{EXPECTED_QUEUE_ROWS}
 THEN
   RAISE EXCEPTION 'Cushman rollback inner payload schema/count mismatch';
 END IF;
END
$cw_rollback_inner$;
DROP TABLE _cw_rollback_inner_text,_cw_rollback_inner;

CREATE TEMP TABLE _pre_listings ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT value FROM _cw_rollback_sections WHERE key='listings')
) AS x(
 id uuid,external_id text,source_url text,canonical_url text,status text,
 transaction_type text,property_type text,title text,address text,city text,
 state text,zip text,country text,lat double precision,lng double precision,
 scraped_at timestamptz,raw_data jsonb,source_lastmod timestamptz,
 canonical_key text,deleted_at timestamptz,updated_at timestamptz
);
CREATE UNIQUE INDEX ON _pre_listings(id);
CREATE TEMP TABLE _pre_repair_plan ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT value FROM _cw_rollback_sections WHERE key='repairPlan')
) AS x(
 id uuid,target_id text,survivor_id uuid,post_external_id text,
 has_current boolean,post_source_url text,post_deleted boolean,
 post_generation text,post_state jsonb,post_raw_data_bytes bigint,
 post_raw_data_sha256 text
);
CREATE UNIQUE INDEX ON _pre_repair_plan(id);
CREATE TEMP TABLE _pre_outer_topology ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset(
 (SELECT payload->'repairTopology' FROM _cw_preimage)
) AS x(
 id uuid,target_id text,survivor_id uuid,post_external_id text,
 has_current boolean,post_source_url text,post_deleted boolean,
 post_generation text
);
CREATE UNIQUE INDEX ON _pre_outer_topology(id);
{child_stage}
CREATE TEMP TABLE _pre_source_index ON COMMIT DROP AS
SELECT * FROM jsonb_populate_recordset(
 NULL::credeals.cre_source_index,
 (SELECT value FROM _cw_rollback_sections WHERE key='sourceIndex')
);
CREATE UNIQUE INDEX ON _pre_source_index(id);
CREATE TEMP TABLE _pre_queue ON COMMIT DROP AS
SELECT * FROM jsonb_populate_recordset(
 NULL::credeals.cre_enrichment_queue,
 (SELECT value FROM _cw_rollback_sections WHERE key='queue')
);
CREATE UNIQUE INDEX ON _pre_queue(id);
DROP TABLE _cw_rollback_sections;

DO $cw_outer_inner_correlation$
DECLARE mismatch integer;
BEGIN
 IF (
   SELECT payload->>'innerSchemaVersion'
            IS DISTINCT FROM {sql_lit(str(PREIMAGE_INNER_SCHEMA_VERSION))}
       OR payload->>'innerEncoding'
            IS DISTINCT FROM {sql_lit(PREIMAGE_INNER_ENCODING)}
       OR payload->'innerCounts' IS DISTINCT FROM jsonb_build_object(
         'repairPlan',{EXPECTED_TOTAL_ROWS},'listings',{EXPECTED_TOTAL_ROWS},
         'contacts',{EXPECTED_CONTACT_ROWS},
         'documents',{EXPECTED_DOCUMENT_ROWS},'images',{EXPECTED_IMAGE_ROWS},
         'media',0,'links',0,'omFacts',{EXPECTED_OM_FACTS},
         'events',{EXPECTED_EVENT_ROWS},'priceHistory',0,'scrapeLogs',0,
         'sourceIndex',{EXPECTED_SOURCE_INDEX_ROWS},'queue',{EXPECTED_QUEUE_ROWS}
       )
   FROM _cw_preimage
 ) THEN
   RAISE EXCEPTION 'Cushman outer/inner metadata mismatch';
 END IF;
 SELECT count(*) INTO mismatch
 FROM _pre_listings p
 FULL JOIN _cw_rows r USING(id)
 WHERE p.id IS NULL OR r.id IS NULL
    OR p.external_id IS DISTINCT FROM r.external_id
    OR p.source_url IS DISTINCT FROM r.source_url
    OR (p.deleted_at IS NOT NULL) IS DISTINCT FROM r.deleted
    OR {generation_expr("p")} IS DISTINCT FROM r.generation
    OR p.updated_at IS DISTINCT FROM r.updated_at;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman outer/inner listing state mismatch: %',mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM _pre_repair_plan p
 FULL JOIN _pre_outer_topology o USING(id)
 WHERE p.id IS NULL OR o.id IS NULL
    OR p.target_id IS DISTINCT FROM o.target_id
    OR p.survivor_id IS DISTINCT FROM o.survivor_id
    OR p.has_current IS DISTINCT FROM o.has_current
    OR p.post_external_id IS DISTINCT FROM o.post_external_id
    OR p.post_source_url IS DISTINCT FROM o.post_source_url
    OR p.post_deleted IS DISTINCT FROM o.post_deleted
    OR p.post_generation IS DISTINCT FROM o.post_generation;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman outer/inner repair topology mismatch: %',mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM _pre_source_index p
 FULL JOIN _cw_si_plan o USING(id)
 WHERE p.id IS NULL OR o.id IS NULL
    OR p.external_id IS DISTINCT FROM o.external_id
    OR p.url IS DISTINCT FROM o.url
    OR p.last_seen IS DISTINCT FROM o.last_seen
    OR p.last_enumerated_at IS DISTINCT FROM o.last_enumerated_at;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman outer/inner source-index state mismatch: %',
     mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM _pre_queue p
 FULL JOIN _cw_queue_plan o USING(id)
 WHERE p.id IS NULL OR o.id IS NULL
    OR p.external_id IS DISTINCT FROM o.external_id
    OR p.url IS DISTINCT FROM o.url
    OR p.reason IS DISTINCT FROM o.reason
    OR (p.claimed_at IS NOT NULL) IS DISTINCT FROM o.claimed
    OR p.enqueued_at IS DISTINCT FROM o.enqueued_at
    OR p.claimed_at IS DISTINCT FROM o.claimed_at
    OR p.done_at IS DISTINCT FROM o.done_at
    OR p.attempts IS DISTINCT FROM o.attempts;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman outer/inner queue state mismatch: %',mismatch;
 END IF;
END
$cw_outer_inner_correlation$;

DO $guard$
DECLARE
 brokerage uuid;
 captured timestamptz;
 mismatch integer;
 logical_drift integer;
BEGIN
 SELECT id INTO brokerage FROM credeals.cre_brokerages
 WHERE slug='cushman-wakefield';
 IF brokerage IS NULL THEN RAISE EXCEPTION 'Cushman brokerage is absent'; END IF;
 SELECT (payload->>'capturedAt')::timestamptz INTO captured FROM _cw_preimage;
 SELECT count(*) INTO mismatch
 FROM _pre_repair_plan p
 FULL JOIN (
   SELECT * FROM credeals.cre_listings WHERE brokerage_id=brokerage
 ) l ON l.id=p.id
 WHERE p.id IS NULL OR l.id IS NULL
    OR l.raw_data #>> '{{cushmanIdentityRepair,repairToken}}'
      IS DISTINCT FROM {sql_lit(REPAIR_TOKEN)}
    OR l.updated_at IS DISTINCT FROM (
      l.raw_data #>> '{{cushmanIdentityRepair,appliedAt}}'
    )::timestamptz
    OR (
      COALESCE(
        (p.post_state->>'deleted_at_uses_apply_timestamp')::boolean,false
      )
      AND l.deleted_at IS DISTINCT FROM l.updated_at
    )
    OR jsonb_build_object(
      'external_id',l.external_id,
      'source_url',l.source_url,
      'canonical_url',l.canonical_url,
      'status',l.status,
      'transaction_type',l.transaction_type,
      'property_type',l.property_type,
      'title',l.title,
      'address',l.address,
      'city',l.city,
      'state',l.state,
      'zip',l.zip,
      'country',l.country,
      'lat',l.lat,
      'lng',l.lng,
      'scraped_at',l.scraped_at,
      'source_lastmod',l.source_lastmod,
      'canonical_key',l.canonical_key,
      'deleted_at_static',CASE WHEN COALESCE(
        (p.post_state->>'deleted_at_uses_apply_timestamp')::boolean,false
      ) THEN NULL ELSE l.deleted_at END,
      'deleted_at_uses_apply_timestamp',COALESCE(
        (p.post_state->>'deleted_at_uses_apply_timestamp')::boolean,false
      )
    ) IS DISTINCT FROM p.post_state
    OR octet_length(
      convert_to(
        (l.raw_data #- '{{cushmanIdentityRepair,appliedAt}}')::text,
        'UTF8'
      )
    ) IS DISTINCT FROM p.post_raw_data_bytes
    OR encode(
      digest(
        convert_to(
          (l.raw_data #- '{{cushmanIdentityRepair,appliedAt}}')::text,
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    ) IS DISTINCT FROM p.post_raw_data_sha256;
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'rollback refused: parent post-repair disposition drift: %',
     mismatch;
 END IF;
{child_precheck}
 SELECT count(*) INTO logical_drift FROM (
   SELECT 1 FROM credeals.cre_source_index si
   WHERE si.brokerage_id=brokerage
     AND (si.last_seen>captured OR si.last_enumerated_at>captured)
   UNION ALL
   SELECT 1 FROM credeals.cre_enrichment_queue q
   WHERE q.brokerage_id=brokerage
     AND (q.enqueued_at>captured OR q.claimed_at IS NOT NULL)
 ) q;
 IF logical_drift<>0 THEN RAISE EXCEPTION 'rollback refused after logical drift'; END IF;
END
$guard$;

UPDATE credeals.cre_listings l
SET external_id='cushman-rollback:v1:'||md5(l.id::text)
FROM _pre_listings p WHERE p.id=l.id;
UPDATE credeals.cre_listings l
SET external_id=p.external_id,source_url=p.source_url,
 canonical_url=p.canonical_url,status=p.status,
 transaction_type=p.transaction_type,property_type=p.property_type,
 title=p.title,address=p.address,city=p.city,state=p.state,zip=p.zip,
 country=p.country,lat=p.lat,lng=p.lng,scraped_at=p.scraped_at,
 raw_data=p.raw_data,source_lastmod=p.source_lastmod,
 canonical_key=p.canonical_key,deleted_at=p.deleted_at,
 updated_at=clock_timestamp()
FROM _pre_listings p WHERE p.id=l.id;

{child_restore}

DELETE FROM credeals.cre_source_index si
WHERE si.brokerage_id=(SELECT id FROM credeals.cre_brokerages
                       WHERE slug='cushman-wakefield');
INSERT INTO credeals.cre_source_index
SELECT * FROM _pre_source_index;
DELETE FROM credeals.cre_enrichment_queue q
WHERE q.brokerage_id=(SELECT id FROM credeals.cre_brokerages
                      WHERE slug='cushman-wakefield');
INSERT INTO credeals.cre_enrichment_queue
SELECT * FROM _pre_queue;

DO $post$
DECLARE brokerage uuid; mismatch integer;
BEGIN
 SELECT id INTO brokerage FROM credeals.cre_brokerages
 WHERE slug='cushman-wakefield';
 SELECT count(*) INTO mismatch
 FROM _pre_listings p
 FULL JOIN (
   SELECT * FROM credeals.cre_listings WHERE brokerage_id=brokerage
 ) l USING(id)
 WHERE p.id IS NULL OR l.id IS NULL
    OR l.external_id IS DISTINCT FROM p.external_id
    OR l.source_url IS DISTINCT FROM p.source_url
    OR l.canonical_url IS DISTINCT FROM p.canonical_url
    OR l.status IS DISTINCT FROM p.status
    OR l.transaction_type IS DISTINCT FROM p.transaction_type
    OR l.property_type IS DISTINCT FROM p.property_type
    OR l.title IS DISTINCT FROM p.title
    OR l.address IS DISTINCT FROM p.address
    OR l.city IS DISTINCT FROM p.city
    OR l.state IS DISTINCT FROM p.state
    OR l.zip IS DISTINCT FROM p.zip
    OR l.country IS DISTINCT FROM p.country
    OR l.lat IS DISTINCT FROM p.lat
    OR l.lng IS DISTINCT FROM p.lng
    OR l.scraped_at IS DISTINCT FROM p.scraped_at
    OR l.raw_data IS DISTINCT FROM p.raw_data
    OR l.source_lastmod IS DISTINCT FROM p.source_lastmod
    OR l.canonical_key IS DISTINCT FROM p.canonical_key
    OR l.deleted_at IS DISTINCT FROM p.deleted_at
    OR l.updated_at IS DISTINCT FROM (
      SELECT trigger_updated_at FROM _cw_rollback_clock
    );
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman rollback parent readback failed: %',mismatch;
 END IF;
{child_postcheck}
 SELECT count(*) INTO mismatch
 FROM _pre_source_index p
 FULL JOIN (
   SELECT * FROM credeals.cre_source_index WHERE brokerage_id=brokerage
 ) live USING(id)
 WHERE p.id IS NULL OR live.id IS NULL
    OR to_jsonb(live) IS DISTINCT FROM to_jsonb(p);
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman rollback source-index readback failed: %',mismatch;
 END IF;
 SELECT count(*) INTO mismatch
 FROM _pre_queue p
 FULL JOIN (
   SELECT * FROM credeals.cre_enrichment_queue WHERE brokerage_id=brokerage
 ) live USING(id)
 WHERE p.id IS NULL OR live.id IS NULL
    OR to_jsonb(live) IS DISTINCT FROM to_jsonb(p);
 IF mismatch<>0 THEN
   RAISE EXCEPTION 'Cushman rollback queue readback failed: %',mismatch;
 END IF;
END
$post$;
SELECT jsonb_build_object(
 'ok',true,'mode','rollback_applied',
 'parentsRestored',{EXPECTED_TOTAL_ROWS},
 'updatedAtDisposition','advanced_by_rollback_trigger'
)::text;
"""


def build_rollback_sql(
    preimage: dict, artifact: list[ArtifactRow], state: dict
) -> str:
    return (
        "BEGIN ISOLATION LEVEL SERIALIZABLE;\n"
        + rollback_body(preimage, artifact, state)
        + "\nCOMMIT;\n"
    )


def transaction_body(sql: str) -> str:
    lines = sql.strip().splitlines()
    if not lines or not lines[0].startswith("BEGIN "):
        raise ValueError("expected explicit transaction")
    body = "\n".join(lines[1:])
    body, marker, trailing = body.rpartition("COMMIT;")
    if not marker or trailing.strip():
        raise ValueError("expected terminal COMMIT")
    return body.rstrip()


def build_roundtrip_sql(
    preimage: dict, artifact: list[ArtifactRow], state: dict
) -> str:
    forward = transaction_body(build_apply_sql(artifact, state))
    reverse = transaction_body(build_rollback_sql(preimage, artifact, state))
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
{forward}
DROP TABLE _cw_current,_cw_image_rank,_cw_aliases,_cw_survivors,
 _cw_om_owner,_cw_om_ranked,_cw_relationship_score,
 _cw_queue_plan,_cw_si_plan,_cw_rows,_cw_artifact;
{reverse}
ROLLBACK;
"""


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
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
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


def load_private_preimage(path: Path, expected_sha256: str) -> tuple[dict, str]:
    if not path.is_absolute():
        raise ValueError("rollback preimage path must be absolute")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected preimage SHA-256 must be lowercase hex")
    _reject_symlink_components(path)
    _require_private_parent(path.parent)
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
    payload = validate_preimage(json.loads(raw))
    return payload, actual_sha256


def assert_db_target(db_url: str) -> None:
    if database_target_fingerprint_from_url(db_url)["value"] != EXPECTED_DB_TARGET_SHA256:
        raise ValueError("database target does not match reviewed Cushman target")


@contextmanager
def shared_cre_lock(lock_dir: Path):
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    if lock_dir.is_symlink():
        raise ValueError("CRE lock path must not be a symlink")
    if lock_dir.exists() and not lock_dir.is_dir():
        current = lock_dir.stat()
        if not lock_dir.is_file() or lock_dir.name != ".cre.lock" or current.st_size:
            raise ValueError("CRE lock path is not a recognized empty legacy lock")
        with lock_dir.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("legacy CRE lock is held") from exc
            opened = os.fstat(handle.fileno())
            current = lock_dir.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError("legacy CRE lock changed")
            lock_dir.unlink()
            with SharedLock(lock_dir) as lock:
                yield lock
        return
    with SharedLock(lock_dir) as lock:
        yield lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--preimage", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-apply-rollback", action="store_true")
    parser.add_argument("--verify-rollback-roundtrip", action="store_true")
    parser.add_argument("--rollback-preimage", type=Path)
    parser.add_argument("--expected-preimage-sha256")
    args = parser.parse_args(argv)
    selected = sum(
        bool(value)
        for value in (
            args.apply,
            args.verify_apply_rollback,
            args.verify_rollback_roundtrip,
            args.rollback_preimage,
        )
    )
    if selected > 1:
        parser.error("repair mutation modes are mutually exclusive")
    if args.apply and args.preimage is None:
        parser.error("--apply requires --preimage")
    if args.preimage is not None and not args.apply:
        parser.error("--preimage is valid only with --apply")
    if args.rollback_preimage and args.expected_preimage_sha256 is None:
        parser.error("--rollback-preimage requires --expected-preimage-sha256")
    if args.expected_preimage_sha256 is not None and not args.rollback_preimage:
        parser.error(
            "--expected-preimage-sha256 is valid only with --rollback-preimage"
        )

    db_url, _ = load_db_url(args.env_file)
    assert_db_target(db_url)
    with shared_cre_lock(DEFAULT_LOCK):
        if args.rollback_preimage:
            assert args.expected_preimage_sha256 is not None
            preimage, preimage_sha256 = load_private_preimage(
                args.rollback_preimage, args.expected_preimage_sha256
            )
            artifact = artifact_from_preimage(preimage)
            state = state_from_preimage(preimage)
            result = run_psql(db_url, build_rollback_sql(preimage, artifact, state))
            result["preimageSha256"] = preimage_sha256
        else:
            artifact = load_artifact(args.artifact.resolve())
            state = load_live_state(db_url)
            preflight = run_psql(db_url, preflight_sql(artifact, state))
            if args.verify_apply_rollback:
                body = transaction_body(build_apply_sql(artifact, state))
                result = run_psql(
                    db_url,
                    "BEGIN ISOLATION LEVEL SERIALIZABLE;\n" + body + "\nROLLBACK;\n",
                    timeout_seconds=ROLLBACK_VERIFICATION_TIMEOUT_SECONDS,
                )
                result["mode"] = "verify_apply_rollback"
                result["persisted"] = False
            elif args.verify_rollback_roundtrip:
                preimage = run_psql(
                    db_url,
                    preimage_sql(artifact, state),
                    timeout_seconds=PREIMAGE_CAPTURE_TIMEOUT_SECONDS,
                    result_mode="preimage_chunks",
                )
                result = run_psql(
                    db_url,
                    build_roundtrip_sql(preimage, artifact, state),
                    timeout_seconds=ROLLBACK_VERIFICATION_TIMEOUT_SECONDS,
                )
                result["mode"] = "verify_rollback_roundtrip"
                result["persisted"] = False
            elif args.apply:
                assert args.preimage is not None
                preimage = run_psql(
                    db_url,
                    preimage_sql(artifact, state),
                    result_mode="preimage_chunks",
                )
                validate_preimage(preimage)
                preimage_sha256 = atomic_private_json(args.preimage, preimage)
                result = run_psql(db_url, build_apply_sql(artifact, state))
                result["preimage"] = str(args.preimage)
                result["preimageSha256"] = preimage_sha256
            else:
                result = preflight
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Cushman identity repair refused: {exc}") from exc
