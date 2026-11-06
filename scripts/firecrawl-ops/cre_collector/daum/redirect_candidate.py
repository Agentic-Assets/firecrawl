"""Read-only candidate evidence for an observed DAUM URL transition.

This module never maps an identity, updates a target, or grants ingest authority.
The candidate and its target preimages must be rederived before any future use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from cre_daum_current_inventory import _path_key, _post
from daum.identity_sql import cohort_members


def candidate_query(member):
    """Return at most one exact historical candidate, including deleted collisions."""
    from cre_ingest import sql_lit

    if not isinstance(member, dict) or set(member) != {
        "native_id",
        "external_id",
        "source_url",
    }:
        raise ValueError("DAUM candidate member invalid")
    native, external, url = (
        member[k] for k in ("native_id", "external_id", "source_url")
    )
    _post(native)
    if external != _path_key(url):
        raise ValueError("DAUM current URL/path differs")
    native_sql, external_sql, url_sql = map(sql_lit, (native, external, url))
    return f"""
WITH history AS MATERIALIZED (
 SELECT t.*, ARRAY(SELECT DISTINCT n.value #>> '{{}}'
     FROM jsonb_path_query(t.raw_data, '$.**.daumPostId') n(value)
     WHERE n.value <> 'null'::jsonb) AS native_ids
 FROM credeals.cre_listings t
 JOIN credeals.cre_brokerages b ON b.id=t.brokerage_id AND b.slug='daum-commercial'
), matched AS (SELECT * FROM history WHERE {native_sql} = ANY(native_ids)),
candidate AS (
 SELECT m.* FROM matched m
 WHERE (SELECT count(*) FROM matched)=1 AND cardinality(m.native_ids)=1
   AND m.deleted_at IS NULL AND m.source_url <> {url_sql} AND m.external_id <> {external_sql}
   AND m.source_url ~ '^https://daumcommercial[.]com/property/[^/?#]+/$'
   AND m.external_id = substr(m.source_url, length('https://daumcommercial.com/') + 1,
                               length(m.source_url) - length('https://daumcommercial.com/') - 1)
   AND NOT EXISTS (SELECT 1 FROM history x WHERE x.id<>m.id AND
       (x.external_id IN (m.external_id, {external_sql}) OR x.source_url IN (m.source_url, {url_sql})))
   AND NOT EXISTS (SELECT 1 FROM credeals.cre_source_index si WHERE si.brokerage_id=m.brokerage_id AND
       ((si.external_id={external_sql} OR si.url={url_sql}) OR
        (si.external_id=m.external_id AND (si.source_key IS DISTINCT FROM 'daum-commercial'
                                           OR si.url IS DISTINCT FROM m.source_url)) OR
        (si.url=m.source_url AND si.external_id IS DISTINCT FROM m.external_id)))
)
SELECT COALESCE((SELECT json_build_object(
 'rowId', m.id::text, 'oldExternalId', m.external_id, 'oldUrl', m.source_url,
 'nativeId', {native_sql}, 'currentExternalId', {external_sql}, 'currentUrl', {url_sql},
 'rowSha256', encode(sha256(convert_to(to_jsonb(m)::text,'UTF8')),'hex'),
 'indexSha256', (SELECT encode(sha256(convert_to(to_jsonb(si)::text,'UTF8')),'hex')
                 FROM credeals.cre_source_index si WHERE si.brokerage_id=m.brokerage_id
                   AND si.external_id=m.external_id))::text FROM candidate m), 'null') AS candidate;
"""


def validate_candidate(candidate, member):
    """Check the bounded query result; this is evidence, not authority."""
    fields = {
        "rowId",
        "oldExternalId",
        "oldUrl",
        "nativeId",
        "currentExternalId",
        "currentUrl",
        "rowSha256",
        "indexSha256",
    }
    if not isinstance(candidate, dict) or set(candidate) != fields:
        raise ValueError("DAUM transition candidate unavailable")
    try:
        if str(uuid.UUID(candidate["rowId"])) != candidate["rowId"]:
            raise ValueError("DAUM candidate UUID differs")
        if (
            candidate["nativeId"] != member["native_id"]
            or candidate["currentExternalId"] != member["external_id"]
            or candidate["currentUrl"] != member["source_url"]
            or candidate["oldUrl"] == candidate["currentUrl"]
            or candidate["oldExternalId"] != _path_key(candidate["oldUrl"])
            or not re.fullmatch(r"[a-f0-9]{64}", candidate["rowSha256"] or "")
            or (
                candidate["indexSha256"] is not None
                and not re.fullmatch(r"[a-f0-9]{64}", candidate["indexSha256"])
            )
        ):
            raise ValueError("DAUM candidate binding differs")
    except (TypeError, AttributeError, KeyError) as exc:
        raise ValueError("DAUM candidate malformed") from exc
    return candidate


def probe_candidate(
    run_dir: Path, manifest: dict, native_id: str, env_file: str | None
):
    """Query one source-bound historical candidate in a read-only transaction."""
    from cre_checkpoint_refresh import git_identity, manifest_database_target_sha256
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    _post(native_id)
    checkpoint = manifest["sources"]["daum-commercial"]
    info = checkpoint["artifact"]
    rel = info["path"]
    if (not isinstance(rel, str) or not rel or Path(rel).is_absolute()
            or any(part in (".", "..") for part in rel.split("/"))
            or chr(92) in rel or run_dir.name != manifest["run_id"]):
        raise ValueError("DAUM transition artifact path differs")
    artifact_path = run_dir / rel
    raw = read_file(artifact_path)
    artifact_sha = hashlib.sha256(raw).hexdigest()
    if artifact_sha != info["sha256"] or len(raw) != info["bytes"]:
        raise ValueError("DAUM transition artifact differs")
    data = json.loads(raw)
    freshness = data["runMeta"]["freshness"]
    if (freshness["generationId"] != manifest["run_id"]
            or freshness["generationStartedAt"] != manifest["started_at"]
            or freshness["requireFreshDetails"] is not True):
        raise ValueError("DAUM transition generation differs")
    members = [
        member for member in cohort_members(data) if member["native_id"] == native_id
    ]
    if len(members) != 1:
        raise ValueError("DAUM transition member is not unique")
    target = manifest_database_target_sha256(manifest)
    code = manifest["collector_git_sha"]
    if not target or git_identity() != (code, False):
        raise ValueError("DAUM transition code or target unavailable")
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("DAUM transition target differs")
    query = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        + PIN_STRING_LITERALS
        + "\n"
        + candidate_query(members[0])
        + "\nROLLBACK;\n"
    )
    result = _run_psql_script(find_psql(), db_url, query, timeout=45)
    if result.returncode:
        raise RuntimeError("DAUM transition candidate query failed")
    current_url, _ = load_db_url(env_file)
    if (
        read_file(artifact_path) != raw
        or git_identity() != (code, False)
        or database_target_fingerprint_from_url(current_url).get("value") != target
    ):
        raise ValueError("DAUM transition inputs changed")
    rows = parse_tsv(result.stdout)
    if len(rows) != 1 or set(rows[0]) != {"candidate"}:
        raise ValueError("DAUM transition candidate response invalid")
    candidate = validate_candidate(json.loads(rows[0]["candidate"]), members[0])
    return {
        "schema": "daum-redirect-candidate/v1",
        "artifact_sha256": artifact_sha,
        "generation_id": manifest["run_id"],
        "producer_sha": code,
        "database_target_sha256": target,
        "member": members[0],
        "candidate": candidate,
        "write_authority": False,
    }


def collect_receipt(run_dir: Path, manifest: dict, native_id: str, env_file: str | None, output: Path):
    """Collect a private observation after two identical read-only target probes.

    The result is evidence only. No caller may treat it as permission to map,
    rekey, ingest or retire a listing.
    """
    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file

    envelope = probe_candidate(run_dir, manifest, native_id, env_file)
    collector_dir = Path(__file__).resolve().parent.parent
    node = "/usr/bin/node"
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    process = subprocess.run(
        [node, "--import", "tsx", "sources/daum-redirect-observe-cli.ts"],
        input=json.dumps(envelope).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=collector_dir,
        env=env,
        timeout=90,
        check=False,
    )
    if process.returncode or len(process.stdout) > 6_000_000:
        raise ValueError("DAUM redirect observation failed")
    receipt = json.loads(process.stdout)
    page, redirect = receipt["page"], receipt["redirect"]
    candidate = envelope["candidate"]
    if (
        receipt["schema"] != "daum-redirect-observation/v1"
        or receipt["write_authority"] is not False
        or receipt["candidate"] != envelope
        or redirect["status"] != 301
        or redirect["oldUrl"] != candidate["oldUrl"]
        or redirect["newUrl"] != candidate["currentUrl"]
        or page["url"] != candidate["currentUrl"]
        or page["shortlink"] != "https://daumcommercial.com/?p=" + native_id
        or hashlib.sha256(page["html"].encode()).hexdigest() != page["sha256"]
        or probe_candidate(run_dir, manifest, native_id, env_file) != envelope
        or git_identity() != (envelope["producer_sha"], False)
    ):
        raise ValueError("DAUM redirect receipt binding changed")
    output = Path(output)
    if not output.is_absolute() or any(p.is_symlink() for p in output.parents):
        raise ValueError("DAUM receipt output path is unsafe")
    payload = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    if read_file(output) != payload:
        raise ValueError("DAUM receipt readback differs")
    return receipt
