"""Authenticate an existing NAI source artifact for post-scoped partial work."""
import hashlib
import json
import math
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from cre_code_handoff_git import read_file
from nai_partial.partial import SOURCE, _timestamp, partition_posts


class NaiSourceProbeRefusal(ValueError):
    """A settled source-local read-only refusal; eligible peers can proceed."""


def _utc_now():
    return datetime.now(timezone.utc)


def candidate(artifact_path, manifest, *, code_sha, dirty):
    """Recompute the partition from the owner's exact, settled source artifact."""
    config = manifest["config"]
    if (dirty or manifest.get("collector_git_dirty") is not False
            or code_sha != manifest.get("collector_git_sha")
            or not re.fullmatch(r"[0-9a-f]{40}", code_sha or "")
            or config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or SOURCE not in config.get("sources", [])):
        raise ValueError("NAI partial owner or configuration invalid")
    path = Path(artifact_path)
    root = path.parent.parent
    checkpoint = manifest["sources"][SOURCE]
    info = checkpoint["artifact"]
    from nai_partial.coverage_candidate import CANDIDATE, ORIGINAL
    expected_path = CANDIDATE if info.get("nai_coverage_partial") is True else ORIGINAL
    if (path.parent.name != "sources" or root.name != manifest.get("run_id")
            or root.is_symlink() or path.parent.is_symlink()
            or path.resolve() != root.resolve() / expected_path):
        raise ValueError("NAI partial source path invalid")
    recovery = info.get("nai_inventory_recovery") is True
    attempts = checkpoint.get("attempts") or []
    if (info.get("path") != expected_path or info.get("nai_post_partial") is not True or not attempts
            or type(attempts[-1].get("rc")) is not int or attempts[-1]["rc"] != 0
            or not attempts[-1].get("finished_at") or (not recovery and attempts[-1].get("error") is not None)
            or attempts[-1].get("started_at") != info.get("attempt_started_at")):
        raise ValueError("NAI source attempt is not a settled success")
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
        raise ValueError("NAI source artifact custody invalid")
    if st.st_size > 128 * 1024 * 1024:
        raise NaiSourceProbeRefusal("NAI source artifact exceeds the shared evidence reader bound")
    resolved_path = path.resolve()
    raw = read_file(resolved_path)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != info.get("sha256") or len(raw) != info.get("bytes"):
        raise ValueError("NAI source artifact differs")
    data = json.loads(raw)
    exact_data = json.loads(raw, parse_float=Decimal)
    meta = data["runMeta"]
    if (meta.get("transactions") != ["sale", "lease"] or meta.get("mode") != "full"
            or (meta.get("freshness") or {}).get("generationId") != manifest["run_id"]
            or info.get("freshness_generation_id") != manifest["run_id"]
            or not isinstance(data.get("listings"), list)):
        raise ValueError("NAI source generation differs")
    start = _timestamp(manifest["started_at"])
    if _timestamp((meta.get("freshness") or {}).get("generationStartedAt")) != start:
        raise ValueError("NAI source generation clock differs")
    attempt_start, attempt_finish = _timestamp(attempts[-1]["started_at"]), _timestamp(attempts[-1]["finished_at"])
    capture_start, capture_finish = _timestamp(meta["startedAt"]), _timestamp(meta["finishedAt"])
    if (not start <= attempt_start <= capture_start <= capture_finish <= attempt_finish
            or _timestamp(info.get("started_at")) != capture_start
            or _timestamp(info.get("finished_at")) != capture_finish
            or _timestamp(info.get("freshness_generation_started_at")) != start):
        raise ValueError("NAI source attempt clocks differ")
    now = _utc_now()
    max_age = info.get("max_observation_age_hours", 24)
    if (type(max_age) not in {int, float} or not math.isfinite(max_age) or max_age <= 0
            or capture_finish > now + timedelta(minutes=5)):
        raise ValueError("NAI source freshness bound invalid")
    source_count = len(data["listings"])
    if recovery:
        from nai_partial.inventory_recovery import SCHEMA, _FRESHNESS_ERROR, derive
        attempt = attempts[-1]
        number = attempt.get("number")
        if (data.get("schema") != SCHEMA or type(number) is not int or number < 1
                or not isinstance(attempt.get("error"), str) or not _FRESHNESS_ERROR.fullmatch(attempt["error"])
                or attempt.get("rejected_artifact") != f"rejected/nai-global-attempt-{number}.json"):
            raise ValueError("NAI recovery original binding differs")
        original_path = root / attempt["rejected_artifact"]
        original_info = original_path.lstat()
        if (not stat.S_ISREG(original_info.st_mode) or original_info.st_uid != os.getuid()
                or original_info.st_nlink != 1 or stat.S_IMODE(original_info.st_mode) != 0o600):
            raise ValueError("NAI recovery original custody differs")
        original = read_file(original_path)
        if hashlib.sha256(original).hexdigest() != attempt.get("rejected_artifact_sha256"):
            raise ValueError("NAI recovery original digest differs")
        expected, partition = derive(original, manifest["run_id"], manifest["started_at"],
                                     attempt["started_at"], attempt["finished_at"], now=now,
                                     max_age_hours=max_age)
        if expected != raw or read_file(original_path) != original:
            raise ValueError("NAI recovery candidate or original changed")
        source_count = data["naiRecovery"]["originalCount"]
    else:
        from nai_partial.coverage_candidate import ORIGINAL, SCHEMA, _private_regular, derive
        if info.get("nai_coverage_partial") is True:
            if data.get("schema") != SCHEMA or info.get("nai_coverage_original") != ORIGINAL:
                raise ValueError("NAI coverage candidate marker differs")
            original_path = root / ORIGINAL
            _private_regular(original_path)
            original = read_file(original_path.resolve())
            if (hashlib.sha256(original).hexdigest() != info.get("nai_coverage_original_sha256")
                    or derive(original) != raw or read_file(original_path.resolve()) != original):
                raise ValueError("NAI coverage original binding differs")
        elif data.get("schema") is not None:
            raise ValueError("NAI partial schema requires a bound checkpoint")
        partition = partition_posts(data["listings"], manifest["run_id"],
                                    capture_start=max(capture_start, now - timedelta(hours=max_age)),
                                    capture_finish=capture_finish, exact_listings=exact_data["listings"])
    if not partition["staged"]:
        raise NaiSourceProbeRefusal("NAI partial has no qualified posts")
    target = manifest["preflight"]["database_target"]
    if target.get("algorithm") != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", target.get("value") or ""):
        raise ValueError("NAI partial target invalid")
    partition_raw = json.dumps(partition, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    after = path.lstat()
    if (path.resolve() != resolved_path or read_file(resolved_path) != raw
            or any(getattr(st, key) != getattr(after, key)
                   for key in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))):
        raise ValueError("NAI source artifact changed during validation")
    return {"artifact_sha256": digest, "producer_sha": code_sha, "generation_id": manifest["run_id"],
            "database_target_sha256": target["value"], "partition_sha256": hashlib.sha256(partition_raw).hexdigest(),
            "source_count": source_count, "source_held": partition["held"],
            "staged_count": len(partition["staged"])}, partition


def run_identity_probe(artifact_path, manifest, env_file):
    """Probe the bound target read-only and retain exact numeric preimage bytes."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from nai_partial.partial import _partition, identity_probe_sql
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    producer, dirty = git_identity()
    binding, partition = candidate(artifact_path, manifest, code_sha=producer, dirty=dirty)
    target = binding["database_target_sha256"]
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("NAI probe target differs")

    def recheck():
        url, _ = load_db_url(env_file)
        current_sha, current_dirty = git_identity()
        current, current_partition = candidate(artifact_path, manifest, code_sha=current_sha, dirty=current_dirty)
        if (database_target_fingerprint_from_url(url).get("value") != target
                or current != binding or current_partition != partition):
            raise ValueError("NAI probe input or target changed")

    def source_refusal(cause):
        recheck()
        try:
            health = _run_psql_script(find_psql(), db_url,
                                      "BEGIN TRANSACTION READ ONLY; SELECT 1 AS healthy; ROLLBACK;", timeout=10)
            healthy = health.returncode == 0 and parse_tsv(health.stdout) == [{"healthy": "1"}]
        except (OSError, RuntimeError, ValueError, TypeError, SystemExit, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("NAI probe target health unknown") from exc
        recheck()
        if not healthy:
            raise RuntimeError("NAI probe target health unknown") from cause
        raise NaiSourceProbeRefusal("NAI source identity query refused") from cause

    sql = ("\\set VERBOSITY verbose\nBEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(partition["staged"]) + ";\nROLLBACK;\n")
    try:
        result = _run_psql_script(find_psql(), db_url, sql, timeout=60)
    except subprocess.TimeoutExpired as exc:
        source_refusal(exc)
    if result.returncode:
        state = re.search(r"ERROR:\s+([0-9A-Z]{5}):", getattr(result, "stderr", "") or "")
        if (result.returncode == 3 and state
                and (state.group(1).startswith("22") or state.group(1) in {"42601", "57014"})):
            source_refusal(RuntimeError("NAI source query refused"))
        raise RuntimeError("NAI identity query or target failed")
    recheck()
    rows = parse_tsv(result.stdout)
    count = str(len(partition["staged"]))
    if (len(rows) != 1 or set(rows[0]) != {"staged", "joined", "held", "preimage"}
            or rows[0]["staged"] != count or rows[0]["joined"] != count):
        raise RuntimeError("NAI probe scope differs")
    preimage_text = rows[0]["preimage"]
    try:
        held = json.loads(rows[0]["held"])
        before = json.loads(preimage_text, parse_float=Decimal)
        _partition(partition["staged"], held, before)
    except (ValueError, TypeError, KeyError) as exc:
        raise NaiSourceProbeRefusal("NAI target identity partition invalid") from exc
    if len(held) == len(partition["staged"]):
        raise NaiSourceProbeRefusal("NAI target has no unambiguous source posts")
    # Keep the exact JSON text as a string in claims; the ordinary manifest
    # encoder would round arbitrary numeric preimages through binary floats.
    return {**binding, "held": held, "preimage": preimage_text,
            "preimage_sha256": hashlib.sha256(preimage_text.encode("utf-8")).hexdigest()}


def admit(artifact_path, manifest, report, *, code_sha, dirty, phase):
    """Replace only the bound NAI count hold with a no-retirement post subset."""
    from cre_checkpoint_refresh import subset_gate_can_admit
    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts
    from nai_partial.partial import _partition

    if phase not in {"source", "aggregate"}:
        raise ValueError("NAI partial gate phase invalid")
    binding, partition = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty)
    probe = manifest["sources"][SOURCE].get("nai_post_identity_probe")
    if not isinstance(probe, dict) or any(probe.get(key) != value for key, value in binding.items()):
        raise ValueError("NAI target probe differs")
    preimage = probe.get("preimage")
    if (not isinstance(preimage, str)
            or hashlib.sha256(preimage.encode("utf-8")).hexdigest() != probe.get("preimage_sha256")):
        raise ValueError("NAI target preimage changed")
    before = json.loads(preimage, parse_float=Decimal)
    held = probe.get("held")
    _partition(partition["staged"], held, before)
    if len(held) == len(partition["staged"]):
        raise NaiSourceProbeRefusal("NAI target has no unambiguous source posts")
    current, errors, sources, _, rejects = count_artifacts([str(artifact_path)], True)
    if sources != {SOURCE} or errors.get(SOURCE) or rejects or type(current.get(SOURCE)) is not int:
        raise ValueError("NAI source gate counts differ")
    if candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty) != (binding, partition):
        raise ValueError("NAI source artifact changed during gate")
    report_manifest = manifest
    report_path = Path(artifact_path)
    if phase == "source" and manifest["sources"][SOURCE]["artifact"].get("nai_coverage_partial") is True:
        import copy
        from nai_partial.coverage_candidate import ORIGINAL
        report_manifest = copy.deepcopy(manifest)
        report_manifest["sources"][SOURCE]["artifact"]["path"] = ORIGINAL
        report_path = report_path.parent.parent / ORIGINAL
    validate_report_scope(report, report_manifest, SOURCE, report_path)
    summary, gate = report["summary"], report["per_source"][SOURCE]
    recovery = manifest["sources"][SOURCE]["artifact"].get("nai_inventory_recovery") is True
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != current[SOURCE]
            or (not recovery and gate.get("mark_missing_safe") is not False)):
        raise ValueError("NAI source gate scope invalid")
    if gate.get("verdict") == "ok_additive_verified_subset":
        raw = {"verdict": gate.get("raw_verdict"), "reason": gate.get("raw_reason")}
    else:
        raw = {"verdict": gate.get("verdict"), "reason": gate.get("reason")}
    if not ((raw["verdict"] == "hold" and subset_gate_can_admit(raw))
            or (recovery and raw["verdict"] in {"ok", "first_seen"})):
        raise ValueError("NAI gate is not a coverage-only hold")
    proof = {**binding, "schema": "nai-public-post-partial-v1", "scope": "nai_public_post_partial_v1",
             "source_key": SOURCE, "held": held, "preimage_sha256": probe["preimage_sha256"],
             "held_count": len(binding["source_held"]) + len(held),
             "emitted_count": len(partition["staged"]) - len(held), "whole_source_coverage": False}
    if phase == "aggregate" and manifest["sources"][SOURCE].get("gate", {}).get("current_inventory_proof") != proof:
        raise ValueError("NAI source gate proof changed")
    gate.update(raw_verdict=raw["verdict"], raw_reason=raw["reason"], verdict="ok_additive_verified_subset",
                reason="NAI source posts admitted as a partial subset; source coverage remains incomplete",
                mark_missing_safe=False, admission_scope="nai_public_post_partial_v1", current_inventory_proof=proof)
    for key in ("hold_sources", "mark_missing_safe_sources", "mark_missing_safe_brokerages"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != SOURCE]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get(SOURCE), dict):
        report["per_brokerage"][SOURCE]["mark_missing_safe"] = False
    counts = {}
    for source_gate in report["per_source"].values():
        verdict = source_gate.get("verdict")
        counts[verdict] = counts.get(verdict, 0) + 1
    summary["verdict_counts"] = counts
    report["scope"] = {"kind": ("nai_public_post_partial_v1" if phase == "source"
                                else "collector_registry_verified_subset"),
                       "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return proof


def ingest_subset_context(artifact_path, envelope, *, code_sha, dirty, target_sha256, attempt_id):
    """Reprove source and both gates immediately at the write boundary."""
    import copy
    import uuid

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("NAI partial claim invalid")
    manifest = copy.deepcopy(envelope["manifest"])
    if manifest["preflight"]["database_target"] != {"algorithm": "sha256", "value": target_sha256}:
        raise ValueError("NAI partial target changed")
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = copy.deepcopy(envelope[name])
        if (report["per_source"][SOURCE].get("current_inventory_proof") != binding
                or admit(artifact_path, manifest, report, code_sha=code_sha, dirty=dirty, phase=phase) != binding):
            raise ValueError("NAI partial gate claim changed")
    current, partition = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty)
    probe = manifest["sources"][SOURCE]["nai_post_identity_probe"]
    if any(current[key] != binding[key] for key in current):
        raise ValueError("NAI source binding changed")
    job = str(uuid.uuid5(uuid.NAMESPACE_URL,
                         "nai-public-post-partial/v1/" + target_sha256 + "/" + current["generation_id"]
                         + "/" + current["artifact_sha256"] + "/" + attempt_id))
    info = manifest["sources"][SOURCE]["artifact"]
    return {"binding": binding, "staged": partition["staged"], "held": probe["held"],
            "preimage": json.loads(probe["preimage"], parse_float=Decimal), "job_id": job,
            "started_at": info["started_at"], "finished_at": info["finished_at"], "attempt_id": attempt_id}


def _require_active_checkpoint(args, claims_raw, envelope):
    """Bind execution to a direct coordinator holding the real lock FDs."""
    from cre_checkpoint_refresh import canonical_shared_lock_dir
    from cre_code_handoff_git import read_file
    from cre_tier_dispatch import verify_inherited_lock
    from reporting.partial_qualification import _exact_json_equal

    verify_inherited_lock(allow_recovery_required=True, require_direct_parent=True)
    authority_fd = int(os.environ["CRE_TIER_LOCK_AUTHORITY_FD"])
    sync_fd = int(os.environ["CRE_TIER_LOCK_SYNC_FD"])
    artifact = Path(args.inputs[0])
    run_dir = artifact.parent.parent
    claims_path = Path(args.nai_post_claims)
    if (not artifact.is_absolute() or not claims_path.is_absolute()
            or not claims_path.is_relative_to(run_dir)
            or not claims_path.resolve().is_relative_to(run_dir.resolve())):
        raise ValueError("NAI checkpoint path differs")
    current = json.loads(read_file(run_dir / "manifest.json"))
    source = current["sources"][SOURCE]
    claims = source["nai_post_claims"]
    if (current.get("run_id") != run_dir.name
            or (current.get("preflight") or {}).get("shared_lock") != str(canonical_shared_lock_dir())
            or (current.get("preflight") or {}).get("database_target") != {"algorithm": "sha256", "value": args.expected_db_target_sha256}
            or current.get("collector_git_sha") != envelope["manifest"].get("collector_git_sha")
            or source.get("subset_ingest_attempt_id") != args.checkpoint_ingest_attempt
            or claims.get("attempt_id") != args.checkpoint_ingest_attempt
            or claims.get("path") != str(claims_path)
            or claims.get("sha256") != hashlib.sha256(claims_raw).hexdigest()
            or (source.get("artifact") or {}).get("path") != str(artifact.relative_to(run_dir))
            or (source.get("artifact") or {}).get("sha256") != envelope["binding"].get("artifact_sha256")
            or not _exact_json_equal((source.get("gate") or {}).get("current_inventory_proof"), envelope["binding"])):
        raise ValueError("NAI active checkpoint binding differs")
    if not args.dry_run and (source.get("state") != "ingesting"
            or (source.get("ingest") or {}).get("attempt_id") != args.checkpoint_ingest_attempt
            or (source.get("ingest") or {}).get("rc") is not None):
        raise ValueError("NAI active checkpoint write attempt differs")
    return authority_fd, sync_fd


def run_ingest(args):
    """Execute only an owner-bound NAI partial attempt, or emit its dry run."""
    import sys
    import tempfile

    from cre_checkpoint_refresh import git_identity
    from cre_ingest import (_status_activation_enabled, assert_expected_database_target,
                            find_psql, load_db_url, psql_connection_args, psql_connection_env)
    from nai_partial.partial import build_partial_sql
    from cre_stop_guarded_tier import _read_at, _safe_stat

    other_claims = ("franklin_site_claims", "lyon_partial_claims", "colliers_partial_claims",
                    "jll_partial_claims", "daum_verified_subset_claims", "pyramid_verified_subset_claims",
                    "matthews_targeted_claims", "newmark_page_attempt", "newmark_page_claims",
                    "checkpoint_collector_git_sha", "checkpoint_pyramid_code_sha", "checkpoint_jll_code_sha",
                    "checkpoint_jll_transactions")
    if (len(args.inputs) != 1 or args.mark_missing or _status_activation_enabled(args.activate_status)
            or not args.expected_db_target_sha256 or not args.checkpoint_ingest_attempt
            or any(getattr(args, key, None) for key in other_claims)):
        raise ValueError("unbound NAI partial ingest")
    claims = Path(args.nai_post_claims)
    directory = os.open(claims.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        raw, _ = _read_at(directory, claims.name, limit=64 * 1024 * 1024)
        envelope = json.loads(raw)
    finally:
        os.close(directory)
    inherited_fds = _require_active_checkpoint(args, raw, envelope)
    artifact_info = envelope["manifest"]["sources"][SOURCE]["artifact"]
    if not (artifact_info.get("nai_inventory_recovery") is True
            or artifact_info.get("nai_coverage_partial") is True):
        raise ValueError("NAI partial requires an intrinsically marked artifact")
    code_sha, dirty = git_identity()
    context = ingest_subset_context(Path(args.inputs[0]), envelope, code_sha=code_sha, dirty=dirty,
                                    target_sha256=args.expected_db_target_sha256,
                                    attempt_id=args.checkpoint_ingest_attempt)
    sql = build_partial_sql(context["staged"], context["held"], context["preimage"], context["job_id"],
                            context["started_at"], context["finished_at"])

    def write_and_run(out_dir):
        out = Path(out_dir)
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("NAI SQL directory is not private")
        descriptor = os.open(out / "ingest.sql", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(sql)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        if args.dry_run:
            _require_active_checkpoint(args, raw, envelope)
            return
        current_sha, current_dirty = git_identity()
        current = ingest_subset_context(Path(args.inputs[0]), envelope, code_sha=current_sha, dirty=current_dirty,
                                        target_sha256=args.expected_db_target_sha256,
                                        attempt_id=args.checkpoint_ingest_attempt)
        if current != context:
            raise ValueError("NAI partial inputs changed before ingest")
        db_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(db_url, args.expected_db_target_sha256)
        _require_active_checkpoint(args, raw, envelope)
        proc = subprocess.run([find_psql(), *psql_connection_args(db_url), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                              input=sql, text=True, env=psql_connection_env(db_url), stdout=sys.stdout,
                              stderr=sys.stderr, check=False, pass_fds=inherited_fds)
        if proc.returncode:
            raise RuntimeError("NAI partial ingest outcome requires reconciliation")
        current_url, _ = load_db_url(args.env_file)
        _require_active_checkpoint(args, raw, envelope)
        assert_expected_database_target(current_url, args.expected_db_target_sha256)

    if args.keep_artifacts:
        write_and_run(args.keep_artifacts)
    else:
        with tempfile.TemporaryDirectory(prefix="cre-nai-post-") as directory:
            write_and_run(directory)


def run_readback(artifact_path, envelope, *, target_sha256, attempt_id, env_file):
    """Read the exact job, held parents, source index and five child sets."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from nai_partial.partial import partial_readback_sql
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    producer, dirty = git_identity()
    context = ingest_subset_context(artifact_path, envelope, code_sha=producer, dirty=dirty,
                                    target_sha256=target_sha256, attempt_id=attempt_id)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("NAI readback target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + PIN_STRING_LITERALS + "\n"
           + partial_readback_sql(context["staged"], context["held"], context["preimage"], context["job_id"],
                                  context["started_at"], context["finished_at"]) + ";\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=60)
    if result.returncode:
        raise RuntimeError("NAI partial readback failed")
    rows = parse_tsv(result.stdout)
    if (len(rows) != 1 or set(rows[0]) != {"jobs", "observed", "mismatches"}
            or rows[0]["jobs"] != "1" or rows[0]["observed"] != str(len(context["staged"]))
            or rows[0]["mismatches"] != "0"):
        raise ValueError("NAI partial readback does not match")
    url, _ = load_db_url(env_file)
    current_sha, current_dirty = git_identity()
    again = ingest_subset_context(artifact_path, envelope, code_sha=current_sha, dirty=current_dirty,
                                  target_sha256=target_sha256, attempt_id=attempt_id)
    if database_target_fingerprint_from_url(url).get("value") != target_sha256 or again != context:
        raise ValueError("NAI readback input or target changed")
    return {"ok": True, "kind": "nai_public_post_partial", "whole_source_coverage": False,
            "job_id": context["job_id"], "attempt_id": attempt_id,
            "generation_id": context["binding"]["generation_id"],
            "artifact_sha256": context["binding"]["artifact_sha256"],
            "database_target_sha256": target_sha256, "staged_count": len(context["staged"]),
            "partition_sha256": context["binding"]["partition_sha256"],
            "preimage_sha256": context["binding"]["preimage_sha256"],
            "emitted_count": len(context["staged"]) - len(context["held"]),
            "held_count": context["binding"]["held_count"], "children_verified": True,
            "new_missing_canonical_count": sum(item["listing"] is None and item["external_id"] not in context["held"]
                                               for item in context["preimage"])}
