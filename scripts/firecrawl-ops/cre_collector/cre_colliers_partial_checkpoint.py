"""Checkpoint custody, gates, readback and worker for Colliers partial windows."""
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from cre_colliers_partial import (
    _time,
    _validate_staged,
    identity_probe_sql,
    partial_readback_sql,
    prepare_partial_rows,
    stage_detail_record,
    transaction_guard_sql,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")

def revalidate_source_receipt(run_dir, manifest, attempt_number, current_code_sha, current_git_dirty):
    """Revalidate source bytes; caller must authenticate manifest and canonical lock.

    Recorded rc/finish are not independent evidence of current owner settlement.
    This helper does not grant admission.
    """
    import os
    import stat
    from copy import deepcopy
    from pathlib import Path

    from cre_partial_page_custody import expected_path, settle

    run_dir = Path(run_dir)
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_dir.name
            or not isinstance(current_code_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)
            or current_git_dirty is not False or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or type(attempt_number) is not int or attempt_number < 1):
        raise ValueError("partial source owner changed")
    target = (manifest.get("preflight") or {}).get("database_target")
    if (not isinstance(target, dict) or target.get("algorithm") != "sha256"
            or not isinstance(target.get("value"), str) or not _HASH.fullmatch(target["value"])):
        raise ValueError("partial source target invalid")
    def directory_identities():
        identities = []
        for directory in (run_dir, run_dir / "rejected", run_dir / "colliers-partial"):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("partial source directory invalid")
            identities.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        return identities

    directories = directory_identities()
    checkpoint = (manifest.get("sources") or {}).get("colliers") or {}
    attempts = [attempt for attempt in checkpoint.get("attempts", [])
                if isinstance(attempt, dict) and attempt.get("number") == attempt_number]
    numbers = [a.get("number") for a in checkpoint.get("attempts", []) if isinstance(a, dict)]
    if (len(attempts) != 1 or not numbers or any(type(n) is not int for n in numbers)
            or attempt_number != max(numbers)):
        raise ValueError("partial source attempt differs")
    attempt = attempts[0]
    if (type(attempt.get("rc")) is not int or not attempt.get("finished_at")
            or _time(attempt.get("started_at")) < _time(manifest.get("started_at"))
            or _time(attempt["finished_at"]) < _time(attempt.get("started_at"))
            or _time(attempt["finished_at"]) > datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("partial source attempt is unsettled")
    state = attempt.get("colliers_partial")
    expected_rel = str(expected_path(run_dir, attempt_number, "colliers").relative_to(run_dir))
    main_rel = f"rejected/colliers-attempt-{attempt_number}.json"
    if (not isinstance(state, dict) or state.get("state") != "sealed_local"
            or state.get("path") != expected_rel or state.get("outcome") != "source_error"
            or state.get("coverage") != "unverified" or attempt.get("rejected_artifact") != main_rel
            or state.get("main_sha256") != attempt.get("rejected_artifact_sha256")):
        raise ValueError("partial source receipt differs")
    probe = deepcopy(attempt)
    probe["colliers_partial"] = {"path": expected_rel, "state": "pending"}
    settle(run_dir, probe, run_dir / main_rel, "colliers")
    keys = ("state", "path", "coverage", "outcome", "bytes", "observationRecords", "sha256", "main_sha256")
    if directory_identities() != directories:
        raise ValueError("partial source directory changed")
    if any(probe["colliers_partial"].get(key) != state.get(key) for key in keys):
        raise ValueError("partial source receipt changed")

    # Source page stamps and the rejected artifact use the same Node-process
    # monotonic-anchored clock.  Preserve the coordinator finish separately:
    # after a wall-clock step it can be slightly earlier than the child's
    # genuine final observation even though the process already settled.
    from source_evidence.automatic import _read_private

    main = run_dir / main_rel
    main_bytes = _read_private(main, main.lstat().st_size)
    if hashlib.sha256(main_bytes).hexdigest() != state["main_sha256"]:
        raise ValueError("partial source main artifact changed")
    main_data = json.loads(main_bytes)
    meta = main_data.get("runMeta") if isinstance(main_data, dict) else None
    source_finish = attempt["finished_at"]
    if isinstance(meta, dict) and meta.get("finishedAt") is not None:
        child_finish = _time(meta["finishedAt"])
        parent_finish = _time(attempt["finished_at"])
        freshness = meta.get("freshness")
        child_start = _time(meta.get("startedAt"))
        if (not isinstance(freshness, dict)
                or freshness.get("generationId") != run_dir.name
                or _time(freshness.get("generationStartedAt")) != _time(manifest["started_at"])
                or child_start < _time(attempt["started_at"]) or child_finish < child_start
                or child_finish - parent_finish > timedelta(minutes=5)
                or child_finish > datetime.now(UTC) + timedelta(minutes=5)):
            raise ValueError("partial source clock discrepancy exceeds bound")
        source_finish = meta["finishedAt"]
    return {key: state[key] for key in keys} | {
        "generation": run_dir.name, "generation_started_at": manifest["started_at"], "attempt": attempt_number,
        "started_at": attempt["started_at"], "finished_at": source_finish,
        "coordinator_finished_at": attempt["finished_at"],
        "producer_sha": current_code_sha, "database_target_sha256": target["value"],
    }


def unpack_window_records(data):
    """Separate the worker's private byte binding from canonical listing data."""
    records = []
    for listing in data["listings"]:
        if not isinstance(listing, dict) or not isinstance(listing.get("colliersPartialRecord"), dict):
            raise TypeError("partial worker record invalid")
        payload = listing["colliersPartialRecord"]
        if set(payload) != {"source", "decodedDetail", "nativeBase64"}:
            raise TypeError("partial worker record invalid")
        records.append({"listing": {key: value for key, value in listing.items() if key != "colliersPartialRecord"}, **payload})
    return records


def validate_window_artifact(data, source_receipt, evidence_receipt):
    """Bind a trusted settled worker result to its exact source and target.

    Caller must authenticate the manifest/lock, child settlement and artifact
    bytes; this function does not independently replay either sidecar.
    """
    keys = {"schema", "sourceKey", "binding", "detailFinishedAt", "producerSha", "targetSha256",
            "source", "sourceOutcome", "coverage", "cursor", "proposedCursor", "selected", "total",
            "heldOther", "unattempted", "evidence", "listings", "held", "runMeta", "sources", "brokers", "totalListings"}
    if not isinstance(data, dict) or set(data) != keys or not isinstance(source_receipt, dict):
        raise ValueError("partial worker artifact shape invalid")
    binding = {"generation": source_receipt.get("generation"), "attempt": source_receipt.get("attempt"),
               "startedAt": source_receipt.get("started_at"), "finishedAt": source_receipt.get("finished_at")}
    expected_source = {"bytes": source_receipt.get("bytes"), "sha256": source_receipt.get("sha256"),
                       "mainSha256": source_receipt.get("main_sha256")}
    run_meta = {"mode": "partial", "transactions": ["sale", "lease"], "startedAt": binding["startedAt"],
                "finishedAt": data["detailFinishedAt"], "freshness": {"generationId": binding["generation"],
                "generationStartedAt": source_receipt.get("generation_started_at"), "requireFreshDetails": True}}
    if not isinstance(data["listings"], list):
        raise TypeError("partial worker listings invalid")
    source_entries = [
        {"sourceKey": "colliers", "transaction": "sale", "supported": True, "truncated": True,
         "listingsCollected": len(data["listings"]), "error": "original source pass failed"},
        {"sourceKey": "colliers", "transaction": "lease", "supported": True, "truncated": False, "listingsCollected": 0},
    ]
    if (data["runMeta"] != run_meta or data["sources"] != source_entries or data["brokers"] != []
            or type(data["totalListings"]) is not int or data["totalListings"] != len(data["listings"])):
        raise ValueError("partial worker gate scope differs")
    if (data["schema"] != "colliers-partial-window/v1" or data["sourceKey"] != "colliers"
            or data["sourceOutcome"] != "source_error" or data["coverage"] != "unverified"
            or data["binding"] != binding or data["source"] != expected_source
            or data["producerSha"] != source_receipt.get("producer_sha")
            or data["targetSha256"] != source_receipt.get("database_target_sha256")
            or data["evidence"] != evidence_receipt):
        raise ValueError("partial worker owner binding differs")
    if (not isinstance(evidence_receipt, dict) or set(evidence_receipt) != {"bytes", "records", "sha256"}
            or type(evidence_receipt["bytes"]) is not int or evidence_receipt["bytes"] < 1
            or type(evidence_receipt["records"]) is not int or evidence_receipt["records"] < 1
            or not isinstance(evidence_receipt["sha256"], str) or not _HASH.fullmatch(evidence_receipt["sha256"])):
        raise ValueError("partial worker evidence receipt invalid")
    numeric = ("cursor", "proposedCursor", "selected", "total", "heldOther", "unattempted", "held")
    if (any(type(data[key]) is not int or data[key] < (1 if key in {"cursor", "proposedCursor", "total"} else 0)
            for key in numeric)
            or data["selected"] > 2048
            or (data["proposedCursor"] > data["total"] and data["proposedCursor"] != data["cursor"])
            or not isinstance(data["listings"], list)
            or len(data["listings"]) + data["held"] != evidence_receipt["records"]
            or evidence_receipt["records"] + data["unattempted"] != data["selected"]):
        raise ValueError("partial worker counts differ")
    if not (_time(binding["startedAt"]) <= _time(data["detailFinishedAt"]) <= datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("partial worker finish invalid")
    records = unpack_window_records(data)
    # Source replay can emit physical pages out of cursor order. Preserve the
    # producer's rotation order for bounded DB history selection.
    records.sort(key=lambda item: (
        item["source"]["start"] + item["source"]["offset"] < data["cursor"],
        item["source"]["start"] + item["source"]["offset"],
    ))
    staged = [stage_detail_record(item, binding["generation"], binding["attempt"], binding["startedAt"],
                                   binding["finishedAt"], data["detailFinishedAt"]) for item in records]
    if staged:
        _validate_staged(staged)
    return staged


def cursor_after_database_holds(data, history_holds):
    """Advance only a well-formed window; held identities remain explicit."""
    if not isinstance(history_holds, dict) or not isinstance(data, dict):
        raise TypeError("partial cursor partition invalid")
    cursor = data["cursor"]
    records = data["listings"]
    if type(cursor) is not int or cursor < 1 or not isinstance(records, list):
        raise ValueError("partial cursor partition invalid")
    ids = [item.get("id") for item in records if isinstance(item, dict)]
    if len(ids) != len(records) or len(set(ids)) != len(ids) or not set(history_holds) <= set(ids):
        raise ValueError("partial cursor identities differ")
    proposed = data["proposedCursor"]
    if type(proposed) is not int or proposed < 1:
        raise ValueError("partial proposed cursor invalid")
    return proposed


def _read_private_file(path, maximum):
    """Read one owner-only regular file through its held private directory."""
    import os
    from pathlib import Path

    from cre_stop_guarded_tier import _read_at, _safe_stat

    path = Path(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(directory, directory=True, private=True)
        raw, _ = _read_at(directory, path.name, limit=maximum)
        return raw
    finally:
        os.close(directory)


def _window_base(attempt, window_index=1):
    """Name independent windows without changing the original first-window path."""
    if type(attempt) is not int or attempt < 1 or type(window_index) is not int or window_index < 1:
        raise ValueError("partial window identity invalid")
    suffix = "" if window_index == 1 else f"-{window_index}"
    return f"colliers-partial/window-{attempt}{suffix}"

def verify_worker_artifact(artifact_path, manifest, *, current_code_sha, current_git_dirty, allow_empty=False):
    """Recheck the settled worker and both private artifacts, without DB access."""
    from pathlib import Path

    source = "colliers"
    checkpoint = manifest["sources"][source]
    worker = checkpoint.get("colliers_partial_worker")
    if (not isinstance(worker, dict) or worker.get("state") != "settled" or worker.get("rc") != 0
            or type(worker.get("attempt")) is not int or worker["attempt"] < 1):
        raise ValueError("partial worker settlement invalid")
    path = Path(artifact_path)
    run_dir = path.parent.parent
    attempt = worker["attempt"]
    window_index = worker.get("window_index", 1)
    base = _window_base(attempt, window_index)
    if (path != run_dir / f"{base}-artifact.json"
            or checkpoint["artifact"].get("path") != f"{base}-artifact.json"
            or worker.get("receipt_path") != f"{base}-receipt.json"
            or worker.get("evidence_path") != f"{base}-evidence.jsonl"):
        raise ValueError("partial worker paths differ")
    source_receipt = revalidate_source_receipt(run_dir, manifest, attempt, current_code_sha, current_git_dirty)
    receipt_raw = _read_private_file(run_dir / worker["receipt_path"], 16384)
    if hashlib.sha256(receipt_raw).hexdigest() != worker.get("receipt_sha256"):
        raise ValueError("partial worker receipt changed")
    receipt = json.loads(receipt_raw)
    receipt_keys = {"schema", "generation", "attempt", "producerSha", "targetSha256", "artifact", "evidence",
                    "detailFinishedAt", "proposedCursor", "admitted", "held", "heldOther", "unattempted"}
    if window_index != 1:
        receipt_keys.add("windowIndex")
    if (not isinstance(receipt, dict) or set(receipt) != receipt_keys
            or receipt["schema"] != "colliers-window-worker-receipt/v1"
            or (window_index != 1 and receipt.get("windowIndex") != window_index)
            or receipt["generation"] != manifest["run_id"] or receipt["attempt"] != attempt
            or receipt["producerSha"] != current_code_sha
            or receipt["targetSha256"] != source_receipt["database_target_sha256"]):
        raise ValueError("partial worker receipt differs")
    artifact_raw = _read_private_file(path, 128 * 1024 * 1024)
    evidence_raw = _read_private_file(run_dir / worker["evidence_path"], 128 * 1024 * 1024)
    info = checkpoint["artifact"]
    if (receipt["artifact"] != {"bytes": len(artifact_raw), "sha256": hashlib.sha256(artifact_raw).hexdigest()}
            or info.get("sha256") != receipt["artifact"]["sha256"] or info.get("bytes") != len(artifact_raw)
            or not isinstance(receipt["evidence"], dict)
            or receipt["evidence"].get("bytes") != len(evidence_raw)
            or receipt["evidence"].get("sha256") != hashlib.sha256(evidence_raw).hexdigest()):
        raise ValueError("partial worker bytes changed")
    data = json.loads(artifact_raw)
    staged = validate_window_artifact(data, source_receipt, receipt["evidence"])
    if ((not staged and not allow_empty) or receipt["admitted"] != len(staged) or receipt["held"] != data["held"]
            or receipt["heldOther"] != data["heldOther"] or receipt["unattempted"] != data["unattempted"]
            or receipt["proposedCursor"] != data["proposedCursor"] or receipt["detailFinishedAt"] != data["detailFinishedAt"]):
        raise ValueError("partial worker result differs")
    return {"checkpoint": checkpoint, "worker": worker, "path": path, "run_dir": run_dir,
            "attempt": attempt, "source_receipt": source_receipt, "receipt": receipt,
            "info": info, "data": data, "staged": staged}


def load_probe_preimage(run_dir, attempt, probe):
    from pathlib import Path

    relative = probe.get("preimage_path") if isinstance(probe, dict) else None
    pattern = rf"colliers-partial/window-{attempt}-preimage-[a-f0-9]{{16}}\.json"
    if not isinstance(relative, str) or not re.fullmatch(pattern, relative):
        raise ValueError("partial preimage path invalid")
    raw = _read_private_file(Path(run_dir) / relative, 32 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != probe.get("preimage_sha256"):
        raise ValueError("partial preimage changed")
    value = json.loads(raw)
    if not isinstance(value, list):
        raise TypeError("partial preimage invalid")
    return value


def admit_window(artifact_path, manifest, report, *, current_code_sha, current_git_dirty, phase):
    """Qualify one source-specific window through the shared subset gate."""
    import copy

    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts

    source = "colliers"
    config = manifest["config"]
    if (phase not in ("source", "aggregate") or config.get("additive") is not True
            or config.get("mark_missing") is not False or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or source not in config.get("sources", []) or current_git_dirty is not False
            or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or not isinstance(current_code_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)):
        raise ValueError("partial owner or configuration invalid")
    verified = verify_worker_artifact(artifact_path, manifest, current_code_sha=current_code_sha,
                                      current_git_dirty=current_git_dirty)
    checkpoint, path = verified["checkpoint"], verified["path"]
    attempt, source_receipt, receipt = verified["attempt"], verified["source_receipt"], verified["receipt"]
    info, data, staged = verified["info"], verified["data"], verified["staged"]
    probe = checkpoint.get("colliers_identity_probe")
    staged_sha = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if (not isinstance(probe, dict) or probe.get("artifact_sha256") != info["sha256"]
            or probe.get("generation_id") != manifest["run_id"] or probe.get("producer_sha") != current_code_sha
            or probe.get("database_target_sha256") != source_receipt["database_target_sha256"]
            or probe.get("staged_sha256") != staged_sha):
        raise ValueError("partial identity probe differs")
    held, preimage = probe.get("held"), load_probe_preimage(verified["run_dir"], attempt, probe)
    records = unpack_window_records(data)
    _, history_holds = prepare_partial_rows(records, staged, held, preimage, manifest["run_id"], attempt,
                                            source_receipt["started_at"], source_receipt["finished_at"], data["detailFinishedAt"])
    transaction_guard_sql(staged, held, preimage, history_holds)
    if probe.get("history_holds") != history_holds:
        raise ValueError("partial history partition differs")
    current, errors, sources, _, rejects = count_artifacts([str(path)], True)
    if sources != {source} or errors.get(source) != "original source pass failed" or rejects != 0 or current.get(source) != len(staged):
        raise ValueError("partial gate evidence differs")
    validate_report_scope(report, manifest, source, path)
    summary, gate = report["summary"], report["per_source"][source]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != len(staged) or gate.get("mark_missing_safe") is not False):
        raise ValueError("partial gate scope invalid")
    verdict = "ok_additive_verified_subset"
    reasons = {("first_seen", "no baseline row; cannot gate (first sight); a source pass also errored this run"),
               ("hold", "source pass error: original source pass failed")}
    raw_pair = ((gate.get("raw_verdict"), gate.get("raw_reason")) if gate.get("verdict") == verdict
                else (gate.get("verdict"), gate.get("reason")))
    if raw_pair not in reasons:
        raise ValueError("partial gate reason differs")
    digest = probe["preimage_sha256"]
    binding = {"schema": "colliers-partial-window-v1", "scope": "colliers_partial_window_v1", "source_key": source,
               "artifact_sha256": info["sha256"], "evidence_sha256": receipt["evidence"]["sha256"],
               "source_sha256": source_receipt["sha256"], "source_main_sha256": source_receipt["main_sha256"],
               "producer_sha": current_code_sha, "database_target_sha256": source_receipt["database_target_sha256"],
               "generation_id": manifest["run_id"], "generation_started_at": manifest["started_at"],
               "attempt": attempt, "staged_sha256": staged_sha, "preimage_sha256": digest,
               "held": held, "history_holds": history_holds, "held_count": len(held) + len(history_holds),
               "emitted_count": len(staged) - len(held) - len(history_holds),
               "cursor": data["cursor"], "next_cursor": cursor_after_database_holds(data, history_holds),
               "original_source_outcome": "source_error", "whole_source_coverage": False}
    if phase == "aggregate" and checkpoint.get("gate", {}).get("current_inventory_proof") != binding:
        raise ValueError("partial source gate changed")
    gate.update(raw_verdict=raw_pair[0], raw_reason=raw_pair[1], verdict=verdict,
                reason=("verified Colliers partial rows admitted additively" if binding["emitted_count"]
                        else "verified Colliers partial partition held without listing writes"),
                mark_missing_safe=False, admission_scope="colliers_partial_window_v1", current_inventory_proof=binding)
    for key in ("hold_sources", "mark_missing_safe_sources", "mark_missing_safe_brokerages"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != source]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get(source), dict):
        report["per_brokerage"][source]["mark_missing_safe"] = False
    if isinstance(summary.get("verdict_counts"), dict):
        counts = {}
        for source_gate in report["per_source"].values():
            value = source_gate.get("verdict")
            counts[value] = counts.get(value, 0) + 1
        summary["verdict_counts"] = counts
    report["scope"] = {"kind": ("colliers_partial_window_v1" if phase == "source" else "collector_registry_verified_subset"),
                       "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return copy.deepcopy(binding)


def run_identity_probe(artifact_path, manifest, env_file):
    """Read-only source/target-bound identity and bounded-history partition."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    producer, dirty = git_identity()
    if dirty or producer != manifest.get("collector_git_sha"):
        raise ValueError("partial probe code differs")
    verified = verify_worker_artifact(artifact_path, manifest, current_code_sha=producer, current_git_dirty=dirty)
    source, receipt, data, staged = verified["source_receipt"], verified["receipt"], verified["data"], verified["staged"]
    target = source["database_target_sha256"]
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("partial probe target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(staged) + ";\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=60)
    if result.returncode:
        raise RuntimeError("partial identity probe failed")
    parsed = parse_tsv(result.stdout)
    if (len(parsed) != 1 or set(parsed[0]) != {"staged", "joined", "held", "preimage"}
            or parsed[0]["staged"] != str(len(staged)) or parsed[0]["joined"] != str(len(staged))):
        raise ValueError("partial probe result differs")
    held, preimage = json.loads(parsed[0]["held"]), json.loads(parsed[0]["preimage"])
    ids = {row["external_id"] for row in staged}
    if (not isinstance(held, list) or held != sorted(set(held)) or not set(held) <= ids
            or not isinstance(preimage, list) or len(preimage) != len(staged)
            or {row.get("external_id") for row in preimage if isinstance(row, dict)} != ids):
        raise ValueError("partial probe partition invalid")
    _, history_holds = prepare_partial_rows(unpack_window_records(data), staged, held, preimage,
        manifest["run_id"], source["attempt"], source["started_at"], source["finished_at"], data["detailFinishedAt"])
    current_url, _ = load_db_url(env_file)
    after = verify_worker_artifact(artifact_path, manifest, current_code_sha=producer, current_git_dirty=False)
    if (database_target_fingerprint_from_url(current_url).get("value") != target
            or git_identity() != (producer, False) or after["source_receipt"] != source
            or after["receipt"] != receipt or after["staged"] != staged):
        raise ValueError("partial probe input or target changed")
    import os
    import secrets

    staged_sha = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    encoded = json.dumps(preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > 32 * 1024 * 1024:
        raise ValueError("partial preimage exceeds bound")
    name = f"window-{source['attempt']}-preimage-{secrets.token_hex(8)}.json"
    directory = os.open(verified["run_dir"] / "colliers-partial", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        from cre_stop_guarded_tier import _safe_stat
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
    return {"artifact_sha256": verified["info"]["sha256"], "generation_id": manifest["run_id"],
            "producer_sha": producer, "database_target_sha256": target, "staged_sha256": staged_sha,
            "held": held, "preimage_path": f"colliers-partial/{name}",
            "preimage_sha256": hashlib.sha256(encoded).hexdigest(), "history_holds": history_holds}


def ingest_subset_context(artifact_path, envelope, *, current_code_sha, current_git_dirty,
                          expected_database_target_sha256, attempt_id):
    """Reprove both shared gates and the exact target before partial ingest."""
    import copy
    import uuid

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("partial claims envelope invalid")
    manifest = copy.deepcopy(envelope["manifest"])
    if manifest["preflight"]["database_target"] != {"algorithm": "sha256", "value": expected_database_target_sha256}:
        raise ValueError("partial target changed")
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = copy.deepcopy(envelope[name])
        if (report["per_source"]["colliers"].get("current_inventory_proof") != binding
                or admit_window(artifact_path, manifest, report, current_code_sha=current_code_sha,
                                current_git_dirty=current_git_dirty, phase=phase) != binding):
            raise ValueError("partial gate claim changed")
    verified = verify_worker_artifact(artifact_path, manifest, current_code_sha=current_code_sha,
                                      current_git_dirty=current_git_dirty)
    checkpoint = verified["checkpoint"]
    probe = checkpoint["colliers_identity_probe"]
    preimage = load_probe_preimage(verified["run_dir"], verified["attempt"], probe)
    return {"binding": binding, "staged": verified["staged"], "held": probe["held"],
            "preimage": preimage, "history_holds": probe["history_holds"],
            "records": unpack_window_records(verified["data"]), "source_receipt": verified["source_receipt"],
            "artifact_finished_at": verified["data"]["detailFinishedAt"],
            "job_id": str(uuid.UUID(attempt_id)), "attempt_id": attempt_id}


def run_partial_readback(artifact_path, envelope, *, code_sha, target, attempt_id, env_file):
    """Bind a read-only postcondition query to the same sealed ingest claims."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    def context():
        producer, dirty = git_identity()
        if producer != code_sha or dirty is not False:
            raise ValueError("partial readback code changed")
        return ingest_subset_context(artifact_path, envelope, current_code_sha=producer,
            current_git_dirty=dirty, expected_database_target_sha256=target, attempt_id=attempt_id)

    before = context()
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("partial readback target changed")
    source = before["source_receipt"]
    sql, history_holds = partial_readback_sql(before["records"], before["staged"], before["held"],
        before["preimage"], source["generation"], source["attempt"], source["started_at"],
        source["finished_at"], before["artifact_finished_at"], before["job_id"])
    if history_holds != before["history_holds"]:
        raise ValueError("partial readback held partition changed")
    result = _run_psql_script(find_psql(), db_url,
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + PIN_STRING_LITERALS
        + "\n" + sql + ";\nROLLBACK;\n", timeout=60)
    if result.returncode:
        raise RuntimeError("partial readback query failed")
    parsed = parse_tsv(result.stdout)
    expected = before["binding"]["emitted_count"]
    current_url, _ = load_db_url(env_file)
    if (database_target_fingerprint_from_url(current_url).get("value") != target or context() != before):
        raise ValueError("partial readback input or target changed")
    if parsed != [{"jobs": "1", "observed": str(expected), "mismatches": "0"}]:
        raise ValueError("partial readback postconditions differ")
    return {"ok": True, "outcome": "exact_commit", "artifact_sha256": before["binding"]["artifact_sha256"],
            "generation_id": source["generation"], "attempt_id": attempt_id,
            "database_target_sha256": target, "expected": expected, "held": before["held"],
            "history_holds": history_holds, "staged_sha256": before["binding"]["staged_sha256"],
            "checked": ["partial_job", "projected_scalars", "native_history", "parent_lifecycle", "source_index", "held_parents"],
            "child_collections_verified": False}


def run_window_worker(run_dir, manifest, owner, runner, save, *, window_index=1):
    """Run one source-specific child under the existing canonical checkpoint owner."""
    import os
    import secrets
    from pathlib import Path

    import cre_checkpoint_refresh as refresh
    from cre_partial_cursor import advance_cursor, read_cursor

    run_dir = Path(run_dir)
    if not run_dir.is_absolute():
        raise ValueError("partial worker run directory must be absolute")
    if not isinstance(owner, refresh.SharedLock) or not callable(runner) or not callable(save):
        raise TypeError("partial worker owner invalid")

    def check_owner():
        owner._require_authority()
        descriptor = owner._owned_directory_fd()
        os.close(descriptor)
        if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
            raise ValueError("partial worker lock differs")

    check_owner()
    config = manifest["config"]
    if (config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False or config.get("transactions") != ["sale", "lease"]
            or config.get("max_items") != 0 or "colliers" not in config.get("sources", [])):
        raise ValueError("partial worker scope invalid")
    checkpoint = manifest["sources"]["colliers"]
    if checkpoint.get("colliers_partial_worker") is not None:
        raise ValueError("partial worker already attempted; do not replay")
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("partial source attempt missing")
    attempt = attempts[-1].get("number")
    producer, dirty = refresh.git_identity()
    source = revalidate_source_receipt(run_dir, manifest, attempt, producer, dirty)
    target = source["database_target_sha256"]
    cursor, _ = read_cursor(owner.path, "colliers", target)
    base = _window_base(attempt, window_index)
    request_path, receipt_path = run_dir / f"{base}-request.json", run_dir / f"{base}-receipt.json"
    evidence_path, artifact_path = run_dir / f"{base}-evidence.jsonl", run_dir / f"{base}-artifact.json"
    for path in (request_path, receipt_path, evidence_path, artifact_path):
        if path.exists() or path.is_symlink():
            raise ValueError("partial worker output already exists")
    request = {**({"windowIndex": window_index} if window_index != 1 else {}),
               "generation": source["generation"], "generationStartedAt": source["generation_started_at"],
               "attempt": attempt, "startedAt": source["started_at"], "finishedAt": source["finished_at"],
               "runDir": str(run_dir), "sourceBytes": source["bytes"], "sourceSha256": source["sha256"],
               "originalSourceMainSha256": source["main_sha256"], "producerSha": producer,
               "targetSha256": target, "cursor": cursor, "ownerPid": os.getpid(),
               "lockLease": owner.lease_token, "authorityToken": owner.authority_token}
    checkpoint["colliers_partial_worker"] = {"state": "dispatching", "attempt": attempt, "rc": None,
        **({"window_index": window_index} if window_index != 1 else {}),
        "receipt_path": str(receipt_path.relative_to(run_dir)), "evidence_path": str(evidence_path.relative_to(run_dir)),
        "artifact_path": str(artifact_path.relative_to(run_dir)), "cursor": cursor}
    save(run_dir, manifest)
    encoded = (json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(request_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        check_owner()
        env = refresh.safe_process_env()
        env["COLLIERS_PARTIAL_WORKER_REQUEST"] = str(request_path)
        rc = runner(["node", "--import", "tsx", "sources/colliers-partial-window-entry.ts"],
                    run_dir / "logs" / "colliers-partial-worker.log", env=env)
        check_owner()
        if rc != 0:
            checkpoint["colliers_partial_worker"].update(state="failed", rc=rc)
            save(run_dir, manifest)
            return None
        raw = _read_private_file(receipt_path, 16384)
        receipt = json.loads(raw)
        worker = checkpoint["colliers_partial_worker"]
        worker.update(state="settled", rc=0, receipt_sha256=hashlib.sha256(raw).hexdigest())
        info = receipt.get("artifact") if isinstance(receipt, dict) else None
        if not isinstance(info, dict):
            raise TypeError("partial worker artifact receipt missing")
        checkpoint["artifact"] = {"path": str(artifact_path.relative_to(run_dir)),
                                  "sha256": info.get("sha256"), "bytes": info.get("bytes")}
        try:
            verified = verify_worker_artifact(artifact_path, manifest, current_code_sha=producer,
                                              current_git_dirty=False, allow_empty=True)
        except BaseException:
            checkpoint.pop("artifact", None)
            raise
        if refresh.git_identity() != (producer, False) or revalidate_source_receipt(run_dir, manifest, attempt, producer, False) != source:
            checkpoint.pop("artifact", None)
            raise ValueError("partial worker source or code changed")
        check_owner()
        if not verified["staged"]:
            # A fully classified, settled no-write window can advance only to
            # the worker's verified prefix; source-wide stops leave it unchanged.
            proposed = receipt["proposedCursor"]
            checkpoint.pop("artifact", None)
            if proposed != cursor:
                attempt_id = secrets.token_hex(16)
                proof = {"ok": True, "artifact_sha256": info["sha256"], "generation_id": source["generation"],
                         "attempt_id": attempt_id, "database_target_sha256": target, "outcome": "settled_no_write"}
                worker.update(state="cursor_publishing", cursor_attempt_id=attempt_id, proposed_cursor=proposed)
                save(run_dir, manifest)
                advance_cursor(owner.path, "colliers", target, cursor, proposed, generation=source["generation"],
                               artifact_sha256=info["sha256"], attempt_id=attempt_id, outcome="settled_no_write",
                               proof=proof, owner_check=check_owner)
            worker["state"] = "settled_no_write"
            checkpoint["state"] = "partial_held_no_write"
            save(run_dir, manifest)
            return None
        identity = artifact_path.stat()
        checkpoint["candidate"] = {"path": str(artifact_path.relative_to(run_dir)), "attempt_started_at": source["started_at"],
            "attempt_number": attempt, "file_identity": [identity.st_dev, identity.st_ino, identity.st_size,
                                                           identity.st_mtime_ns, identity.st_ctime_ns],
            "bytes": identity.st_size}
        checkpoint.pop("artifact", None)
        checkpoint["state"] = "candidate"
        save(run_dir, manifest)
        return artifact_path
    except BaseException as exc:
        checkpoint.pop("artifact", None)
        worker = checkpoint["colliers_partial_worker"]
        worker["state"] = ("cursor_recovery_required" if worker["state"] == "cursor_publishing" else
                           "settlement_unknown" if refresh._cleanup_unknown_in_chain(exc) else "failed")
        try:
            save(run_dir, manifest)
        except BaseException as save_error:  # noqa: BLE001 - preserve an interrupt and the original failure
            raise BaseExceptionGroup("partial worker failure and checkpoint save failure", [exc, save_error]) from exc
        raise
