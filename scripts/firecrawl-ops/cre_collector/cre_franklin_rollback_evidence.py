"""Read-only classification of one settled Franklin partial transaction.

This report never releases an owner, changes a checkpoint, or proves that a
remote backend or process descendants settled. Those are separate recovery
evidence roles and must be verified independently.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC
from pathlib import Path

ERROR = "Franklin expected scalar postimage differs"
SCHEMA = "franklin-settled-rollback-evidence/v1"


def classify(*, binding, expected_sql_sha, sql, log, snapshot):
    """Fail closed unless the exact target-bound, pre-COMMIT refusal is shown."""
    if not isinstance(binding, dict) or set(binding) != {
        "generation_id", "attempt_id", "artifact_sha256", "claims_sha256",
        "target_sha256", "job_id", "artifact_run_key",
    }:
        raise ValueError("Franklin binding invalid")
    for name, length in (("attempt_id", 32), ("artifact_sha256", 64),
                         ("claims_sha256", 64), ("target_sha256", 64)):
        if not isinstance(binding[name], str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", binding[name]):
            raise ValueError("Franklin binding invalid")
    if not isinstance(binding["generation_id"], str) or not binding["generation_id"]:
        raise ValueError("Franklin generation invalid")
    import uuid
    if str(uuid.UUID(binding["job_id"])) != binding["job_id"]:
        raise ValueError("Franklin job invalid")
    if not re.fullmatch(r"ingest:v1:[0-9a-f]{64}", binding["artifact_run_key"]):
        raise ValueError("Franklin artifact key invalid")
    if not isinstance(sql, str) or not isinstance(log, str):
        raise TypeError("Franklin evidence invalid")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    if digest != expected_sql_sha:
        raise ValueError("Franklin SQL differs")
    lines = sql.splitlines()
    begin = [n for n, line in enumerate(lines, 1) if line.strip() == "BEGIN;"]
    commit = [n for n, line in enumerate(lines, 1) if line.strip() == "COMMIT;"]
    guards = [n for n, line in enumerate(lines, 1) if "RAISE EXCEPTION '" + ERROR + "'" in line]
    if len(begin) != 1 or len(commit) != 1 or len(guards) != 1 or not begin[0] < guards[0] < commit[0]:
        raise ValueError("Franklin transaction scope differs")
    # psql reports the closing line of the DO statement, not the RAISE line.
    matches = re.findall(r"^psql:<stdin>:(\d+): ERROR:  Franklin expected scalar postimage differs$", log, re.MULTILINE)
    results = list(re.finditer(r"(?m)^\[[^\n]+\] rc=(\d+)$", log))
    error_position = log.find("psql:<stdin>:")
    if (len(results) != 1 or results[0].group(1) != "1"
            or results[0].start() <= error_position or log[results[0].end():].strip()
            or len(matches) != 1 or int(matches[0]) != guards[0] + 1
            or len(re.findall(r"^psql:<stdin>:\d+: ERROR:", log, re.MULTILINE)) != 1

            or "--franklin-site-claims " not in log
            or "--checkpoint-ingest-attempt " + binding["attempt_id"] not in log
            or "--expected-db-target-sha256 " + binding["target_sha256"] not in log):
        raise ValueError("Franklin psql refusal not exact")
    required = {"target_sha256", "job_uuid_or_key", "last_generation", "history_generation", "malformed_history"}
    if not isinstance(snapshot, dict) or set(snapshot) != required or snapshot["target_sha256"] != binding["target_sha256"]:
        raise ValueError("Franklin target snapshot invalid")
    for name in required - {"target_sha256"}:
        if type(snapshot[name]) is not int or snapshot[name] != 0:
            raise ValueError("Franklin target snapshot not empty")
    return {"schema": SCHEMA, "outcome": "exact_precommit_rollback",
            "binding": binding, "sql_sha256": digest,
            "log_sha256": hashlib.sha256(log.encode("utf-8")).hexdigest(),
            "snapshot": snapshot,
            "scope": "transactional database effects; remote and descendant settlement separate"}


def snapshot_sql(binding):
    """One target-bound, repeatable-read query including soft-deleted parents."""
    from cre_ingest import sql_lit

    generation = sql_lit(binding["generation_id"])
    job = sql_lit(binding["job_id"])
    key = sql_lit(binding["artifact_run_key"])
    return f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH parents AS (
 SELECT l.raw_data FROM credeals.cre_listings l
 JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
 WHERE b.slug='franklin-street'
), counts AS (
 SELECT
  (SELECT count(*) FROM credeals.cre_scrape_jobs
   WHERE id={job}::uuid OR artifact_run_key={key}) AS job_uuid_or_key,
  (SELECT count(*) FROM parents WHERE raw_data->>'franklinSiteLastGenerationId'={generation}) AS last_generation,
  (SELECT count(*) FROM parents p WHERE EXISTS (
     SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.raw_data->'franklinSiteObservations')='array'
       THEN p.raw_data->'franklinSiteObservations' ELSE '[]'::jsonb END) x
     WHERE x->>'generationId'={generation})) AS history_generation,
  (SELECT count(*) FROM parents p WHERE p.raw_data ? 'franklinSiteObservations'
    AND (jsonb_typeof(p.raw_data->'franklinSiteObservations') IS DISTINCT FROM 'array'
      OR EXISTS (SELECT 1 FROM jsonb_array_elements(CASE
         WHEN jsonb_typeof(p.raw_data->'franklinSiteObservations')='array'
         THEN p.raw_data->'franklinSiteObservations' ELSE '[]'::jsonb END) x
       WHERE jsonb_typeof(x) IS DISTINCT FROM 'object'
          OR jsonb_typeof(x->'generationId') IS DISTINCT FROM 'string'))) AS malformed_history
)
SELECT row_to_json(counts)::text AS result FROM counts;
ROLLBACK;
"""


def query_snapshot(binding, env_file):
    """Run SELECT only after independently matching the connection target."""
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import _run_psql_script, parse_tsv

    url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(url).get("value") != binding["target_sha256"]:
        raise ValueError("Franklin database target differs")
    result = _run_psql_script(find_psql(), url, snapshot_sql(binding), timeout=45)
    if result.returncode:
        raise ValueError("Franklin target snapshot failed")
    rows = parse_tsv(result.stdout)
    if len(rows) != 1 or set(rows[0]) != {"result"}:
        raise ValueError("Franklin target snapshot shape invalid")
    data = json.loads(rows[0]["result"])
    current, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(current).get("value") != binding["target_sha256"]:
        raise ValueError("Franklin database target changed")
    return {"target_sha256": binding["target_sha256"], **data}
def _private_file(path, limit, *, public_read=False):
    import os
    import stat

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1:
            raise ValueError("Franklin evidence ownership invalid")
        if stat.S_IMODE(before.st_mode) & (0o022 if public_read else 0o077):
            raise ValueError("Franklin evidence is not private")
        chunks, size = [], 0
        while block := os.read(descriptor, 65536):
            size += len(block)
            if size > limit:
                raise ValueError("Franklin evidence too large")
            chunks.append(block)
        after = os.fstat(descriptor)
        named = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_uid, s.st_mode, s.st_nlink)
        if identity(before) != identity(after) or identity(before) != identity(named):
            raise ValueError("Franklin evidence changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def authenticated_report(run_dir, env_file):
    """Rebuild sealed source transaction, then use a read-only bound snapshot.

    Run with imports from the clean original producer checkout. The returned
    report is known-outcome evidence only; it does not discharge the separate
    backend or descendant settlement requirements.
    """
    import cre_checkpoint_refresh as cp
    from cre_franklin_site_admission import ingest_subset_context
    from cre_franklin_site_sql import _binding, build_sql, stage

    run_dir = Path(run_dir)
    if not run_dir.is_absolute() or run_dir.resolve() != run_dir:
        raise ValueError("Franklin run path invalid")
    manifest_raw = _private_file(run_dir / "manifest.json", 8 * 1024 * 1024)
    manifest = json.loads(manifest_raw)
    source = manifest["sources"]["franklin-street"]
    pipeline = manifest.get("pipeline") or {}
    ingest = source.get("ingest") or {}
    if (source.get("state") != "ingest_recovery_required"
            or pipeline.get("commit_outcome_unknown") is not True
            or not ingest.get("finished_at") or ingest.get("rc") != 1):
        raise ValueError("Franklin checkpoint is not a settled unknown")
    sha, dirty = cp.git_identity()
    if dirty or sha != manifest.get("collector_git_sha"):
        raise ValueError("Franklin original producer checkout differs")
    target = cp.manifest_database_target_sha256(manifest)
    attempt = source["subset_ingest_attempt_id"]
    if ingest.get("attempt_id") != attempt:
        raise ValueError("Franklin ingest attempt differs")
    artifact = run_dir / source["artifact"]["path"]
    if not artifact.is_absolute() or artifact.resolve() != artifact or not artifact.is_relative_to(run_dir):
        raise ValueError("Franklin artifact path differs")
    artifact_raw = _private_file(artifact, 64 * 1024 * 1024)
    if hashlib.sha256(artifact_raw).hexdigest() != source["artifact"]["sha256"]:
        raise ValueError("Franklin artifact differs")
    claims_path = Path(source["franklin_partial_claims"]["path"])
    if not claims_path.is_absolute() or claims_path.resolve() != claims_path or not claims_path.is_relative_to(run_dir):
        raise ValueError("Franklin claims path differs")
    claims_raw = _private_file(claims_path, 64 * 1024 * 1024)
    if hashlib.sha256(claims_raw).hexdigest() != source["franklin_partial_claims"]["sha256"]:
        raise ValueError("Franklin claims differ")
    envelope = json.loads(claims_raw)
    context = ingest_subset_context(artifact, envelope, code_sha=sha, dirty=dirty,
        target_sha256=target, attempt_id=attempt, env=cp.safe_process_env())
    sql = build_sql(context["listings"], context["held"], context["sql_binding"])
    job = _binding(stage(context["listings"]), context["held"], context["sql_binding"])
    binding = {"generation_id": manifest["run_id"], "attempt_id": attempt,
        "artifact_sha256": source["artifact"]["sha256"],
        "claims_sha256": source["franklin_partial_claims"]["sha256"],
        "target_sha256": target, "job_id": job["job_id"], "artifact_run_key": job["key"]}
    if context["sql_binding"]["generation_id"] != binding["generation_id"]:
        raise ValueError("Franklin generation differs")
    log_path = run_dir / ingest["log"]
    if not log_path.is_absolute() or log_path.resolve() != log_path or not log_path.is_relative_to(run_dir):
        raise ValueError("Franklin log path differs")
    log_raw = _private_file(log_path, 2 * 1024 * 1024, public_read=True)
    import shlex
    from datetime import datetime

    lines = log_raw.decode("utf-8").splitlines()
    commands = [line.split("] command: ", 1)[1] for line in lines if "] command: " in line]
    if len(commands) != 1:
        raise ValueError("Franklin ingest command ambiguous")
    argv = shlex.split(commands[0])
    expected_argv = [
        "/usr/bin/python3", "cre_ingest.py", "--in", str(artifact),
        "--skip-post-commit-summary", "--require-strict-freshness",
        "--env-file", env_file, "--expected-db-target-sha256", target,
        "--franklin-site-claims", str(claims_path),
        "--checkpoint-ingest-attempt", attempt,
        "--expected-db-target-sha256", target,
    ]
    if argv != expected_argv:
        raise ValueError("Franklin ingest command differs")
    snapshot_started = datetime.now(UTC).isoformat()
    snapshot = query_snapshot(binding, env_file)
    snapshot_finished = datetime.now(UTC).isoformat()
    report = classify(binding=binding, expected_sql_sha=source["dry_run"]["sql"]["sha256"],
        sql=sql, log=log_raw.decode("utf-8"), snapshot=snapshot)
    # Recheck every locally observed boundary after the remote read.
    if (_private_file(run_dir / "manifest.json", 8 * 1024 * 1024) != manifest_raw
            or _private_file(artifact, 64 * 1024 * 1024) != artifact_raw
            or _private_file(claims_path, 64 * 1024 * 1024) != claims_raw
            or _private_file(log_path, 2 * 1024 * 1024, public_read=True) != log_raw
            or cp.git_identity() != (sha, False)):
        raise ValueError("Franklin input changed after snapshot")
    report["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    report["snapshot_started_at"] = snapshot_started
    report["snapshot_finished_at"] = snapshot_finished
    return report

def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-collector", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    producer = args.producer_collector
    if not producer.is_absolute() or producer.resolve() != producer:
        parser.error("producer collector path must be canonical and absolute")
    sys.path.insert(0, str(producer))
    import cre_checkpoint_refresh as cp
    if Path(cp.__file__).resolve().parent != producer:
        raise ValueError("Franklin producer import differs")
    print(json.dumps(authenticated_report(args.run_dir, args.env_file), sort_keys=True))


if __name__ == "__main__":
    main()