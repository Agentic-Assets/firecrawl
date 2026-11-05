"""Source-bound claims for Franklin site-card partial admission."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from cre_code_handoff_git import read_file
from cre_franklin_site_partial import derive
from cre_franklin_site_sql import stage

SOURCE = "franklin-street"
SCOPE = "franklin_site_partial_v1"
VERDICT = "ok_additive_verified_subset"


def candidate(artifact_path, manifest, *, code_sha, dirty, env):
    """Recompute the candidate from private source bytes and bind exact inputs."""
    config = manifest["config"]
    if (dirty or manifest.get("collector_git_dirty") is not False
            or code_sha != manifest.get("collector_git_sha")
            or not re.fullmatch(r"[0-9a-f]{40}", code_sha or "")
            or config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or SOURCE not in config.get("sources", [])):
        raise ValueError("Franklin partial owner or configuration invalid")
    path = Path(artifact_path)
    root = path.parent.parent
    if (path.name != "franklin-street.json" or path.parent.name != "sources"
            or root.name != manifest.get("run_id")):
        raise ValueError("Franklin partial path invalid")
    checkpoint = manifest["sources"][SOURCE]
    info = checkpoint["artifact"]
    origin = checkpoint["franklin_original"]
    attempts = checkpoint.get("attempts") or []
    if (not attempts or attempts[-1].get("number") != origin.get("attempt_number")
            or attempts[-1].get("rejected_artifact") != origin.get("path")
            or attempts[-1].get("rejected_artifact_sha256") != origin.get("sha256")
            or not attempts[-1].get("finished_at")):
        raise ValueError("Franklin source attempt binding differs")
    rel = Path(origin["path"])
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "rejected":
        raise ValueError("Franklin original path invalid")
    original = root / rel
    st = original.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
        raise ValueError("Franklin original custody invalid")
    raw = read_file(original)
    original_sha = hashlib.sha256(raw).hexdigest()
    artifact = read_file(path)
    digest = hashlib.sha256(artifact).hexdigest()
    if (original_sha != origin.get("sha256") or digest != info.get("sha256")
            or len(artifact) != info.get("bytes") or info.get("franklin_partial") is not True):
        raise ValueError("Franklin artifact binding changed")
    data = json.loads(artifact)
    meta = data["runMeta"]
    source_binding = {"generation_id": manifest["run_id"], "artifact_sha256": original_sha,
                      "generation_started_at": manifest["started_at"],
                      "started_at": meta["startedAt"], "finished_at": meta["finishedAt"]}
    rederived = derive(raw, source_binding, env)
    if rederived != data:
        raise ValueError("Franklin candidate differs from retained source")
    staged = stage(data["listings"])
    target = manifest["preflight"]["database_target"]
    if target.get("algorithm") != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", target.get("value") or ""):
        raise ValueError("Franklin target invalid")
    marker = meta["franklinSitePartial"]
    if (origin.get("buildout_pending") != marker.get("buildoutPending")
            or origin.get("held_observations") != marker.get("heldCardObservations")):
        raise ValueError("Franklin pending accounting differs")
    staged_sha = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
    if read_file(original) != raw or read_file(path) != artifact:
        raise ValueError("Franklin input changed during validation")
    return {"artifact_sha256": digest, "source_artifact_sha256": original_sha,
            "producer_sha": code_sha, "collector_git_sha": code_sha,
            "database_target_sha256": target["value"], "generation_id": manifest["run_id"],
            "generation_started_at": manifest["started_at"], "started_at": meta["startedAt"],
            "finished_at": meta["finishedAt"], "staged_sha256": staged_sha,
            "buildout_pending": marker["buildoutPending"], "held_observations": marker["heldCardObservations"],
            "identity_uncertainty": marker["identityUncertainty"]}, data


def admit(artifact_path, manifest, report, *, code_sha, dirty, env, phase):
    """Admit only exact source-bound partial rows; never establish coverage."""
    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts

    if phase not in ("source", "aggregate"):
        raise ValueError("invalid Franklin admission phase")
    binding, data = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty, env=env)
    probe = manifest["sources"][SOURCE].get("franklin_identity_probe")
    if (not isinstance(probe, dict) or any(probe.get(k) != binding[k] for k in
            ("artifact_sha256", "source_artifact_sha256", "generation_id", "producer_sha",
             "database_target_sha256", "staged_sha256"))):
        raise ValueError("Franklin identity probe binding differs")
    held = probe.get("held")
    from cre_franklin_site_sql import _partition
    staged = stage(data["listings"])
    _partition(staged, held)
    current, errors, sources, _, rejects = count_artifacts([str(artifact_path)], True)
    if (sources != {SOURCE} or errors.get(SOURCE) not in {"source_incomplete", "buildout_peers_pending"}
            or rejects != 0 or current.get(SOURCE) != len(staged)):
        raise ValueError("Franklin source gate counts differ")
    validate_report_scope(report, manifest, SOURCE, Path(artifact_path))
    summary, gate = report["summary"], report["per_source"][SOURCE]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != len(staged)):
        raise ValueError("Franklin source gate scope invalid")
    if gate.get("verdict") == VERDICT:
        raw_pair = (gate.get("raw_verdict"), gate.get("raw_reason"))
    else:
        raw_pair = (gate.get("verdict"), gate.get("reason"))
    expected = {
        ("first_seen", "no baseline row; cannot gate (first sight); a source pass also errored this run"),
        ("hold", f"source pass error: {errors[SOURCE]}"),
    }
    if raw_pair not in expected or gate.get("mark_missing_safe") is not False:
        raise ValueError("Franklin source gate reason differs")
    binding.update(schema="franklin-site-partial-v1", scope=SCOPE, source_key=SOURCE,
                   held=held, held_count=len(held), emitted_count=len(staged)-len(held),
                   whole_source_coverage=False)
    if phase == "aggregate" and manifest["sources"][SOURCE].get("gate", {}).get("current_inventory_proof") != binding:
        raise ValueError("Franklin source gate binding changed")
    gate.update(raw_verdict=raw_pair[0], raw_reason=raw_pair[1], verdict=VERDICT,
                reason="Franklin site cards admitted as partial; Buildout peers remain pending",
                mark_missing_safe=False, admission_scope=SCOPE, current_inventory_proof=binding)
    for key in ("hold_sources", "mark_missing_safe_sources"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != SOURCE]
    if isinstance(summary.get("mark_missing_safe_brokerages"), list):
        summary["mark_missing_safe_brokerages"] = [value for value in summary["mark_missing_safe_brokerages"]
                                                    if value != SOURCE]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get(SOURCE), dict):
        report["per_brokerage"][SOURCE]["mark_missing_safe"] = False
    if isinstance(summary.get("verdict_counts"), dict):
        counts = {}
        for source_gate in report["per_source"].values():
            verdict = source_gate.get("verdict")
            counts[verdict] = counts.get(verdict, 0) + 1
        summary["verdict_counts"] = counts
    report["scope"] = {"kind": SCOPE, "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return binding


def run_identity_probe(artifact_path, manifest, *, env_file, process_env):
    """Read-only exact target probe; caller persists this before the gate."""
    from cre_checkpoint_refresh import git_identity
    from cre_franklin_site_sql import _partition, identity_probe_sql
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    code_sha, dirty = git_identity()
    binding, data = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty, env=process_env)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != binding["database_target_sha256"]:
        raise ValueError("Franklin probe target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(data["listings"]) + "\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise RuntimeError("Franklin identity probe failed")
    rows = parse_tsv(result.stdout)
    if len(rows) != 1 or set(rows[0]) != {"result"}:
        raise ValueError("Franklin identity probe output invalid")
    value = json.loads(rows[0]["result"])
    if value.get("staged") != len(data["listings"]) or value.get("joined") != len(data["listings"]):
        raise ValueError("Franklin identity probe counts differ")
    held = value.get("held")
    _partition(stage(data["listings"]), held)
    current_url, _ = load_db_url(env_file)
    current_code, current_dirty = git_identity()
    again, _ = candidate(artifact_path, manifest, code_sha=current_code, dirty=current_dirty, env=process_env)
    if (database_target_fingerprint_from_url(current_url).get("value") != binding["database_target_sha256"]
            or again != binding):
        raise ValueError("Franklin identity probe input or target changed")
    return {**binding, "held": held}



def ingest_subset_context(artifact_path, envelope, *, code_sha, dirty, target_sha256, attempt_id, env):
    """Reprove original bytes and both gate reports at the ingest boundary."""
    import copy

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("Franklin partial claims invalid")
    manifest = copy.deepcopy(envelope["manifest"])
    if manifest["preflight"]["database_target"] != {"algorithm": "sha256", "value": target_sha256}:
        raise ValueError("Franklin partial target changed")
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = copy.deepcopy(envelope[name])
        if (report["per_source"][SOURCE].get("current_inventory_proof") != binding
                or admit(artifact_path, manifest, report, code_sha=code_sha, dirty=dirty,
                         env=env, phase=phase) != binding):
            raise ValueError("Franklin partial gate claim changed")
    current, data = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty, env=env)
    held = binding["held"]
    from cre_franklin_site_sql import _partition
    _partition(stage(data["listings"]), held)
    sql_binding = {key: current[key] for key in (
        "artifact_sha256", "generation_id", "generation_started_at", "started_at", "finished_at",
        "producer_sha", "database_target_sha256")}
    sql_binding["attempt_id"] = attempt_id
    return {"binding": binding, "sql_binding": sql_binding, "held": held, "listings": data["listings"]}



def run_ingest(args):
    """Run only the existing checkpoint's exact, source-bound ingest claim."""
    import subprocess
    import sys
    import tempfile

    from cre_checkpoint_refresh import git_identity, safe_process_env
    from cre_franklin_site_sql import build_sql
    from cre_ingest import (
        _status_activation_enabled,
        assert_expected_database_target,
        find_psql,
        load_db_url,
        psql_connection_args,
        psql_connection_env,
    )
    from cre_stop_guarded_tier import _read_at, _safe_stat

    if (len(args.inputs) != 1 or args.mark_missing or _status_activation_enabled(args.activate_status)
            or not args.require_strict_freshness or not args.expected_db_target_sha256
            or not args.checkpoint_ingest_attempt
            or args.colliers_partial_claims or args.jll_partial_claims
            or args.daum_verified_subset_claims or args.pyramid_verified_subset_claims
            or args.matthews_targeted_claims or args.newmark_page_attempt or args.newmark_page_claims
            or args.checkpoint_collector_git_sha or args.checkpoint_pyramid_code_sha or args.checkpoint_jll_code_sha):
        raise ValueError("unbound Franklin partial ingest")
    claims = Path(args.franklin_site_claims)
    directory = os.open(claims.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        raw, _ = _read_at(directory, claims.name, limit=64 * 1024 * 1024)
        envelope = json.loads(raw)
    finally:
        os.close(directory)
    code_sha, dirty = git_identity()
    context = ingest_subset_context(Path(args.inputs[0]), envelope, code_sha=code_sha, dirty=dirty,
                                    target_sha256=args.expected_db_target_sha256,
                                    attempt_id=args.checkpoint_ingest_attempt, env=safe_process_env())
    sql = build_sql(context["listings"], context["held"], context["sql_binding"])

    def write_and_run(out_dir):
        out = Path(out_dir)
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Franklin SQL directory is not private")
        sql_path = out / "ingest.sql"
        fd = os.open(sql_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(sql)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        if args.dry_run:
            return
        current_code, current_dirty = git_identity()
        current, _ = candidate(Path(args.inputs[0]), envelope["manifest"], code_sha=current_code,
                               dirty=current_dirty, env=safe_process_env())
        if any(current[key] != context["binding"][key] for key in current):
            raise ValueError("Franklin partial inputs changed before ingest")
        db_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(db_url, args.expected_db_target_sha256)
        proc = subprocess.run([find_psql(), *psql_connection_args(db_url), "-X", "-q",
                               "-v", "ON_ERROR_STOP=1", "-f", "-"],
                              input=sql, text=True, env=psql_connection_env(db_url),
                              stdout=sys.stdout, stderr=sys.stderr, check=False)
        if proc.returncode:
            raise RuntimeError("Franklin partial ingest outcome requires reconciliation")
        current_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(current_url, args.expected_db_target_sha256)

    if args.keep_artifacts:
        write_and_run(args.keep_artifacts)
    else:
        with tempfile.TemporaryDirectory(prefix="cre-franklin-site-") as out_dir:
            write_and_run(out_dir)



def run_readback(artifact_path, manifest, claims, *, target_sha256, env_file, process_env):
    """Read exact job, card and generation on the bound target, without writes."""
    from cre_checkpoint_refresh import git_identity
    from cre_franklin_site_sql import readback_sql
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("Franklin readback target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n"
           + readback_sql(claims["listings"], claims["held"], claims["sql_binding"])
           + "\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise RuntimeError("Franklin readback failed")
    rows = parse_tsv(result.stdout)
    if len(rows) != 1 or set(rows[0]) != {"result"}:
        raise ValueError("Franklin readback output invalid")
    value = json.loads(rows[0]["result"])
    expected = len(claims["listings"]) - len(claims["held"])
    invalid = value.get("invalid")
    allowed = {row["id"] for row in claims["listings"]} - set(claims["held"])
    if (value.get("expected") != expected or value.get("matching_jobs") != 1
            or not isinstance(invalid, list) or invalid != sorted(set(invalid)) or not set(invalid) <= allowed):
        raise ValueError("Franklin readback partition invalid")
    current_url, _ = load_db_url(env_file)
    code_sha, dirty = git_identity()
    current, _ = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty, env=process_env)
    if (database_target_fingerprint_from_url(current_url).get("value") != target_sha256
            or any(current[key] != claims["binding"][key] for key in current)):
        raise ValueError("Franklin readback inputs or target changed")
    return {"ok": not invalid, "invalid": invalid, "expected": expected, "held": claims["held"],
            "artifact_sha256": claims["binding"]["artifact_sha256"],
            "generation_id": claims["binding"]["generation_id"],
            "staged_sha256": claims["binding"]["staged_sha256"], "matching_jobs": 1}
