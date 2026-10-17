#!/usr/bin/env python3
"""Exact-evidence JLL image cleanup. Default: a READ ONLY comparison.

This tool never derives a delete set from live URL patterns. The production CLI
accepts only the immutable AGENTIC-4371 conservative proposal and its two exact
evidence files. Apply, verification DML and restoration each require a separate
hash-bound founder approval. A failed mutation is not automatically retried.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

PROPOSAL_SHA256 = "e6171be4092f0c3f1056e28c2142db79bb693a70582ba6907f51b87f2fdaae92"
CANDIDATES_SHA256 = "79d54a4cd5046db6da39b74cea403269389c9a959b917b7c13f8d586e19ebff1"
PREIMAGE_SHA256 = "42fd011d62e6046d7fb60d5318927cf21f06e817ce2f7be1b3f8f76cf877ad96"
EXPECTED_DB_TARGET_SHA256 = (
    "e9f33bdcbcb58ea4422005d4e4f04b12cbd4fa5e64ddc15a91731c7fa412eeb0"
)
REASON = (
    "exact_historical_dom_comma_srcset_fragment_removed_by_unchanged_"
    "production_helper_and_not_native_or_retained_dom_reference"
)
IMAGE_FIELDS = ("id", "listing_id", "url", "alt_text", "is_primary", "display_order")
# The captured files are 100MB compressed / 431.5MB JSONL and 120MB JSON.
# Bounds leave headroom while preventing arbitrary expansion or row populations.
MAX_INPUT_BYTES = 256 * 1024**2
MAX_EXPANDED_BYTES = 640 * 1024**2
MAX_LINE_BYTES = 256 * 1024
MAX_METADATA_BYTES = 2 * 1024**2
CLIENT_SECONDS = 600


@dataclass(frozen=True)
class EvidencePins:
    proposal_sha256: str = PROPOSAL_SHA256
    candidates_sha256: str = CANDIDATES_SHA256
    preimage_sha256: str = PREIMAGE_SHA256
    candidate_count: int = 345440
    parent_count: int = 9985
    candidate_ids_sha256: str = (
        "e24cf1f90aba49bf2e6178580626e474ed58a1bc7ab1112c2b2226437885a99c"
    )
    parent_ids_sha256: str = (
        "cc3ceb497eb9cbbb5dd147c3360ad8ea1ca5d2e88956b32c19379aba6f1d81f3"
    )
    preimage_parent_count: int = 10626
    preimage_image_count: int = 419538


@dataclass(frozen=True)
class Evidence:
    work_dir: Path
    parent_count: int
    candidate_count: int
    pins: dict
    image_columns: list[dict]

    @property
    def parents_csv(self):
        return self.work_dir / "parents.csv"

    @property
    def images_csv(self):
        return self.work_dir / "images.csv"

    @property
    def candidates_csv(self):
        return self.work_dir / "candidates.csv"


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(value, length=64):
    return (
        isinstance(value, str)
        and bool(re.fullmatch(f"[a-f0-9]{{{length}}}", value))
        and value != "0" * length
    )


def identifier(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def private_path(path):
    path = Path(os.path.abspath(path))
    require(
        not any(p.is_symlink() for p in (path, *path.parents)), "evidence path alias"
    )
    return path


@contextmanager
def checked_open(path, maximum=MAX_INPUT_BYTES, *, private=True):
    path = private_path(path)
    if private:
        private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and 0 < before.st_size <= maximum,
            "input must be a bounded singly linked regular file",
        )
        if private:
            require(
                before.st_uid == os.getuid()
                and stat.S_IMODE(before.st_mode) & 0o077 == 0,
                "input must be private and owned",
            )
        yield stream
        after = os.fstat(stream.fileno())
        named = path.lstat()

        def fields(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        require(
            fields(before) == fields(after) == fields(named),
            "input changed during consumption",
        )


@contextmanager
def private_input(path, expected_hash, maximum=MAX_INPUT_BYTES):
    with checked_open(path, maximum) as stream:
        require(digest(expected_hash), "invalid input hash")
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == expected_hash,
            "input hash mismatch",
        )
        stream.seek(0)
        yield stream


def file_hash(path, *, private=True):
    with checked_open(path, private=private) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_directory(path):
    info = private_path(path).lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "private owned mode0700 directory required",
    )


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(encoded) <= MAX_METADATA_BYTES, "metadata exceeds bound")
    path = private_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)
    return hashlib.sha256(encoded).hexdigest()


def validate_candidate(row):
    require(isinstance(row, dict), "candidate must be an object")
    require(
        row.get("source_key") == "jll"
        and row.get("default_off") is True
        and row.get("apply_authorized") is False,
        "candidate source/evidence authorization differs",
    )
    require(
        identifier(row.get("parent_id")) and identifier(row.get("image_id")),
        "candidate UUID differs",
    )
    require(
        isinstance(row.get("external_id"), str)
        and row["external_id"]
        and not row["external_id"].startswith("investor:")
        and row.get("native_refId") == row["external_id"],
        "native parent identity differs",
    )
    require(
        row.get("is_primary") is False
        and type(row.get("display_order")) is int
        and row["display_order"] > 0,
        "protected primary/order-zero image",
    )
    require(
        isinstance(row.get("url"), str)
        and row["url"].startswith("https://")
        and row.get("reason") == REASON,
        "candidate historical qualification absent",
    )
    for name in (
        "expected_before_row_digest",
        "expected_before_parent_digest",
        "expected_before_image_collection_digest",
    ):
        require(digest(row.get(name), 32), "candidate full-row witness absent")
    require(
        digest(row.get("historical_cache_sha256"))
        and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{6}Z-[a-f0-9]{12}",
            row.get("historical_generation", ""),
        ),
        "historical generation/cache proof absent",
    )
    cache = row.get("historical_cache_relative_path")
    require(
        isinstance(cache, str)
        and not cache.startswith(("/", "\\"))
        and ".." not in cache.split("/")
        and "\\" not in cache
        and "/jll-detail/" in "/" + cache
        and cache.endswith(".json"),
        "historical cache locator differs",
    )
    proofs = row.get("fragment_proof")
    require(
        isinstance(proofs, list) and 0 < len(proofs) <= 128,
        "historical DOM fragment proof absent",
    )
    for proof in proofs:
        require(
            isinstance(proof, dict)
            and proof.get("attribute") == "srcset"
            and digest(proof.get("srcset_sha256"))
            and type(proof.get("complete_candidate_url_count")) is int
            and proof["complete_candidate_url_count"] > 0,
            "historical DOM witness differs",
        )


@contextmanager
def csv_output(path, fields):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(fields)
        yield writer
        stream.flush()
        os.fsync(stream.fileno())


def load_evidence(proposal, candidates, preimage, work_dir, *, pins=None):
    """Stream the large JSONL; retain compact bindings and one bounded preimage.

    Synthetic pins are a Python test seam, deliberately absent from the CLI.
    The durable normalized full image rows are also the restoration preimages.
    """
    pins = EvidencePins() if pins is None else pins
    with private_input(proposal, pins.proposal_sha256, MAX_METADATA_BYTES) as stream:
        plan = json.load(stream)
    require(
        plan.get("apply_authorized") is False and plan.get("default_off") is True,
        "immutable proposal is evidence, not production approval",
    )
    require(
        plan["files"]["conservative-candidates.jsonl.gz"]["sha256"]
        == pins.candidates_sha256
        and plan["inputs"]["full_row_capture"]["sha256"] == pins.preimage_sha256,
        "proposal does not bind inputs",
    )
    require(
        plan["counts"]["conservative_candidate_rows"] == pins.candidate_count
        and plan["counts"]["affected_distinct_parents"] == pins.parent_count
        and plan["candidate_ids_sha256"] == pins.candidate_ids_sha256
        and plan["parent_ids_sha256"] == pins.parent_ids_sha256,
        "proposal cohort differs",
    )
    work_dir = private_path(work_dir)
    private_directory(work_dir.parent)
    work_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    approved, parents = {}, {}
    expanded = 0
    ids = hashlib.sha256()
    prior = ""
    with (
        private_input(candidates, pins.candidates_sha256) as compressed,
        gzip.GzipFile(fileobj=compressed) as stream,
    ):
        while line := stream.readline(MAX_LINE_BYTES + 1):
            expanded += len(line)
            require(
                len(line) <= MAX_LINE_BYTES and expanded <= MAX_EXPANDED_BYTES,
                "candidate expansion exceeds bound",
            )
            row = json.loads(line)
            validate_candidate(row)
            iid, pid = row["image_id"], row["parent_id"]
            require(
                iid > prior and len(approved) < pins.candidate_count,
                "candidate order/count differs",
            )
            prior = iid
            ids.update((iid + "\n").encode())
            approved[iid] = (
                pid,
                row["url"],
                row["display_order"],
                row["expected_before_row_digest"],
            )
            binding = (
                row["external_id"],
                row["expected_before_parent_digest"],
                row["expected_before_image_collection_digest"],
                row["historical_generation"],
                row["historical_cache_sha256"],
            )
            require(
                pid not in parents or parents[pid] == binding,
                "parent historical bindings conflict",
            )
            parents[pid] = binding
    require(
        len(approved) == pins.candidate_count
        and len(parents) == pins.parent_count
        and ids.hexdigest() == pins.candidate_ids_sha256
        and hashlib.sha256(
            "".join(p + "\n" for p in sorted(parents)).encode()
        ).hexdigest()
        == pins.parent_ids_sha256,
        "exact approved candidate/parent ID set differs",
    )
    with private_input(preimage, pins.preimage_sha256) as stream:
        full = json.load(stream)
    require(
        full.get("read_only") == "on"
        and isinstance(full.get("parents"), list)
        and len(full["parents"]) == pins.preimage_parent_count,
        "preimage scope differs",
    )
    columns = full.get("image_columns")
    require(
        isinstance(columns, list)
        and [c["name"] for c in columns] == list(IMAGE_FIELDS),
        "complete six-column image catalog required",
    )
    require(
        not any(
            t["table"] == "cre_listing_images"
            for t in full.get("noninternal_triggers", [])
        ),
        "captured image trigger is unsupported",
    )
    evidence = Evidence(work_dir, len(parents), len(approved), asdict(pins), columns)
    seen_parents, seen_images, matched, total_images = set(), set(), 0, 0
    with (
        csv_output(
            evidence.parents_csv,
            (
                "id",
                "brokerage_id",
                "external_id",
                "source_url",
                "parent_digest",
                "raw_digest",
            ),
        ) as pw,
        csv_output(
            evidence.images_csv, ("id", "listing_id", "row_digest", "row_json")
        ) as iw,
        csv_output(
            evidence.candidates_csv, ("image_id", "parent_id", "expected_digest")
        ) as cw,
    ):
        for parent in full["parents"]:
            pid = parent["id"]
            require(
                identifier(pid) and pid not in seen_parents, "preimage duplicate parent"
            )
            seen_parents.add(pid)
            total_images += len(parent["images"])
            require(
                total_images <= pins.preimage_image_count,
                "preimage image bound exceeded",
            )
            if pid not in parents:
                continue
            binding = parents[pid]
            require(
                parent["brokerage_slug"] == "jll"
                and parent["status"] == "active"
                and parent["deleted_at"] is None
                and parent["external_id"] == binding[0]
                and parent["parent_digest"] == binding[1]
                and digest(parent["raw_digest"], 32),
                "parent preimage drift",
            )
            collection = hashlib.md5(
                "".join(sorted(i["row_digest"] for i in parent["images"])).encode()
            ).hexdigest()
            require(
                collection == binding[2], "complete parent image collection differs"
            )
            pw.writerow(
                [
                    pid,
                    parent["brokerage_id"],
                    parent["external_id"],
                    parent["source_url"],
                    parent["parent_digest"],
                    parent["raw_digest"],
                ]
            )
            for item in parent["images"]:
                row, row_digest = item["row"], item["row_digest"]
                iid = row["id"]
                require(
                    set(row) == set(IMAGE_FIELDS)
                    and identifier(iid)
                    and iid not in seen_images
                    and row["listing_id"] == pid
                    and digest(row_digest, 32),
                    "image full-row preimage differs",
                )
                seen_images.add(iid)
                iw.writerow(
                    [
                        iid,
                        pid,
                        row_digest,
                        json.dumps(row, sort_keys=True, separators=(",", ":")),
                    ]
                )
                if iid in approved:
                    require(
                        approved[iid]
                        == (pid, row["url"], row["display_order"], row_digest)
                        and row["is_primary"] is False
                        and row["display_order"] > 0,
                        "candidate/full-row preimage mismatch",
                    )
                    matched += 1
        require(
            total_images == pins.preimage_image_count
            and set(parents) <= seen_parents
            and matched == pins.candidate_count,
            "complete preimage candidate binding differs",
        )
        for iid, (pid, _url, _order, row_digest) in approved.items():
            cw.writerow([iid, pid, row_digest])
    del full, approved, seen_images
    manifest = {
        "schema_version": 1,
        "issue": "AGENTIC-4371",
        "pins": evidence.pins,
        "image_columns": columns,
        "historical_bindings_sha256": hashlib.sha256(
            json.dumps(parents, sort_keys=True).encode()
        ).hexdigest(),
        "files": {
            p.name: file_hash(p)
            for p in (
                evidence.parents_csv,
                evidence.images_csv,
                evidence.candidates_csv,
            )
        },
    }
    write_json(work_dir / "manifest.json", manifest)
    sync_directory(work_dir)
    return evidence


def copy_path(path):
    value = str(Path(path).absolute())
    require(
        re.fullmatch(r"[A-Za-z0-9_./ -]+", value) and value.startswith("/"),
        "unsafe psql copy path",
    )
    return "'" + value + "'"


def assert_sql(condition, message):
    from cre_ingest import sql_lit

    return f"DO $guard$ BEGIN IF NOT ({condition}) THEN RAISE EXCEPTION {sql_lit(message)}; END IF; END $guard$;\n"


def image_set_query(*, remaining=False):
    exclude = (
        "WHERE NOT EXISTS (SELECT 1 FROM _approved a WHERE a.image_id=e.id)"
        if remaining
        else ""
    )
    return f"SELECT e.id,e.listing_id,e.row_digest FROM _images e {exclude}"


def current_image_query():
    return "SELECT i.id,i.listing_id,md5(to_jsonb(i)::text) AS row_digest FROM credeals.cre_listing_images i JOIN _parents p ON p.id=i.listing_id"


def same_images(expected):
    current = current_image_query()
    return f"NOT EXISTS (({current} EXCEPT {expected}) UNION ALL ({expected} EXCEPT {current}))"


def build_sql(evidence, *, operation="check", commit=False):
    """Build the actual psql program, also used unchanged by isolated PG tests."""
    from cre_ingest import lifecycle_transaction_lock_sql, sql_lit

    require(
        operation in {"check", "delete", "restore"}
        and (operation != "check" or not commit),
        "unsupported repair operation",
    )
    readonly = operation == "check"
    # Mutation checks see a fresh snapshot AFTER the canonical/table locks.
    # REPEATABLE READ before lock acquisition could hide a concurrent insert.
    limits = "SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='120s'; SET LOCAL idle_in_transaction_session_timeout='30s';\n"
    sql = "\\set ON_ERROR_STOP on\n"
    if not readonly:
        sql += "BEGIN ISOLATION LEVEL READ COMMITTED;\n" + limits
    # PostgreSQL forbids CREATE, including TEMP, inside READ ONLY. Stage only
    # private local evidence first; the read-only snapshot starts before live reads.
    temporary_lifetime = "PRESERVE ROWS" if readonly else "DROP"
    sql += f"""CREATE TEMP TABLE _parents(id uuid PRIMARY KEY,brokerage_id uuid,external_id text,source_url text,parent_digest text,raw_digest text) ON COMMIT {temporary_lifetime};
CREATE TEMP TABLE _images(id uuid PRIMARY KEY,listing_id uuid,row_digest text,row_json jsonb) ON COMMIT {temporary_lifetime};
CREATE TEMP TABLE _approved(image_id uuid PRIMARY KEY,parent_id uuid,expected_digest text) ON COMMIT {temporary_lifetime};
"""
    for table, path in (
        ("_parents", evidence.parents_csv),
        ("_images", evidence.images_csv),
        ("_approved", evidence.candidates_csv),
    ):
        sql += f"\\copy {table} FROM {copy_path(path)} WITH (FORMAT csv, HEADER true)\n"
    sql += assert_sql(
        f"(SELECT count(*) FROM _parents)={evidence.parent_count} AND (SELECT count(*) FROM _approved)={evidence.candidate_count}",
        "exact staged counts differ",
    )
    for table, col, key in (
        ("_approved", "image_id", "candidate_ids_sha256"),
        ("_parents", "id", "parent_ids_sha256"),
    ):
        sql += assert_sql(
            f"(SELECT encode(sha256(convert_to(string_agg({col}::text||chr(10),'' ORDER BY {col}),'UTF8')),'hex') FROM {table})={sql_lit(evidence.pins[key])}",
            "exact staged identity digest differs",
        )
    if readonly:
        sql += "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + limits
    else:
        sql += lifecycle_transaction_lock_sql() + "\n"
        sql += """-- Preserve the collector's global -> source-index -> parent -> image order.
CREATE TEMP TABLE _source_locks ON COMMIT DROP AS SELECT si.id FROM credeals.cre_source_index si JOIN _parents p ON p.brokerage_id=si.brokerage_id AND p.external_id=si.external_id ORDER BY si.id FOR UPDATE OF si;
CREATE TEMP TABLE _parent_locks ON COMMIT DROP AS SELECT l.id FROM credeals.cre_listings l JOIN _parents p ON p.id=l.id ORDER BY l.id FOR UPDATE OF l;
LOCK TABLE credeals.cre_listing_images IN SHARE ROW EXCLUSIVE MODE;
CREATE TEMP TABLE _image_locks ON COMMIT DROP AS SELECT i.id FROM credeals.cre_listing_images i JOIN _parents p ON p.id=i.listing_id ORDER BY i.id FOR UPDATE OF i;
"""
    # Reject unexpected executable/cascading behavior, rather than normalizing it.
    sql += assert_sql(
        "NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='credeals.cre_listing_images'::regclass AND NOT tgisinternal) AND NOT EXISTS(SELECT 1 FROM pg_rewrite WHERE ev_class='credeals.cre_listing_images'::regclass AND rulename<>'_RETURN') AND NOT EXISTS(SELECT 1 FROM pg_constraint WHERE contype='f' AND confrelid='credeals.cre_listing_images'::regclass)",
        "image trigger/rule/inbound foreign key requires separate review",
    )
    columns = json.dumps(evidence.image_columns, separators=(",", ":"))
    sql += assert_sql(
        f"(SELECT jsonb_agg(jsonb_build_object('name',attname,'type',format_type(atttypid,atttypmod),'not_null',attnotnull) ORDER BY attnum) FROM pg_attribute WHERE attrelid='credeals.cre_listing_images'::regclass AND attnum>0 AND NOT attisdropped)={sql_lit(columns)}::jsonb",
        "image column catalog drift",
    )
    sql += assert_sql(
        "NOT EXISTS(SELECT 1 FROM _parents p LEFT JOIN credeals.cre_listings l ON l.id=p.id LEFT JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE l.id IS NULL OR b.slug IS DISTINCT FROM 'jll' OR l.brokerage_id IS DISTINCT FROM p.brokerage_id OR l.external_id IS DISTINCT FROM p.external_id OR l.external_id LIKE 'investor:%' OR l.source_url IS DISTINCT FROM p.source_url OR l.status IS DISTINCT FROM 'active' OR l.deleted_at IS NOT NULL OR md5(to_jsonb(l)::text) IS DISTINCT FROM p.parent_digest OR md5(l.raw_data::text) IS DISTINCT FROM p.raw_digest)",
        "parent source/generation/full-row CAS drift",
    )
    sql += assert_sql(
        "NOT EXISTS(SELECT 1 FROM _approved a LEFT JOIN _images e ON e.id=a.image_id WHERE e.id IS NULL OR e.listing_id IS DISTINCT FROM a.parent_id OR e.row_digest IS DISTINCT FROM a.expected_digest OR (e.row_json->>'is_primary')::boolean IS DISTINCT FROM false OR (e.row_json->>'display_order')::integer<=0 OR e.row_json->>'display_order' IS NULL)",
        "approved image preimage/protection drift",
    )
    expected_before = image_set_query(remaining=operation == "restore")
    sql += assert_sql(
        same_images(expected_before), "complete current image cohort drift"
    )
    if operation == "delete":
        sql += """CREATE TEMP TABLE _changed ON COMMIT DROP AS WITH deleted AS (
DELETE FROM credeals.cre_listing_images i USING _approved a WHERE i.id=a.image_id AND i.listing_id=a.parent_id AND md5(to_jsonb(i)::text)=a.expected_digest RETURNING i.id)
SELECT id FROM deleted;
"""
    elif operation == "restore":
        sql += """CREATE TEMP TABLE _changed ON COMMIT DROP AS WITH restored AS (
INSERT INTO credeals.cre_listing_images(id,listing_id,url,alt_text,is_primary,display_order)
SELECT r.id,r.listing_id,r.url,r.alt_text,r.is_primary,r.display_order FROM _images e JOIN _approved a ON a.image_id=e.id CROSS JOIN LATERAL jsonb_populate_record(NULL::credeals.cre_listing_images,e.row_json) r ORDER BY e.id RETURNING id)
SELECT id FROM restored;
"""
    if not readonly:
        sql += assert_sql(
            "NOT EXISTS((SELECT image_id FROM _approved EXCEPT SELECT id FROM _changed) UNION ALL (SELECT id FROM _changed EXCEPT SELECT image_id FROM _approved))",
            "mutation identity set differs",
        )
        sql += assert_sql(
            same_images(image_set_query(remaining=operation == "delete")),
            "unexpected post-mutation image changes",
        )
        # A generation change, including ordinary updated_at/raw_data changes,
        # must also refuse restoration. No parent is assigned by this SQL.
        sql += assert_sql(
            "NOT EXISTS(SELECT 1 FROM _parents p JOIN credeals.cre_listings l ON l.id=p.id WHERE md5(to_jsonb(l)::text)<>p.parent_digest)",
            "unexpected parent mutation",
        )
    sql += "COMMIT;\n" if commit else "ROLLBACK;\n"
    result = json.dumps(
        {
            "operation": operation,
            "committed": commit,
            "matched": evidence.candidate_count,
            "status": "passed",
        }
    )
    sql += f"SELECT 'JLL_REPAIR_RESULT:'||{sql_lit(result)};\n"
    return sql


def verify_stage(evidence):
    manifest_path = evidence.work_dir / "manifest.json"
    with private_input(
        manifest_path, file_hash(manifest_path), MAX_METADATA_BYTES
    ) as stream:
        manifest = json.load(stream)
    require(
        manifest["pins"] == evidence.pins
        and manifest["image_columns"] == evidence.image_columns,
        "staged manifest differs",
    )
    for name, expected in manifest["files"].items():
        require(
            name in {"parents.csv", "images.csv", "candidates.csv"},
            "unknown staged file",
        )
        with private_input(evidence.work_dir / name, expected):
            pass
    require(
        set(manifest["files"]) == {"parents.csv", "images.csv", "candidates.csv"},
        "staged preimages incomplete",
    )
    return file_hash(manifest_path)


def verify_postgres_terminal(verification, executor):
    path = verification.get("postgres_terminal_proof_path")
    require(
        isinstance(path, str) and Path(path).is_absolute(),
        "PostgreSQL terminal proof path absent",
    )
    with private_input(
        path, verification.get("postgres_terminal_proof_sha256"), MAX_METADATA_BYTES
    ) as stream:
        terminal = json.load(stream)
    tests = Path(__file__).parent / "tests"
    require(
        terminal.get("schema") == "jll-image-repair-postgres-runner/v1"
        and type(terminal.get("exit_code")) is int
        and terminal["exit_code"] == 0
        and "error" in terminal
        and terminal.get("error") is None
        and terminal.get("container_removed_or_absent") is True
        and terminal.get("container_cleanup")
        in {"absent", "removed", "removed_after_uncertain_cli_result"}
        and terminal.get("source_sha256") == executor
        and terminal.get("test_sha256")
        == file_hash(tests / "test_cre_repair_jll_images.py", private=False)
        and terminal.get("runner_sha256")
        == file_hash(
            tests / "run_jll_image_repair_postgres_contract.py", private=False
        ),
        "PostgreSQL terminal proof failed, unsettled or from different code",
    )


def verify_applied_receipt(path, expected_hash, evidence):
    require(path is not None, "exact prior applied receipt required")
    with private_input(path, expected_hash, MAX_METADATA_BYTES) as stream:
        receipt = json.load(stream)
    result = receipt.get("result")
    require(
        receipt.get("status") == "applied"
        and receipt.get("operation") == "delete"
        and receipt.get("evidence_pins") == evidence.pins
        and receipt.get("staged_manifest_sha256") == verify_stage(evidence)
        and receipt.get("database_target_sha256") == EXPECTED_DB_TARGET_SHA256
        and digest(receipt.get("executor_sha256"))
        and digest(receipt.get("collector_sha"), 40)
        and digest(receipt.get("approval_sha256"))
        and receipt.get("local_client_settled") is True
        and receipt.get("remote_sql_rollback_claim") is False
        and isinstance(result, dict)
        and result.get("committed") is True
        and type(result.get("matched")) is int
        and receipt.get("result")
        == {
            "operation": "delete",
            "committed": True,
            "matched": evidence.candidate_count,
            "status": "passed",
        },
        "prior receipt does not prove the exact committed apply",
    )
    # The approved whole-file hash binds older code provenance when restoration
    # uses a newer reviewed executor. Current live cohort/generation CAS still applies.
    completed = datetime.fromisoformat(receipt.get("completed_at", ""))
    require(completed.utcoffset() is not None, "prior apply completion clock absent")


def verify_approval(
    path, verification_path, evidence, *, operation, collector_sha, applied_receipt=None
):
    """A separate exact execution contract; design approval is insufficient."""
    require(
        path is not None and verification_path is not None,
        "separate production approval and isolated verification receipt required",
    )
    with private_input(path, file_hash(path), MAX_METADATA_BYTES) as stream:
        approval = json.load(stream)
    with private_input(
        verification_path, file_hash(verification_path), MAX_METADATA_BYTES
    ) as stream:
        verification = json.load(stream)
    executor = file_hash(Path(__file__), private=False)
    require(
        approval.get("schema_version") == 1
        and approval.get("issue") == "AGENTIC-4371"
        and approval.get("approved") is True
        and approval.get("approved_by") in {"cayman", "stace"}
        and approval.get("approval_scope") == "production_" + operation
        and re.fullmatch(
            r"https://linear\.app/agenticassets/issue/AGENTIC-4371#comment-[a-f0-9-]{36}",
            approval.get("approval_reference", ""),
        ),
        "exact founder production approval absent",
    )
    require(
        approval.get("executor_sha256") == executor
        and approval.get("collector_sha") == collector_sha
        and approval.get("evidence_pins") == evidence.pins
        and approval.get("staged_manifest_sha256") == verify_stage(evidence)
        and approval.get("verification_receipt_sha256") == file_hash(verification_path),
        "approval pins differ",
    )
    require(
        verification.get("status") == "passed"
        and verification.get("isolated_postgres") is True
        and verification.get("executor_sha256") == executor
        and verification.get("evidence_pins") == evidence.pins,
        "isolated PostgreSQL verification does not bind executor/evidence",
    )
    verify_postgres_terminal(verification, executor)
    if operation == "restore":
        verify_applied_receipt(
            applied_receipt, approval.get("applied_receipt_sha256"), evidence
        )
    else:
        require(applied_receipt is None, "applied receipt is only valid for restore")
    return file_hash(path)


def run_psql(db_url, sql, work_dir, *, timeout=CLIENT_SECONDS):
    """Bound output/wall time; settle only the process group we just spawned."""
    from cre_checkpoint_refresh import (
        ProcessCleanupUnknown,
        _require_completed_group_settlement,
    )
    from cre_ingest import find_psql, psql_connection_args, psql_connection_env
    from cre_owned_processes import (
        OwnedProcessGroup,
        _defer_cleanup_signals,
        settle_owned_groups,
    )

    path = work_dir / "transaction.sql"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(sql)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(work_dir)
    env = psql_connection_env(db_url)
    require(
        env.get("PGSSLMODE") == "verify-full",
        "verify-full database connection required",
    )
    env.update(PGCONNECT_TIMEOUT="5", PGAPPNAME="cre-jll-exact-image-repair")
    command = [
        find_psql(),
        *psql_connection_args(db_url),
        "-X",
        "-q",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        str(path),
    ]
    group = None
    process = None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        group = OwnedProcessGroup.from_spawn(process)
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selected:
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                selected.register(pipe, selectors.EVENT_READ, name)
            while selected.get_map():
                require(time.monotonic() < deadline, "repair client deadline")
                for key, _ in selected.select(
                    min(0.2, max(0, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selected.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        require(
                            sum(map(len, output.values())) <= 4 * 1024**2,
                            "repair output bound exceeded",
                        )
        rc = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        _require_completed_group_settlement(group)
        require(
            rc == 0, "psql transaction refused; inspect private output, do not retry"
        )
        lines = output["stdout"].decode("utf-8").splitlines()
        records = [
            json.loads(line.removeprefix("JLL_REPAIR_RESULT:"))
            for line in lines
            if line.startswith("JLL_REPAIR_RESULT:")
        ]
        require(
            len(records) == 1 and records[0]["status"] == "passed",
            "positive transaction terminal result absent",
        )
        return records[0]
    except BaseException as exc:
        if group is not None:
            with _defer_cleanup_signals():
                settlement = settle_owned_groups(
                    [group], grace_seconds=5, kill_seconds=3
                )
                if not settlement.settled:
                    raise ProcessCleanupUnknown(
                        "repair client cleanup unknown; preserve recovery fence"
                    ) from exc
        elif process is not None:
            raise ProcessCleanupUnknown("repair dispatch identity unknown") from exc
        raise
    finally:
        for name, content in output.items():
            fd = os.open(
                work_dir / (name + ".log"),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        if process is not None:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


def runtime_identity(expected_sha):
    require(
        re.fullmatch(r"[a-f0-9]{40}", expected_sha or ""),
        "exact reviewed collector SHA required",
    )
    root = Path(__file__).resolve().parents[3]

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], timeout=15, text=True
        ).strip()

    require(
        git("rev-parse", "HEAD") == expected_sha and not git("status", "--porcelain"),
        "exact clean reviewed runtime required",
    )


@contextmanager
def interruption_handler():
    def interrupt(_number, _frame):
        raise KeyboardInterrupt("operator interruption")

    old = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("proposal", "candidates", "preimage", "work-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--expected-sha", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify-apply-rollback", action="store_true")
    mode.add_argument("--restore", action="store_true")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--verification-receipt", type=Path)
    parser.add_argument(
        "--receipt", type=Path, help="Exact prior applied receipt, required for restore"
    )
    args = parser.parse_args(argv)
    mutation = args.apply or args.verify_apply_rollback or args.restore
    require(
        os.name == "posix",
        "repair requires the canonical POSIX host ownership contract",
    )
    require(
        not mutation or args.approval is not None,
        "production mutation authorization is OFF by default",
    )
    require(
        bool(args.restore) == bool(args.receipt),
        "restore requires its exact prior applied receipt",
    )
    runtime_identity(args.expected_sha)
    evidence = load_evidence(
        args.proposal, args.candidates, args.preimage, args.work_dir
    )
    stage_sha = verify_stage(evidence)
    operation = "restore" if args.restore else ("delete" if mutation else "check")
    approval_sha = None
    if mutation:
        scope = "verify_apply_rollback" if args.verify_apply_rollback else operation
        approval_sha = verify_approval(
            args.approval,
            args.verification_receipt,
            evidence,
            operation=scope,
            collector_sha=args.expected_sha,
            applied_receipt=args.receipt,
        )
    from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir
    from cre_ingest import assert_expected_database_target, load_db_url

    db_url, _ = load_db_url(args.env_file)
    assert_expected_database_target(db_url, EXPECTED_DB_TARGET_SHA256)
    receipt = {
        "status": "not_started",
        "operation": operation,
        "evidence_pins": evidence.pins,
        "executor_sha256": file_hash(Path(__file__), private=False),
        "collector_sha": args.expected_sha,
        "staged_manifest_sha256": stage_sha,
        "approval_sha256": approval_sha,
        "database_target_sha256": EXPECTED_DB_TARGET_SHA256,
    }
    try:
        with (
            interruption_handler(),
            SharedLock(
                canonical_shared_lock_dir(), recovery_required=mutation
            ) as lease,
        ):
            runtime_identity(args.expected_sha)
            verify_stage(evidence)
            receipt["status"] = (
                "dispatched_unverified"  # Persist intent before any psql dispatch.
            )
            write_json(evidence.work_dir / "intent.json", receipt)
            result = run_psql(
                db_url,
                build_sql(
                    evidence, operation=operation, commit=args.apply or args.restore
                ),
                evidence.work_dir,
            )
            require(
                result["operation"] == operation
                and result["committed"] == (args.apply or args.restore)
                and result["matched"] == evidence.candidate_count,
                "terminal result does not bind requested operation",
            )
            receipt.update(
                status="restored"
                if args.restore
                else (
                    "applied"
                    if args.apply
                    else ("verified_rollback" if mutation else "checked_read_only")
                ),
                result=result,
                completed_at=datetime.now(UTC).isoformat(),
                local_client_settled=True,
                remote_sql_rollback_claim=False,
            )
            write_json(evidence.work_dir / "receipt.json", receipt)
            if mutation:
                lease.clear_recovery_requirement()
    except BaseException:
        receipt.update(
            status="failed_unverified_do_not_retry",
            completed_at=datetime.now(UTC).isoformat(),
        )
        write_json(evidence.work_dir / "failure.json", receipt)
        raise
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "matched": evidence.candidate_count,
                "receipt_sha256": file_hash(evidence.work_dir / "receipt.json"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
