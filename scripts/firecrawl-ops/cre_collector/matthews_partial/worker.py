"""Guarded internal Matthews checkpoint writer; not an operator entrypoint."""
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from matthews_partial.checkpoint import _read_private, admit_gate, recheck_candidate
from matthews_partial.fill import fill_sql, readback_sql


def context(artifact_path, envelope, *, code_sha, dirty, target_sha256, attempt_id):
    """Rederive source and both existing gate reports for the exact target."""
    from cre_checkpoint_refresh import COHORT_SETTLED_FAILURE_EXIT

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("Matthews partial claims invalid")
    manifest = copy.deepcopy(envelope["manifest"])
    artifact = Path(artifact_path)
    root = artifact.parent.parent
    checkpoint = manifest["sources"]["matthews"]
    attempts = checkpoint.get("attempts") or []
    if not attempts or dirty or code_sha != manifest.get("collector_git_sha"):
        raise ValueError("Matthews partial code or attempt differs")
    attempt = attempts[-1]
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = copy.deepcopy(envelope[name])
        if (report.get("per_source", {}).get("matthews", {}).get("current_inventory_proof") != binding
                or admit_gate(artifact, root, manifest, report, git_identity=lambda: (code_sha, dirty),
                              target_sha256=target_sha256, phase=phase,
                              settled_rcs=(0, 1, COHORT_SETTLED_FAILURE_EXIT)) != binding):
            raise ValueError("Matthews partial gate claim changed")
    candidate = recheck_candidate(root, manifest, attempt, git_identity=lambda: (code_sha, dirty),
                                  target_sha256=target_sha256, settled_rcs=(0, 1, COHORT_SETTLED_FAILURE_EXIT))
    sql_binding = {"artifact_sha256": binding["artifact_sha256"], "database_target_sha256": target_sha256,
                   "producer_sha": code_sha, "generation_id": manifest["run_id"], "attempt_id": attempt_id,
                   "started_at": candidate["started_at"], "finished_at": candidate["finished_at"]}
    return {"binding": binding, "sql_binding": sql_binding, "rows": candidate["rows"]}


def run_ingest(args):
    """Execute only bound checkpoint claims while inheriting the shared owner."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import (
        _status_activation_enabled,
        assert_expected_database_target,
        find_psql,
        load_db_url,
        psql_connection_args,
        psql_connection_env,
    )
    from cre_tier_dispatch import verify_inherited_lock

    competing = ("nai_post_claims", "franklin_site_claims", "colliers_partial_claims", "lyon_partial_claims",
                 "jll_partial_claims", "daum_verified_subset_claims", "pyramid_verified_subset_claims",
                 "matthews_targeted_claims", "newmark_page_attempt", "newmark_page_claims",
                 "checkpoint_collector_git_sha", "checkpoint_pyramid_code_sha", "checkpoint_jll_code_sha")
    if (len(args.inputs) != 1 or args.mark_missing or _status_activation_enabled(args.activate_status)
            or not args.require_strict_freshness or not args.expected_db_target_sha256
            or not args.checkpoint_ingest_attempt or not isinstance(args.matthews_partial_claims_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", args.matthews_partial_claims_sha256)
            or any(getattr(args, name, None) for name in competing)):
        raise ValueError("unbound Matthews partial ingest")
    verify_inherited_lock(allow_recovery_required=True, require_direct_parent=True)
    claim_path = Path(args.matthews_partial_claims)
    claim_bytes = _read_private(claim_path)
    if hashlib.sha256(claim_bytes).hexdigest() != args.matthews_partial_claims_sha256:
        raise ValueError("Matthews partial claims hash differs")
    envelope = json.loads(claim_bytes)
    code, dirty = git_identity()
    bound = context(Path(args.inputs[0]), envelope, code_sha=code, dirty=dirty,
                    target_sha256=args.expected_db_target_sha256, attempt_id=args.checkpoint_ingest_attempt)
    sql = fill_sql(bound["rows"], bound["sql_binding"])

    def write_and_run(directory):
        out = Path(directory)
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Matthews SQL directory is not private")
        fd = os.open(out / "ingest.sql", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(sql)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        if args.dry_run:
            return
        verify_inherited_lock(allow_recovery_required=True, require_direct_parent=True)
        code, dirty = git_identity()
        current = context(Path(args.inputs[0]), envelope, code_sha=code, dirty=dirty,
                          target_sha256=args.expected_db_target_sha256, attempt_id=args.checkpoint_ingest_attempt)
        if current != bound or _read_private(claim_path) != claim_bytes:
            raise ValueError("Matthews partial inputs changed before ingest")
        db_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(db_url, args.expected_db_target_sha256)
        proc = subprocess.run([find_psql(), *psql_connection_args(db_url), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                              input=sql, text=True, env=psql_connection_env(db_url),
                              stdout=sys.stdout, stderr=sys.stderr, check=False)
        if proc.returncode:
            raise RuntimeError("Matthews partial ingest outcome requires reconciliation")
        current_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(current_url, args.expected_db_target_sha256)
        verify_inherited_lock(allow_recovery_required=True, require_direct_parent=True)

    if args.keep_artifacts:
        write_and_run(args.keep_artifacts)
    else:
        with tempfile.TemporaryDirectory(prefix="cre-matthews-partial-") as out_dir:
            write_and_run(out_dir)


def settled_readback_sql(staged, binding):
    """Acquire the writer's transaction lock before the read-committed snapshot.

    A disconnected client can leave the server committing. READ COMMITTED takes
    the receipt statement's snapshot only after the lock wait finishes. A lock
    timeout is an unresolved outcome, never evidence of rollback.
    """
    from cre_ingest import LIFECYCLE_TRANSACTION_LOCK_NAME, sql_lit
    from cre_validate import PIN_STRING_LITERALS

    return ("BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY;\n"
            "SET LOCAL lock_timeout = '30s';\n" + PIN_STRING_LITERALS + "\n"
            "DO $$ BEGIN PERFORM pg_advisory_xact_lock(hashtextextended("
            + sql_lit(LIFECYCLE_TRANSACTION_LOCK_NAME) + ", 0)); END $$;\n"
            + readback_sql(staged, binding) + "\nROLLBACK;\n")


def run_readback(artifact_path, envelope, *, target_sha256, attempt_id, env_file):
    """Read the exact receipt and row/child digest on the bound target."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import _run_psql_script, parse_tsv

    code, dirty = git_identity()
    bound = context(artifact_path, envelope, code_sha=code, dirty=dirty,
                    target_sha256=target_sha256, attempt_id=attempt_id)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("Matthews readback target differs")
    sql = settled_readback_sql(bound["rows"], bound["sql_binding"])
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise RuntimeError("Matthews readback failed")
    rows = parse_tsv(result.stdout)
    if len(rows) != 1 or set(rows[0]) != {"result"}:
        raise ValueError("Matthews readback output invalid")
    value = json.loads(rows[0]["result"])
    occupied, matched, verified = value.get("occupied_jobs"), value.get("matching_jobs"), value.get("verified_jobs")
    held, updated, reasons = value.get("held"), value.get("updated"), value.get("held_reasons")
    identities = {row["external_id"] for row in bound["rows"]}
    if (any(type(count) is not int for count in (occupied, matched, verified))
            or not 0 <= verified <= matched <= occupied <= 1
            or not isinstance(held, list) or not isinstance(updated, list)
            or any(not isinstance(item, str) for item in held + updated)
            or held != sorted(set(held)) or updated != sorted(set(updated))
            or not set(held) <= identities or not set(updated) <= identities - set(held)
            or not isinstance(reasons, dict) or set(reasons) != set(held)
            or any(reason not in {"identity_conflict_or_unknown", "record_write_rejected"} for reason in reasons.values())
            or (verified == 0 and (held or updated or reasons))):
        raise ValueError("Matthews readback partition invalid")
    current_url, _ = load_db_url(env_file)
    code, dirty = git_identity()
    again = context(artifact_path, envelope, code_sha=code, dirty=dirty,
                    target_sha256=target_sha256, attempt_id=attempt_id)
    if database_target_fingerprint_from_url(current_url).get("value") != target_sha256 or again != bound:
        raise ValueError("Matthews readback inputs or target changed")
    return value
