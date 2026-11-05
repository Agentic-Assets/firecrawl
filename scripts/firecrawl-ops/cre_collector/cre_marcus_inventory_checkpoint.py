"""Owner-bound local Marcus inventory candidate; no network or DB writes."""
import hashlib
import json
import os
from pathlib import Path

from cre_marcus_inventory import candidate_data, rederive_settled_artifact


def run_replay_worker(run_dir, manifest, owner, save):
    """Seal a candidate from an already settled, rejected source artifact.

    The caller must have reaped the collector group and authenticated its
    attempt receipt. This function never grants gate admission.
    """
    import cre_checkpoint_refresh as refresh
    from cre_colliers_partial_checkpoint import _read_private_file
    from cre_stop_guarded_tier import StopRefused, _safe_stat

    run_dir = Path(run_dir)
    if not run_dir.is_absolute() or not isinstance(owner, refresh.SharedLock) or not callable(save):
        raise ValueError("Marcus inventory owner invalid")

    def check_owner():
        owner._require_authority()
        descriptor = owner._owned_directory_fd()
        os.close(descriptor)
        if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
            raise ValueError("Marcus inventory lock differs")

    check_owner()
    config = manifest["config"]
    if (config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"]
            or "marcus-millichap" not in config.get("sources", [])):
        raise ValueError("Marcus inventory scope invalid")
    checkpoint = manifest["sources"]["marcus-millichap"]
    if checkpoint.get("marcus_inventory_worker") is not None:
        raise ValueError("Marcus inventory replay already attempted")
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("Marcus inventory source attempt missing")
    number = attempts[-1].get("number")
    producer, dirty = refresh.git_identity()
    if dirty is not False or producer != manifest.get("collector_git_sha"):
        raise ValueError("Marcus inventory code changed")
    try:
        source = rederive_settled_artifact(run_dir, manifest, number, producer, dirty)
        if not source["partition"]["staged"]:
            raise ValueError("Marcus inventory has no qualified identities")
    except (OSError, ValueError, TypeError, KeyError, StopRefused):
        check_owner()
        checkpoint["marcus_inventory_worker"] = {"state": "source_held", "attempt": number}
        checkpoint["state"] = "partial_held_no_write"
        save(run_dir, manifest)
        return None
    checkpoint["marcus_inventory_worker"] = {"state": "preparing", "attempt": number}
    save(run_dir, manifest)
    try:
        candidate = candidate_data(source)
        encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
        name = f"marcus-millichap-inventory-attempt-{number}.json"
        directory = os.open(run_dir / "rejected", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _safe_stat(directory, directory=True, private=True)
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.fsync(directory)
        finally:
            os.close(directory)
        path = run_dir / "rejected" / name
        check_owner()
        if (refresh.git_identity() != (producer, False)
                or rederive_settled_artifact(run_dir, manifest, number, producer, False) != source
                or _read_private_file(path, len(encoded)) != encoded):
            raise ValueError("Marcus inventory replay bytes or code changed")
        identity = path.stat()
        checkpoint["candidate"] = {"path": str(path.relative_to(run_dir)), "attempt_started_at": source["started_at"],
            "attempt_number": number, "file_identity": [identity.st_dev, identity.st_ino, identity.st_size,
                                                           identity.st_mtime_ns, identity.st_ctime_ns],
            "bytes": identity.st_size}
        checkpoint["marcus_inventory_worker"].update(state="settled", sha256=hashlib.sha256(encoded).hexdigest())
        checkpoint["state"] = "candidate"
        save(run_dir, manifest)
        return path
    except BaseException as exc:
        checkpoint.pop("candidate", None)
        checkpoint["marcus_inventory_worker"]["state"] = "failed"
        try:
            save(run_dir, manifest)
        except BaseException as save_error:
            raise BaseExceptionGroup("Marcus inventory replay and checkpoint save failed", [exc, save_error]) from exc
        raise


def verify_candidate_artifact(artifact_path, manifest, *, current_code_sha, current_git_dirty):
    """Recheck source and candidate bytes. Caller retains owner and DB fences."""
    from cre_colliers_partial_checkpoint import _read_private_file

    path = Path(artifact_path)
    run_dir = path.parent.parent
    checkpoint = (manifest.get("sources") or {}).get("marcus-millichap") or {}
    worker, candidate = checkpoint.get("marcus_inventory_worker"), checkpoint.get("candidate")
    if (not isinstance(worker, dict) or worker.get("state") != "settled"
            or type(worker.get("attempt")) is not int or worker["attempt"] < 1
            or not isinstance(candidate, dict)):
        raise ValueError("Marcus inventory worker unsettled")
    number = worker["attempt"]
    relative = f"rejected/marcus-millichap-inventory-attempt-{number}.json"
    if path != run_dir / relative or candidate.get("path") != relative or candidate.get("attempt_number") != number:
        raise ValueError("Marcus inventory candidate path differs")
    source = rederive_settled_artifact(run_dir, manifest, number, current_code_sha, current_git_dirty)
    size = path.lstat().st_size
    raw = _read_private_file(path, size)
    if (len(raw) != size or candidate.get("bytes") != size
            or hashlib.sha256(raw).hexdigest() != worker.get("sha256")
            or json.loads(raw) != candidate_data(source)):
        raise ValueError("Marcus inventory candidate differs from source")
    return {"run_dir": run_dir, "source": source, "staged": source["partition"]["staged"],
            "sha256": worker["sha256"], "bytes": size}


def candidate_structure(data):
    """Check the candidate's own structure; source custody remains separate."""
    import re
    from cre_marcus_inventory import stage_envelope
    from cre_marcus_inventory_db import validate_staged

    if not isinstance(data, dict) or not isinstance(data.get("binding"), dict):
        raise ValueError("Marcus inventory candidate invalid")
    binding = data["binding"]
    if (set(binding) != {"generation", "attempt", "startedAt", "finishedAt", "coordinatorFinishedAt",
                        "sourceSha256", "sourceBytes", "producerSha", "targetSha256"}
            or not isinstance(binding["generation"], str) or not binding["generation"]
            or type(binding["attempt"]) is not int or binding["attempt"] < 1
            or type(binding["sourceBytes"]) is not int or binding["sourceBytes"] < 1
            or not isinstance(binding["producerSha"], str) or not re.fullmatch(r"[a-f0-9]{40}", binding["producerSha"])
            or any(not isinstance(binding[key], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[key])
                   for key in ("sourceSha256", "targetSha256"))
            or type(data.get("detailErrors")) is not int or data["detailErrors"] < 1
            or type(data.get("detailQualified")) is not int or data["detailQualified"] != 0
            or not isinstance(data.get("listings"), list)
            or not isinstance(data.get("held"), list)):
        raise ValueError("Marcus inventory candidate binding invalid")
    held = data["held"]
    if (any(not isinstance(item, dict) or set(item) != {"index", "reason"}
            or type(item["index"]) is not int or item["index"] < 0
            or item["reason"] not in {"unqualified_inventory_identity", "conflicting_inventory_identity"}
            for item in held)
            or [item["index"] for item in held] != sorted({item["index"] for item in held})):
        raise ValueError("Marcus inventory held partition invalid")
    staged = []
    for listing in data["listings"]:
        if not isinstance(listing, dict) or set(listing) != {"sourceKey", "id", "url", "marcusInventoryRecord"}:
            raise ValueError("Marcus inventory candidate listing invalid")
        staged.append(stage_envelope(listing["marcusInventoryRecord"], binding["generation"],
                                      binding["startedAt"], binding["finishedAt"]))
    validate_staged(staged, binding["generation"], binding["startedAt"], binding["finishedAt"])
    generation_start = data["runMeta"]["freshness"]["generationStartedAt"]
    source = {"schema": "marcus-inventory-custody/v1", "source": "marcus-millichap",
              "generation": binding["generation"], "generation_started_at": generation_start,
              "attempt": binding["attempt"], "started_at": binding["startedAt"], "finished_at": binding["finishedAt"],
              "coordinator_finished_at": binding["coordinatorFinishedAt"], "source_sha256": binding["sourceSha256"],
              "source_bytes": binding["sourceBytes"], "producer_sha": binding["producerSha"],
              "target_sha256": binding["targetSha256"], "detail_errors": data["detailErrors"],
              "detail_qualified": 0, "coverage": "partial", "partition": {"staged": staged, "held": data["held"]}}
    if candidate_data(source) != data:
        raise ValueError("Marcus inventory candidate differs")
    return source


class MarcusInventoryProbeRefusal(ValueError):
    """A settled source-local target probe refused the offered partition."""


def run_identity_probe(artifact_path, manifest, env_file):
    """Probe one bound target read-only; write only a private local preimage."""
    import re
    import secrets
    import subprocess
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_marcus_inventory_db import _validate_partition, identity_probe_sql
    from cre_stop_guarded_tier import _safe_stat
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    producer, dirty = git_identity()
    if dirty or producer != manifest.get("collector_git_sha"):
        raise ValueError("Marcus probe code changed")
    verified = verify_candidate_artifact(artifact_path, manifest, current_code_sha=producer, current_git_dirty=dirty)
    source, staged = verified["source"], verified["staged"]
    target = source["target_sha256"]
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("Marcus probe target changed")

    def recheck():
        current_url, _ = load_db_url(env_file)
        after = verify_candidate_artifact(artifact_path, manifest, current_code_sha=producer, current_git_dirty=False)
        if (database_target_fingerprint_from_url(current_url).get("value") != target
                or git_identity() != (producer, False) or after["source"] != source or after["staged"] != staged):
            raise ValueError("Marcus probe input or target changed")

    def source_refusal(cause):
        recheck()
        try:
            health = _run_psql_script(find_psql(), db_url,
                "BEGIN TRANSACTION READ ONLY;\nSELECT 1 AS healthy;\nROLLBACK;\n", timeout=10)
            healthy = health.returncode == 0 and parse_tsv(health.stdout) == [{"healthy": "1"}]
        except (OSError, RuntimeError, ValueError, TypeError, SystemExit, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Marcus probe target health is unknown") from exc
        recheck()
        if not healthy:
            raise RuntimeError("Marcus probe target health is unknown") from cause
        raise MarcusInventoryProbeRefusal("Marcus read-only identity query was refused") from cause

    sql = ("\\set VERBOSITY verbose\nBEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(staged, source["generation"],
               source["started_at"], source["finished_at"]) + ";\nROLLBACK;\n")
    try:
        result = _run_psql_script(find_psql(), db_url, sql, timeout=60)
    except subprocess.TimeoutExpired as exc:
        source_refusal(exc)
    if result.returncode:
        state = re.search(r"ERROR:\s+([0-9A-Z]{5}):", getattr(result, "stderr", "") or "")
        if (result.returncode == 3 and state
                and (state.group(1).startswith("22") or state.group(1) in {"42601", "57014"})):
            source_refusal(RuntimeError("Marcus source query was refused"))
        raise RuntimeError("Marcus identity probe target or query failed")
    recheck()
    parsed = parse_tsv(result.stdout)
    if (len(parsed) != 1 or set(parsed[0]) != {"staged", "joined", "held", "preimage"}
            or parsed[0]["staged"] != str(len(staged)) or parsed[0]["joined"] != str(len(staged))):
        raise RuntimeError("Marcus probe query scope differs")
    try:
        held, preimage = json.loads(parsed[0]["held"]), json.loads(parsed[0]["preimage"])
        _validate_partition(staged, held, preimage, source["generation"], source["started_at"], source["finished_at"])
    except (ValueError, TypeError, KeyError) as exc:
        raise MarcusInventoryProbeRefusal("Marcus source identity partition was invalid") from exc
    encoded = json.dumps(preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    staged_bytes = json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    name = f"marcus-millichap-preimage-{source['attempt']}-{secrets.token_hex(8)}.json"
    directory = os.open(verified["run_dir"] / "rejected", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.fsync(directory)
    finally:
        os.close(directory)
    return {"artifact_sha256": verified["sha256"], "generation_id": source["generation"],
            "producer_sha": producer, "database_target_sha256": target,
            "staged_sha256": hashlib.sha256(staged_bytes).hexdigest(), "held": held,
            "preimage_path": f"rejected/{name}", "preimage_bytes": len(encoded),
            "preimage_sha256": hashlib.sha256(encoded).hexdigest()}


def load_probe_preimage(run_dir, attempt, probe):
    import re
    from cre_colliers_partial_checkpoint import _read_private_file

    relative = probe.get("preimage_path") if isinstance(probe, dict) else None
    if not isinstance(relative, str) or not re.fullmatch(
            rf"rejected/marcus-millichap-preimage-{attempt}-[a-f0-9]{{16}}\.json", relative):
        raise ValueError("Marcus inventory preimage path invalid")
    size = probe.get("preimage_bytes")
    if type(size) is not int or size < 1:
        raise ValueError("Marcus inventory preimage size invalid")
    raw = _read_private_file(Path(run_dir) / relative, size)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != probe.get("preimage_sha256"):
        raise ValueError("Marcus inventory preimage changed")
    result = json.loads(raw)
    if not isinstance(result, list):
        raise ValueError("Marcus inventory preimage invalid")
    return result


def admit_inventory(artifact_path, manifest, report, *, current_code_sha, current_git_dirty, phase):
    """Bind source and target proof to the existing additive coverage gate."""
    from copy import deepcopy
    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts
    from cre_marcus_inventory_db import _validate_partition

    config = manifest["config"]
    if (phase not in ("source", "aggregate") or config.get("additive") is not True
            or config.get("mark_missing") is not False or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or "marcus-millichap" not in config.get("sources", [])):
        raise ValueError("Marcus inventory gate scope invalid")
    verified = verify_candidate_artifact(artifact_path, manifest, current_code_sha=current_code_sha,
                                         current_git_dirty=current_git_dirty)
    source, staged = verified["source"], verified["staged"]
    checkpoint = manifest["sources"]["marcus-millichap"]
    probe = checkpoint.get("marcus_inventory_probe")
    staged_bytes = json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    staged_sha = hashlib.sha256(staged_bytes).hexdigest()
    if (not isinstance(probe, dict) or probe.get("artifact_sha256") != verified["sha256"]
            or probe.get("generation_id") != manifest["run_id"] or probe.get("producer_sha") != current_code_sha
            or probe.get("database_target_sha256") != source["target_sha256"] or probe.get("staged_sha256") != staged_sha):
        raise ValueError("Marcus inventory probe differs")
    held = probe.get("held")
    preimage = load_probe_preimage(verified["run_dir"], source["attempt"], probe)
    _validate_partition(staged, held, preimage, source["generation"], source["started_at"], source["finished_at"])
    current, errors, sources, _, rejects = count_artifacts([str(artifact_path)], True)
    if (sources != {"marcus-millichap"} or errors.get("marcus-millichap") != "original source details unverified"
            or rejects != 0 or current.get("marcus-millichap") != len(staged)):
        raise ValueError("Marcus inventory gate evidence differs")
    validate_report_scope(report, manifest, "marcus-millichap", artifact_path)
    summary, gate = report["summary"], report["per_source"]["marcus-millichap"]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != len(staged) or gate.get("mark_missing_safe") is not False):
        raise ValueError("Marcus inventory coverage gate differs")
    verdict = "ok_additive_verified_subset"
    reasons = {("first_seen", "no baseline row; cannot gate (first sight); a source pass also errored this run"),
               ("hold", "source pass error: original source details unverified")}
    pair = ((gate.get("raw_verdict"), gate.get("raw_reason")) if gate.get("verdict") == verdict
            else (gate.get("verdict"), gate.get("reason")))
    if pair not in reasons:
        raise ValueError("Marcus inventory gate reason differs")
    binding = {"schema": "marcus-inventory-partial-v1", "scope": "marcus_inventory_partial_v1",
               "source_key": "marcus-millichap", "artifact_sha256": verified["sha256"],
               "source_sha256": source["source_sha256"], "producer_sha": current_code_sha,
               "database_target_sha256": source["target_sha256"], "generation_id": manifest["run_id"],
               "generation_started_at": manifest["started_at"], "attempt": source["attempt"],
               "staged_sha256": staged_sha, "preimage_sha256": probe["preimage_sha256"], "held": held,
               "held_count": len(held) + len(source["partition"]["held"]), "emitted_count": len(staged) - len(held),
               "detail_qualified": 0, "detail_errors": source["detail_errors"],
               "original_source_outcome": "detail_freshness_rejected", "whole_source_coverage": False}
    if phase == "aggregate" and checkpoint.get("gate", {}).get("current_inventory_proof") != binding:
        raise ValueError("Marcus source gate changed")
    gate.update(raw_verdict=pair[0], raw_reason=pair[1], verdict=verdict,
                reason="source-bound Marcus inventory observations admitted additively; detail unverified",
                mark_missing_safe=False, admission_scope="marcus_inventory_partial_v1", current_inventory_proof=binding)
    for key in ("hold_sources", "mark_missing_safe_sources", "mark_missing_safe_brokerages"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != "marcus-millichap"]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get("marcus-millichap"), dict):
        report["per_brokerage"]["marcus-millichap"]["mark_missing_safe"] = False
    if isinstance(summary.get("verdict_counts"), dict):
        counts = {}
        for item in report["per_source"].values():
            value = item.get("verdict")
            counts[value] = counts.get(value, 0) + 1
        summary["verdict_counts"] = counts
    report["scope"] = {"kind": ("marcus_inventory_partial_v1" if phase == "source" else "collector_registry_verified_subset"),
                       "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return deepcopy(binding)


def ingest_subset_context(artifact_path, envelope, *, current_code_sha, current_git_dirty,
                          expected_database_target_sha256, attempt_id):
    """Reprove source and both subset gates immediately before SQL creation."""
    import re
    import uuid
    from copy import deepcopy

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("Marcus inventory claims invalid")
    manifest = deepcopy(envelope["manifest"])
    if manifest["preflight"]["database_target"] != {"algorithm": "sha256", "value": expected_database_target_sha256}:
        raise ValueError("Marcus inventory target changed")
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = deepcopy(envelope[name])
        if (report["per_source"]["marcus-millichap"].get("current_inventory_proof") != binding
                or admit_inventory(artifact_path, manifest, report, current_code_sha=current_code_sha,
                                   current_git_dirty=current_git_dirty, phase=phase) != binding):
            raise ValueError("Marcus inventory gate proof changed")
    verified = verify_candidate_artifact(artifact_path, manifest, current_code_sha=current_code_sha,
                                         current_git_dirty=current_git_dirty)
    probe = manifest["sources"]["marcus-millichap"]["marcus_inventory_probe"]
    preimage = load_probe_preimage(verified["run_dir"], verified["source"]["attempt"], probe)
    return {"binding": binding, "staged": verified["staged"], "held": probe["held"], "preimage": preimage,
            "source": verified["source"], "job_id": str(uuid.UUID(attempt_id)), "attempt_id": attempt_id}


def run_partial_readback(artifact_path, envelope, *, code_sha, target, attempt_id, env_file):
    """Read back exact owned postconditions without retrying an uncertain write."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_marcus_inventory_db import partial_readback_sql
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    def context():
        producer, dirty = git_identity()
        if producer != code_sha or dirty is not False:
            raise ValueError("Marcus readback code changed")
        return ingest_subset_context(artifact_path, envelope, current_code_sha=producer, current_git_dirty=dirty,
            expected_database_target_sha256=target, attempt_id=attempt_id)

    before = context()
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("Marcus readback target changed")
    sql = partial_readback_sql(before["staged"], before["held"], before["preimage"], before["source"], before["job_id"])
    result = _run_psql_script(find_psql(), db_url,
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + PIN_STRING_LITERALS
        + "\n" + sql + ";\nROLLBACK;\n", timeout=60)
    if result.returncode:
        raise RuntimeError("Marcus inventory readback query failed")
    parsed = parse_tsv(result.stdout)
    current_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(current_url).get("value") != target or context() != before:
        raise ValueError("Marcus inventory readback input or target changed")
    if parsed != [{"jobs": "1", "observed": str(len(before["staged"])), "mismatches": "0"}]:
        raise ValueError("Marcus inventory readback postconditions differ")
    held = set(before["held"])
    new_missing = sum(item["listing"] is None and item["external_id"] not in held for item in before["preimage"])
    return {"ok": True, "outcome": "exact_commit", "artifact_sha256": before["binding"]["artifact_sha256"],
            "generation_id": before["source"]["generation"], "attempt_id": attempt_id,
            "database_target_sha256": target, "expected": before["binding"]["emitted_count"],
            "held": before["held"], "new_missing_canonical_count": new_missing,
            "staged_sha256": before["binding"]["staged_sha256"], "detail_qualified": 0,
            "checked": ["partial_job", "projected_scalars", "source_history", "parent_lifecycle",
                        "source_index", "contacts", "documents", "images", "media", "links", "held_parents"],
            "whole_source_coverage": False}


def run_ingest(args):
    """Execute a source-bound additive claim within the owned checkpoint process."""
    import stat
    import subprocess
    import sys
    import tempfile
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import (_status_activation_enabled, assert_expected_database_target, find_psql,
                            load_db_url, psql_connection_args, psql_connection_env)
    from cre_marcus_inventory_db import build_partial_sql
    from cre_stop_guarded_tier import _read_at, _safe_stat

    if (len(args.inputs) != 1 or args.mark_missing or _status_activation_enabled(args.activate_status)
            or not args.require_strict_freshness or not args.expected_db_target_sha256
            or not args.checkpoint_ingest_attempt or args.franklin_site_claims or args.colliers_partial_claims
            or args.lyon_partial_claims or args.jll_partial_claims or args.daum_verified_subset_claims
            or args.pyramid_verified_subset_claims or args.matthews_targeted_claims
            or args.newmark_page_attempt or args.newmark_page_claims or args.checkpoint_collector_git_sha
            or args.checkpoint_pyramid_code_sha or args.checkpoint_jll_code_sha):
        raise ValueError("unbound Marcus inventory ingest")
    claims = Path(args.marcus_inventory_claims)
    directory = os.open(claims.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        raw, _ = _read_at(directory, claims.name, limit=64 * 1024 * 1024)
        envelope = json.loads(raw)
    finally:
        os.close(directory)
    code_sha, dirty = git_identity()
    context = ingest_subset_context(Path(args.inputs[0]), envelope, current_code_sha=code_sha, current_git_dirty=dirty,
                                    expected_database_target_sha256=args.expected_db_target_sha256,
                                    attempt_id=args.checkpoint_ingest_attempt)
    sql = build_partial_sql(context["staged"], context["held"], context["preimage"], context["source"], context["job_id"])

    def write_and_run(out_dir):
        out = Path(out_dir)
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = out.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Marcus inventory SQL directory is not private")
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
        current_code, current_dirty = git_identity()
        current = ingest_subset_context(Path(args.inputs[0]), envelope, current_code_sha=current_code,
                                        current_git_dirty=current_dirty,
                                        expected_database_target_sha256=args.expected_db_target_sha256,
                                        attempt_id=args.checkpoint_ingest_attempt)
        if current != context:
            raise ValueError("Marcus inventory inputs changed before ingest")
        db_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(db_url, args.expected_db_target_sha256)
        proc = subprocess.run([find_psql(), *psql_connection_args(db_url), "-X", "-q",
                               "-v", "ON_ERROR_STOP=1", "-f", "-"],
                              input=sql, text=True, env=psql_connection_env(db_url),
                              stdout=sys.stdout, stderr=sys.stderr, check=False)
        if proc.returncode:
            raise RuntimeError("Marcus inventory ingest outcome requires reconciliation")
        current_url, _ = load_db_url(args.env_file)
        assert_expected_database_target(current_url, args.expected_db_target_sha256)

    if args.keep_artifacts:
        write_and_run(args.keep_artifacts)
    else:
        with tempfile.TemporaryDirectory(prefix="cre-marcus-inventory-") as out_dir:
            write_and_run(out_dir)
