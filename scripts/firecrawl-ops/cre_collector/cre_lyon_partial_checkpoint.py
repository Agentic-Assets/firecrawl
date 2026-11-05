"""Authenticate and replay the source-owned Lyon search capture.

This read-only helper never grants gate admission or starts database writes.
The caller must authenticate the manifest and hold the canonical owner lock.
"""
import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cre_colliers_partial_checkpoint import _read_private_file
from cre_lyon_partial import _validate_staged, stage_record
from cre_lyon_search_replay import replay_search_lines, validate_source_artifact
from cre_partial_page_custody import expected_path, settle


class LyonSourceProbeRefusal(ValueError):
    """A settled read-only probe returned an invalid source partition."""


def replay_settled_source(run_dir, manifest, attempt_number, current_code_sha, current_git_dirty):
    """Revalidate source custody, outcomes, and exact rows under the owner."""
    run_dir = Path(run_dir)
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_dir.name
            or not isinstance(current_code_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)
            or current_git_dirty is not False or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or type(attempt_number) is not int or attempt_number < 1):
        raise ValueError("Lyon source owner changed")
    target = (manifest.get("preflight") or {}).get("database_target")
    if (not isinstance(target, dict) or target.get("algorithm") != "sha256"
            or not isinstance(target.get("value"), str) or not re.fullmatch(r"[a-f0-9]{64}", target["value"])):
        raise ValueError("Lyon source target invalid")

    def directories():
        result = []
        for directory in (run_dir, run_dir / "rejected", run_dir / "lyon-stahl-partial"):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Lyon source directory invalid")
            result.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        return result

    before = directories()
    checkpoint = (manifest.get("sources") or {}).get("lyon-stahl") or {}
    attempts = checkpoint.get("attempts", [])
    numbers = [item.get("number") for item in attempts if isinstance(item, dict)]
    matches = [item for item in attempts if isinstance(item, dict) and item.get("number") == attempt_number]
    if (len(matches) != 1 or not numbers or any(type(number) is not int for number in numbers)
            or attempt_number != max(numbers)):
        raise ValueError("Lyon attempt differs")
    attempt = matches[0]
    from cre_colliers_partial import _time
    if (type(attempt.get("rc")) is not int or attempt["rc"] != 80 or not attempt.get("finished_at")
            or _time(attempt.get("started_at")) < _time(manifest.get("started_at"))
            or _time(attempt["finished_at"]) < _time(attempt.get("started_at"))
            or _time(attempt["finished_at"]) > datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("Lyon attempt unsettled")
    state = attempt.get("lyon-stahl_partial")
    relative = str(expected_path(run_dir, attempt_number, "lyon-stahl").relative_to(run_dir))
    main_rel = f"rejected/lyon-stahl-attempt-{attempt_number}.json"
    if (not isinstance(state, dict) or state.get("state") != "sealed_local" or state.get("path") != relative
            or state.get("outcome") != "source_error" or state.get("coverage") != "unverified"
            or attempt.get("rejected_artifact") != main_rel
            or state.get("main_sha256") != attempt.get("rejected_artifact_sha256")):
        raise ValueError("Lyon source receipt differs")
    probe = deepcopy(attempt)
    probe["lyon-stahl_partial"] = {"path": relative, "state": "pending"}
    settle(run_dir, probe, run_dir / main_rel, "lyon-stahl")
    keys = ("state", "path", "coverage", "outcome", "bytes", "observationRecords", "sha256", "main_sha256")
    if directories() != before or any(probe["lyon-stahl_partial"].get(key) != state.get(key) for key in keys):
        raise ValueError("Lyon source receipt changed")
    from source_evidence.automatic import _read_private
    main_path = run_dir / main_rel
    main_size = main_path.lstat().st_size
    main_raw = _read_private(main_path, main_size)
    if len(main_raw) != main_size:
        raise ValueError("Lyon source main artifact size changed")
    if hashlib.sha256(main_raw).hexdigest() != state["main_sha256"]:
        raise ValueError("Lyon source main artifact changed")
    summary = validate_source_artifact(json.loads(main_raw))

    # A response is acquired with the existing 32 MiB bound. Stream the three
    # JSONL snapshots so JSON escaping cannot create an arbitrary whole-spool
    # admission ceiling. The sealed digest and file identity remain authoritative.
    from cre_stop_guarded_tier import _identity, _safe_stat
    path = run_dir / relative
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            original = _safe_stat(descriptor, private=True)
            digest, size, count = hashlib.sha256(), 0, 0
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                def lines():
                    nonlocal size, count
                    for raw_line in source:
                        if not raw_line.endswith(b"\n"):
                            raise ValueError("Lyon source line is incomplete")
                        digest.update(raw_line)
                        size += len(raw_line)
                        if count:
                            page = json.loads(raw_line)
                            observed = _time(page.get("observedAt"))
                            del page
                            if not (_time(attempt["started_at"]) <= observed <= _time(attempt["finished_at"])):
                                raise ValueError("Lyon observation is outside its source attempt")
                        count += 1
                        yield raw_line

                records, held = replay_search_lines(lines(), run_dir.name, attempt_number, summary=summary)
            current = os.fstat(descriptor)
            named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (_identity(original) != _identity(current) or _identity(current) != _identity(named)
                    or size != state["bytes"] or digest.hexdigest() != state["sha256"]
                    or count != state["observationRecords"] + 1):
                raise ValueError("Lyon source bytes changed")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    staged = [stage_record(record) for record in records]
    if staged:
        _validate_staged(staged)
    if directories() != before:
        raise ValueError("Lyon source directory changed")
    return {"generation": run_dir.name, "attempt": attempt_number, "started_at": attempt["started_at"],
            "finished_at": attempt["finished_at"], "producer_sha": current_code_sha,
            "database_target_sha256": target["value"], "source_sha256": state["sha256"],
            "main_sha256": state["main_sha256"], "records": records, "staged": staged, "held": held}


def candidate_data(source, generation_started_at):
    """Project a source replay for the existing gate, without asserting status."""
    generation, started, finished = source["generation"], source["started_at"], source["finished_at"]
    records, staged = source["records"], source["staged"]
    if len(records) != len(staged):
        raise ValueError("Lyon candidate partition differs")
    listings = [{"sourceKey": "lyon-stahl", "id": row["external_id"], "url": row["source_url"],
                 "lyonSearchRecord": record} for record, row in zip(records, staged, strict=True)]
    return {"schema": "lyon-search-partial/v1", "sourceKey": "lyon-stahl",
            "binding": {"generation": generation, "attempt": source["attempt"], "startedAt": started,
                        "finishedAt": finished, "sourceSha256": source["source_sha256"],
                        "sourceMainSha256": source["main_sha256"], "producerSha": source["producer_sha"],
                        "targetSha256": source["database_target_sha256"]},
            "coverage": "partial_unknown", "held": source["held"],
            "runMeta": {"mode": "partial", "transactions": ["sale", "lease"], "startedAt": started,
                        "finishedAt": finished, "freshness": {"generationId": generation,
                        "generationStartedAt": generation_started_at, "requireFreshDetails": False}},
            "sources": [{"sourceKey": "lyon-stahl", "transaction": "sale", "supported": True,
                         "truncated": True, "listingsCollected": len(listings), "error": "original source pass was partial"},
                        {"sourceKey": "lyon-stahl", "transaction": "lease", "supported": True,
                         "truncated": True, "listingsCollected": 0}],
            "listings": listings, "brokers": [], "totalListings": len(listings)}


def validate_candidate(data, source, generation_started_at):
    """Bind the gate projection to an independently replayed source receipt."""
    if data != candidate_data(source, generation_started_at):
        raise ValueError("Lyon candidate differs from source replay")
    return source["staged"]


def run_replay_worker(run_dir, manifest, owner, save):
    """Build one private candidate under the canonical checkpoint owner.

    Caller has settled the collector group. This does not grant admission.
    """
    import cre_checkpoint_refresh as refresh
    from cre_stop_guarded_tier import StopRefused, _safe_stat

    run_dir = Path(run_dir)
    if not run_dir.is_absolute() or not isinstance(owner, refresh.SharedLock) or not callable(save):
        raise ValueError("Lyon replay owner invalid")

    def check_owner():
        owner._require_authority()
        descriptor = owner._owned_directory_fd()
        os.close(descriptor)
        if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
            raise ValueError("Lyon replay lock differs")

    check_owner()
    config = manifest["config"]
    if (config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False or config.get("transactions") != ["sale", "lease"]
            or config.get("max_items") != 0 or "lyon-stahl" not in config.get("sources", [])):
        raise ValueError("Lyon replay scope invalid")
    checkpoint = manifest["sources"]["lyon-stahl"]
    if checkpoint.get("lyon_partial_worker") is not None:
        raise ValueError("Lyon replay already attempted")
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("Lyon source attempt missing")
    number = attempts[-1].get("number")
    producer, dirty = refresh.git_identity()
    if dirty is not False or producer != manifest.get("collector_git_sha"):
        raise ValueError("Lyon replay code changed")
    try:
        source = replay_settled_source(run_dir, manifest, number, producer, dirty)
    except (OSError, ValueError, TypeError, KeyError, StopRefused):
        # The settled source is held; loss of the owner itself still raises.
        check_owner()
        checkpoint["lyon_partial_worker"] = {"state": "source_held", "attempt": number}
        checkpoint["state"] = "partial_held_no_write"
        save(run_dir, manifest)
        return None
    checkpoint["lyon_partial_worker"] = {"state": "preparing", "attempt": number}
    save(run_dir, manifest)
    try:
        if not source["staged"]:
            checkpoint["lyon_partial_worker"]["state"] = "settled_no_write"
            checkpoint["state"] = "partial_held_no_write"
            save(run_dir, manifest)
            return None
        data = candidate_data(source, manifest["started_at"])
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + bytes([10])
        path = run_dir / "lyon-stahl-partial" / f"candidate-{number}.json"
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _safe_stat(directory, directory=True, private=True)
            descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.fsync(directory)
        finally:
            os.close(directory)
        check_owner()
        if refresh.git_identity() != (producer, False) or replay_settled_source(run_dir, manifest, number, producer, False) != source:
            raise ValueError("Lyon replay source or code changed")
        if _read_private_file(path, len(encoded)) != encoded:
            raise ValueError("Lyon replay candidate changed")
        identity = path.stat()
        checkpoint["candidate"] = {"path": str(path.relative_to(run_dir)), "attempt_started_at": source["started_at"],
            "attempt_number": number, "file_identity": [identity.st_dev, identity.st_ino, identity.st_size,
                                                          identity.st_mtime_ns, identity.st_ctime_ns],
            "bytes": identity.st_size}
        checkpoint["lyon_partial_worker"].update(state="settled", sha256=hashlib.sha256(encoded).hexdigest())
        checkpoint["state"] = "candidate"
        save(run_dir, manifest)
        return path
    except BaseException as exc:
        checkpoint.pop("candidate", None)
        checkpoint["lyon_partial_worker"]["state"] = "failed"
        try:
            save(run_dir, manifest)
        except BaseException as save_error:
            raise BaseExceptionGroup("Lyon replay and checkpoint save failed", [exc, save_error]) from exc
        raise


def candidate_structure(data):
    """Validate self-consistency only; this does not authenticate source bytes."""
    if not isinstance(data, dict) or not isinstance(data.get("binding"), dict):
        raise ValueError("Lyon candidate structure invalid")
    binding = data["binding"]
    if set(binding) != {"generation", "attempt", "startedAt", "finishedAt", "sourceSha256", "sourceMainSha256", "producerSha", "targetSha256"}:
        raise ValueError("Lyon candidate binding invalid")
    if (not isinstance(binding["generation"], str) or not binding["generation"]
            or type(binding["attempt"]) is not int or binding["attempt"] < 1
            or not isinstance(binding["producerSha"], str) or not re.fullmatch(r"[a-f0-9]{40}", binding["producerSha"])
            or any(not isinstance(binding[key], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[key])
                   for key in ("sourceSha256", "sourceMainSha256", "targetSha256"))
            or not isinstance(data.get("listings"), list)):
        raise ValueError("Lyon candidate binding invalid")
    records = []
    for listing in data["listings"]:
        if not isinstance(listing, dict) or set(listing) != {"sourceKey", "id", "url", "lyonSearchRecord"}:
            raise ValueError("Lyon candidate listing invalid")
        records.append(listing["lyonSearchRecord"])
    staged = [stage_record(record) for record in records]
    if not staged:
        raise ValueError("Lyon candidate empty")
    _validate_staged(staged)
    source = {"generation": binding["generation"], "attempt": binding["attempt"], "started_at": binding["startedAt"],
              "finished_at": binding["finishedAt"], "source_sha256": binding["sourceSha256"],
              "main_sha256": binding["sourceMainSha256"], "producer_sha": binding["producerSha"],
              "database_target_sha256": binding["targetSha256"], "records": records,
              "staged": staged, "held": data.get("held")}
    try:
        generation_started_at = data["runMeta"]["freshness"]["generationStartedAt"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Lyon candidate generation missing") from exc
    validate_candidate(data, source, generation_started_at)
    return source


def verify_candidate_artifact(artifact_path, manifest, *, current_code_sha, current_git_dirty):
    """Recheck candidate bytes against the original sealed source and owner."""
    path = Path(artifact_path)
    run_dir = path.parent.parent
    checkpoint = manifest["sources"]["lyon-stahl"]
    worker, info = checkpoint.get("lyon_partial_worker"), checkpoint.get("artifact")
    if (not isinstance(worker, dict) or worker.get("state") != "settled"
            or type(worker.get("attempt")) is not int or worker["attempt"] < 1
            or not isinstance(info, dict)):
        raise ValueError("Lyon candidate settlement invalid")
    attempt = worker["attempt"]
    relative = f"lyon-stahl-partial/candidate-{attempt}.json"
    if path != run_dir / relative or info.get("path") != relative or type(info.get("bytes")) is not int or info["bytes"] < 1:
        raise ValueError("Lyon candidate path differs")
    source = replay_settled_source(run_dir, manifest, attempt, current_code_sha, current_git_dirty)
    raw = _read_private_file(path, info["bytes"])
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != info["bytes"] or digest != info.get("sha256") or digest != worker.get("sha256"):
        raise ValueError("Lyon candidate bytes differ")
    data = json.loads(raw)
    staged = validate_candidate(data, source, manifest["started_at"])
    return {"run_dir": run_dir, "path": path, "checkpoint": checkpoint, "info": info, "source": source,
            "data": data, "staged": staged, "attempt": attempt}


def load_probe_preimage(run_dir, attempt, probe):
    relative = probe.get("preimage_path") if isinstance(probe, dict) else None
    if not isinstance(relative, str) or not re.fullmatch(rf"lyon-stahl-partial/preimage-{attempt}-[a-f0-9]{{16}}\.json", relative):
        raise ValueError("Lyon preimage path invalid")
    size = probe.get("preimage_bytes")
    if type(size) is not int or size < 1:
        raise ValueError("Lyon preimage size invalid")
    raw = _read_private_file(Path(run_dir) / relative, size)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != probe.get("preimage_sha256"):
        raise ValueError("Lyon preimage changed")
    preimage = json.loads(raw)
    if not isinstance(preimage, list):
        raise ValueError("Lyon preimage invalid")
    return preimage


def run_identity_probe(artifact_path, manifest, env_file):
    """Classify source identities against the bound target without writing."""
    import secrets

    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_lyon_partial import _validate_partition, identity_probe_sql
    from cre_stop_guarded_tier import _safe_stat
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    producer, dirty = git_identity()
    if dirty or producer != manifest.get("collector_git_sha"):
        raise ValueError("Lyon probe code changed")
    verified = verify_candidate_artifact(artifact_path, manifest, current_code_sha=producer, current_git_dirty=dirty)
    source, staged = verified["source"], verified["staged"]
    target = source["database_target_sha256"]
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("Lyon probe target changed")
    import subprocess

    def recheck():
        current_url, _ = load_db_url(env_file)
        after = verify_candidate_artifact(artifact_path, manifest, current_code_sha=producer, current_git_dirty=False)
        if (database_target_fingerprint_from_url(current_url).get("value") != target
                or git_identity() != (producer, False) or after["source"] != source or after["staged"] != staged):
            raise ValueError("Lyon probe input or target changed")

    def source_refusal(cause):
        # subprocess.run has killed and reaped its direct psql child on timeout.
        # A separate read-only health query must also succeed on the same bound
        # target before a settled source-specific refusal can spare peers.
        recheck()
        try:
            health = _run_psql_script(find_psql(), db_url,
                "BEGIN TRANSACTION READ ONLY;\nSELECT 1 AS healthy;\nROLLBACK;\n", timeout=10)
            healthy = health.returncode == 0 and parse_tsv(health.stdout) == [{"healthy": "1"}]
        except (OSError, RuntimeError, ValueError, TypeError, SystemExit, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Lyon probe target health is unknown") from exc
        recheck()
        if not healthy:
            raise RuntimeError("Lyon probe target health is unknown") from cause
        raise LyonSourceProbeRefusal("Lyon read-only identity query was refused") from cause

    # Verbose PostgreSQL errors expose SQLSTATE without copying provider data
    # into logs. Only data/query-local states are eligible for a source hold.
    sql = ("\\set VERBOSITY verbose\nBEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(staged) + ";\nROLLBACK;\n")
    try:
        result = _run_psql_script(find_psql(), db_url, sql, timeout=60)
    except subprocess.TimeoutExpired as exc:
        source_refusal(exc)
    if result.returncode:
        state = re.search(r"ERROR:\s+([0-9A-Z]{5}):", getattr(result, "stderr", "") or "")
        if (result.returncode == 3 and state
                and (state.group(1).startswith("22") or state.group(1) in {"42601", "57014"})):
            source_refusal(RuntimeError("Lyon source query was refused"))
        raise RuntimeError("Lyon identity probe target or query failed")
    recheck()
    parsed = parse_tsv(result.stdout)
    if (len(parsed) != 1 or set(parsed[0]) != {"staged", "joined", "held", "preimage"}
            or parsed[0]["staged"] != str(len(staged)) or parsed[0]["joined"] != str(len(staged))):
        raise RuntimeError("Lyon probe query scope differs")
    try:
        held, preimage = json.loads(parsed[0]["held"]), json.loads(parsed[0]["preimage"])
        _validate_partition(staged, held, preimage)
    except (ValueError, TypeError, KeyError) as exc:
        raise LyonSourceProbeRefusal("Lyon source identity partition was invalid") from exc
    encoded = json.dumps(preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    staged_bytes = json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    number = source["attempt"]
    name = f"preimage-{number}-{secrets.token_hex(8)}.json"
    directory = os.open(verified["run_dir"] / "lyon-stahl-partial", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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
    return {"artifact_sha256": verified["info"]["sha256"], "generation_id": source["generation"],
            "producer_sha": producer, "database_target_sha256": target,
            "staged_sha256": hashlib.sha256(staged_bytes).hexdigest(), "held": held,
            "preimage_path": f"lyon-stahl-partial/{name}", "preimage_bytes": len(encoded),
            "preimage_sha256": hashlib.sha256(encoded).hexdigest()}


def admit_search(artifact_path, manifest, report, *, current_code_sha, current_git_dirty, phase):
    """Admit the authenticated partial search through the shared additive gate."""
    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts
    from cre_lyon_partial import _validate_partition

    config = manifest["config"]
    if (phase not in ("source", "aggregate") or config.get("additive") is not True
            or config.get("mark_missing") is not False or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or "lyon-stahl" not in config.get("sources", [])):
        raise ValueError("Lyon partial gate scope invalid")
    verified = verify_candidate_artifact(artifact_path, manifest, current_code_sha=current_code_sha,
                                         current_git_dirty=current_git_dirty)
    source, staged, info = verified["source"], verified["staged"], verified["info"]
    checkpoint, path = verified["checkpoint"], verified["path"]
    probe = checkpoint.get("lyon_identity_probe")
    staged_bytes = json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    staged_sha = hashlib.sha256(staged_bytes).hexdigest()
    if (not isinstance(probe, dict) or probe.get("artifact_sha256") != info["sha256"]
            or probe.get("generation_id") != manifest["run_id"] or probe.get("producer_sha") != current_code_sha
            or probe.get("database_target_sha256") != source["database_target_sha256"]
            or probe.get("staged_sha256") != staged_sha):
        raise ValueError("Lyon identity probe differs")
    held = probe.get("held")
    preimage = load_probe_preimage(verified["run_dir"], verified["attempt"], probe)
    _validate_partition(staged, held, preimage)
    current, errors, sources, _, rejects = count_artifacts([str(path)], True)
    if (sources != {"lyon-stahl"} or errors.get("lyon-stahl") != "original source pass was partial"
            or rejects != 0 or current.get("lyon-stahl") != len(staged)):
        raise ValueError("Lyon gate evidence differs")
    validate_report_scope(report, manifest, "lyon-stahl", path)
    summary, gate = report["summary"], report["per_source"]["lyon-stahl"]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != len(staged) or gate.get("mark_missing_safe") is not False):
        raise ValueError("Lyon coverage gate differs")
    verdict = "ok_additive_verified_subset"
    reasons = {("first_seen", "no baseline row; cannot gate (first sight); a source pass also errored this run"),
               ("hold", "source pass error: original source pass was partial")}
    pair = ((gate.get("raw_verdict"), gate.get("raw_reason")) if gate.get("verdict") == verdict
            else (gate.get("verdict"), gate.get("reason")))
    if pair not in reasons:
        raise ValueError("Lyon coverage gate reason differs")
    binding = {"schema": "lyon-search-partial-v1", "scope": "lyon_partial_search_v1", "source_key": "lyon-stahl",
               "artifact_sha256": info["sha256"], "source_sha256": source["source_sha256"],
               "source_main_sha256": source["main_sha256"], "producer_sha": current_code_sha,
               "database_target_sha256": source["database_target_sha256"], "generation_id": manifest["run_id"],
               "generation_started_at": manifest["started_at"], "attempt": source["attempt"],
               "staged_sha256": staged_sha, "preimage_sha256": probe["preimage_sha256"], "held": held,
               "held_count": len(held) + len(source["held"]), "emitted_count": len(staged) - len(held),
               "original_source_outcome": "source_error", "whole_source_coverage": False}
    if phase == "aggregate" and checkpoint.get("gate", {}).get("current_inventory_proof") != binding:
        raise ValueError("Lyon source gate changed")
    gate.update(raw_verdict=pair[0], raw_reason=pair[1], verdict=verdict,
                reason="verified Lyon search observations admitted additively", mark_missing_safe=False,
                admission_scope="lyon_partial_search_v1", current_inventory_proof=binding)
    for key in ("hold_sources", "mark_missing_safe_sources", "mark_missing_safe_brokerages"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != "lyon-stahl"]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get("lyon-stahl"), dict):
        report["per_brokerage"]["lyon-stahl"]["mark_missing_safe"] = False
    if isinstance(summary.get("verdict_counts"), dict):
        counts = {}
        for item in report["per_source"].values():
            value = item.get("verdict")
            counts[value] = counts.get(value, 0) + 1
        summary["verdict_counts"] = counts
    report["scope"] = {"kind": ("lyon_partial_search_v1" if phase == "source" else "collector_registry_verified_subset"),
                       "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return deepcopy(binding)


def ingest_subset_context(artifact_path, envelope, *, current_code_sha, current_git_dirty,
                          expected_database_target_sha256, attempt_id):
    """Reprove source and both subset gates immediately before the SQL write."""
    import uuid

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("Lyon partial claims invalid")
    manifest = deepcopy(envelope["manifest"])
    if manifest["preflight"]["database_target"] != {"algorithm": "sha256", "value": expected_database_target_sha256}:
        raise ValueError("Lyon target changed")
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = deepcopy(envelope[name])
        if (report["per_source"]["lyon-stahl"].get("current_inventory_proof") != binding
                or admit_search(artifact_path, manifest, report, current_code_sha=current_code_sha,
                                current_git_dirty=current_git_dirty, phase=phase) != binding):
            raise ValueError("Lyon gate proof changed")
    verified = verify_candidate_artifact(artifact_path, manifest, current_code_sha=current_code_sha,
                                         current_git_dirty=current_git_dirty)
    probe = verified["checkpoint"]["lyon_identity_probe"]
    preimage = load_probe_preimage(verified["run_dir"], verified["attempt"], probe)
    return {"binding": binding, "staged": verified["staged"], "held": probe["held"], "preimage": preimage,
            "source": verified["source"], "job_id": str(uuid.UUID(attempt_id)), "attempt_id": attempt_id}


def run_partial_readback(artifact_path, envelope, *, code_sha, target, attempt_id, env_file):
    """Check post-write state against the same source-bound gate claims."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_lyon_partial import new_missing_canonical_count, partial_readback_sql
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    def context():
        producer, dirty = git_identity()
        if producer != code_sha or dirty is not False:
            raise ValueError("Lyon readback code changed")
        return ingest_subset_context(artifact_path, envelope, current_code_sha=producer, current_git_dirty=dirty,
            expected_database_target_sha256=target, attempt_id=attempt_id)

    before = context()
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("Lyon readback target changed")
    source = before["source"]
    sql = partial_readback_sql(before["staged"], before["held"], before["preimage"], before["job_id"],
                               source["started_at"], source["finished_at"])
    result = _run_psql_script(find_psql(), db_url, "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        + PIN_STRING_LITERALS + "\n" + sql + ";\nROLLBACK;\n", timeout=60)
    if result.returncode:
        raise RuntimeError("Lyon readback query failed")
    parsed = parse_tsv(result.stdout)
    current_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(current_url).get("value") != target or context() != before:
        raise ValueError("Lyon readback input or target changed")
    if parsed != [{"jobs": "1", "observed": str(len(before["staged"])), "mismatches": "0"}]:
        raise ValueError("Lyon readback postconditions differ")
    return {"ok": True, "outcome": "exact_commit", "artifact_sha256": before["binding"]["artifact_sha256"],
            "generation_id": source["generation"], "attempt_id": attempt_id, "database_target_sha256": target,
            "expected": before["binding"]["emitted_count"], "held": before["held"],
            "new_missing_canonical_count": new_missing_canonical_count(before["staged"], before["held"], before["preimage"]),
            "staged_sha256": before["binding"]["staged_sha256"],
            "checked": ["partial_job", "projected_scalars", "source_history", "parent_lifecycle", "source_index", "children", "held_parents"],
            "child_collections_verified": True}
