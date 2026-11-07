"""Derive Matthews partial candidates from one settled private attempt.

Candidate derivation alone grants no write authority. The checkpoint must bind
its target, exact bytes, source and aggregate admission before invoking a writer.
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from cre_ingest import parse_strict_freshness_timestamp

from matthews_partial.fill import stage_observations


def derive(raw, *, generation_id, generation_started_at, attempt_started_at, attempt_finished_at):
    if not isinstance(raw, bytes) or not isinstance(generation_id, str) or not generation_id:
        raise ValueError("invalid Matthews source bytes or generation")
    source = json.loads(raw)
    meta = source.get("runMeta") if isinstance(source, dict) else None
    fresh = meta.get("freshness") if isinstance(meta, dict) else None
    if (not isinstance(meta, dict) or meta.get("mode") != "full" or meta.get("transactions") != ["sale", "lease"]
            or meta.get("maxItemsPerSource") is not None or meta.get("terminalCaptureFailure")
            or not isinstance(fresh, dict) or fresh.get("requireFreshDetails") is not True
            or fresh.get("generationId") != generation_id):
        raise ValueError("Matthews source scope differs")
    g = parse_strict_freshness_timestamp(generation_started_at, field="generation_started_at")
    a = parse_strict_freshness_timestamp(attempt_started_at, field="attempt_started_at")
    f = parse_strict_freshness_timestamp(attempt_finished_at, field="attempt_finished_at")
    started = parse_strict_freshness_timestamp(meta.get("startedAt"), field="startedAt")
    finished = parse_strict_freshness_timestamp(meta.get("finishedAt"), field="finishedAt")
    if (parse_strict_freshness_timestamp(fresh.get("generationStartedAt"), field="generationStartedAt") != g
            or not g <= a <= started <= finished <= f):
        raise ValueError("Matthews source attempt clocks differ")
    entries, listings = source.get("sources"), source.get("listings")
    if (not isinstance(entries, list) or len(entries) != 2 or not isinstance(listings, list)
            or type(source.get("totalListings")) is not int or source["totalListings"] != len(listings)
            or not isinstance(source.get("brokers"), list)):
        raise ValueError("Matthews source shape differs")
    by_tx = {entry.get("transaction"): entry for entry in entries if isinstance(entry, dict)}
    if set(by_tx) != {"sale", "lease"} or len(by_tx) != len(entries):
        raise ValueError("Matthews transaction scope differs")
    candidates, held, receipt_warnings = [], [], []
    held_identities = set()
    errors = 0
    for tx, entry in by_tx.items():
        if entry.get("sourceKey") != "matthews" or entry.get("supported") is not True:
            raise ValueError("Matthews source receipt differs")
        error = entry.get("error")
        selected = [(index, row) for index, row in enumerate(listings)
                    if isinstance(row, dict) and row.get("transactionMode") == tx]
        if error:
            errors += 1
            typed = entry.get("matthewsPartialOutcome")
            if "matthewsPartialOutcome" in entry:
                if (not isinstance(typed, dict)
                        or set(typed) != {"schema", "transaction", "temporaryRedirects", "detailFailures", "httpFailures"}
                        or typed.get("schema") != "matthews-partial-outcome/v1"
                        or typed.get("transaction") != tx
                        or any(type(typed.get(key)) is not int or typed[key] < 0
                               for key in ("temporaryRedirects", "detailFailures", "httpFailures"))):
                    raise ValueError("Matthews typed partial outcome invalid")
                counts = (typed["temporaryRedirects"], typed["detailFailures"], typed["httpFailures"])
            else:
                # Only older artifacts lack the versioned source outcome.
                match = re.fullmatch(
                    rf"Error: Matthews strict {tx} partial: ([0-9]+) temporary redirect\(s\) held; ([0-9]+) detail parse or identity failure\(s\) held(?:; ([0-9]+) settled HTTP detail failure\(s\) held)?", error
                ) if isinstance(error, str) else None
                if not match:
                    raise ValueError("Matthews partial failure is not a settled supported case")
                counts = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
            if selected:
                raise ValueError("Matthews partial failure emitted canonical rows")
            observations = entry.get("retainedObservations")
            if not isinstance(observations, list) or not observations:
                raise ValueError("Matthews partial observations missing")
            verified_counts = [0, 0, 0]
            requested_identities = set()
            for ordinal, observation in enumerate(observations):
                if isinstance(observation, dict) and observation.get("schema") == "matthews-completed-parse-observation/v1":
                    candidates.append((tx, "retained", ordinal, observation))
                    continue
                category = None
                if isinstance(observation, dict):
                    schema = observation.get("schema")
                    if (schema == "matthews-detail-redirect-observation/v1"
                            and observation.get("outcome") == "held_temporary_redirect"
                            and type(observation.get("httpStatus")) is int and observation["httpStatus"] == 307):
                        category = 0
                    elif (schema == "matthews-detail-observation/v1"
                            and type(observation.get("httpStatus")) is int and 200 <= observation["httpStatus"] < 300):
                        category = 1
                    elif (schema == "matthews-detail-http-observation/v1"
                            and observation.get("outcome") == "held_http_error"
                            and type(observation.get("httpStatus")) is int and observation["httpStatus"] in (404, 410)):
                        category = 2
                reason = "invalid_held_observation"
                if category is not None and observation.get("sourceKey") == "matthews" and observation.get("requestedTransaction") == tx:
                    url = observation.get("requestedUrl")
                    try:
                        parsed = urlsplit(url) if isinstance(url, str) else None
                        valid_url = (parsed is not None and parsed.scheme == "https"
                            and parsed.netloc == "www.matthews.com" and not parsed.query and not parsed.fragment
                            and re.fullmatch(r"/properties/[^/]+", parsed.path)
                            and not re.search(r"%(?![0-9a-fA-F]{2})", parsed.path)
                            and all(ord(c) >= 33 and ord(c) != 127 for c in url))
                    except ValueError:
                        valid_url = False
                    if valid_url:
                        identity = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m[0].lower(), parsed.path[12:])
                        if identity in requested_identities:
                            reason = "duplicate_held_identity"
                        else:
                            requested_identities.add(identity)
                            held_identities.add((tx, identity))
                            verified_counts[category] += 1
                            reason = "source_observation_held"
                held.append({"transaction": tx, "ordinal": ordinal, "reason": reason})
            if sum(counts) == 0:
                raise ValueError("Matthews partial receipt has no held outcome")
            if tuple(verified_counts) != counts:
                receipt_warnings.append({"transaction": tx, "reason": "held_receipt_counts_unverified",
                                         "reported": list(counts), "verified": verified_counts})
        else:
            if "matthewsPartialOutcome" in entry:
                raise ValueError("Matthews typed outcome missing partial failure")
            if type(entry.get("listingsCollected")) is not int or entry["listingsCollected"] != len(selected):
                raise ValueError("Matthews successful transaction count differs")
            for ordinal, row in selected:
                if row.get("sourceKey") != "matthews":
                    raise ValueError("Matthews successful source key differs")
                candidates.append((tx, "listing", ordinal, {"schema": "matthews-completed-parse-observation/v1",
                                                 "sourceKey": "matthews", "observation": row}))
    if not errors or sum(1 for row in listings if isinstance(row, dict) and row.get("transactionMode") in by_tx) != len(listings):
        raise ValueError("Matthews source is not a supported partial artifact")
    accepted = []
    for tx, kind, ordinal, item in candidates:
        try:
            observed = item.get("observation") or {}
            if observed.get("transactionType") != ("Sale" if tx == "sale" else "Lease"):
                raise ValueError("Matthews transaction mode differs")
            one = stage_observations([item], generation_id, meta["startedAt"], meta["finishedAt"])[0]
        except (ValueError, TypeError, KeyError, OverflowError):
            held.append({"transaction": tx, "ordinal": ordinal, "reason": "invalid_completed_observation"})
            continue
        if (tx, one["external_id"]) in held_identities:
            held.append({"transaction": tx, "ordinal": ordinal, "reason": "held_identity_conflict"})
            continue
        accepted.append((tx, kind, ordinal, one))
    identities = {}
    urls = {}
    for _, _, _, row in accepted:
        identities[row["external_id"]] = identities.get(row["external_id"], 0) + 1
        urls[row["source_url"]] = urls.get(row["source_url"], 0) + 1
    rows, accepted_observations = [], []
    for tx, kind, ordinal, row in accepted:
        if identities[row["external_id"]] != 1 or urls[row["source_url"]] != 1:
            held.append({"transaction": tx, "ordinal": ordinal, "reason": "duplicate_identity"})
        else:
            rows.append(row)
            accepted_observations.append({"transaction": tx, "kind": kind, "ordinal": ordinal})
    if not rows:
        raise ValueError("Matthews has no individually valid peers")
    return {"schema": "matthews-partial-fill-candidate/v1", "source_key": "matthews",
            "source_artifact_sha256": hashlib.sha256(raw).hexdigest(), "generation_id": generation_id,
            "generation_started_at": generation_started_at, "attempt_started_at": attempt_started_at,
            "attempt_finished_at": attempt_finished_at, "started_at": meta["startedAt"],
            "finished_at": meta["finishedAt"], "rows": rows, "accepted_observations": accepted_observations, "held": held,
            "receipt_warnings": receipt_warnings, "coverage": "partial_unknown", "mark_missing_safe": False}


def _read_private(path):
    """Read the same private regular file without a size or row-count cap."""
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Matthews artifact has a symlink ancestor")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600):
            raise ValueError("Matthews original custody invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        after, named = os.fstat(fd), path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) or getattr(before, key) != getattr(named, key) for key in fields):
            raise ValueError("Matthews original changed during read")
        return raw
    finally:
        os.close(fd)


def prepare_candidate(run_dir, manifest, attempt, *, git_identity, settled_rcs=(0, 1)):
    """Seal a derived candidate from the exact rejected file and owner attempt."""
    root = Path(run_dir)
    config = manifest.get("config") or {}
    checkpoint = (manifest.get("sources") or {}).get("matthews") or {}
    attempts = checkpoint.get("attempts") or []
    original_rel, expected_sha = attempt.get("rejected_artifact"), attempt.get("rejected_artifact_sha256")
    if (root.name != manifest.get("run_id") or manifest.get("collector_git_dirty") is not False
            or git_identity() != (manifest.get("collector_git_sha"), False)
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("collector_git_sha", "")))
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False or "matthews" not in config.get("sources", [])
            or not attempts or attempts[-1] is not attempt or type(attempt.get("number")) is not int or attempt["number"] < 1
            or type(attempt.get("rc")) is not int
            or attempt["rc"] not in settled_rcs or not attempt.get("finished_at")
            or not isinstance(original_rel, str) or not re.fullmatch(r"[a-f0-9]{64}", str(expected_sha or ""))):
        raise ValueError("Matthews candidate owner or settlement invalid")
    relative = Path(original_rel)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "rejected":
        raise ValueError("Matthews original path invalid")
    original = root / relative
    if original.is_symlink() or not original.resolve().is_relative_to(root.resolve()):
        raise ValueError("Matthews original escaped attempt directory")
    info = original.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Matthews original custody invalid")
    raw = _read_private(original)
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Matthews original hash changed")
    result = derive(raw, generation_id=manifest["run_id"], generation_started_at=manifest["started_at"],
                    attempt_started_at=attempt["started_at"], attempt_finished_at=attempt["finished_at"])
    if git_identity() != (manifest["collector_git_sha"], False) or _read_private(original) != raw:
        raise ValueError("Matthews original or code changed during projection")
    destination = root / "sources" / f"matthews-partial-fill-attempt-{attempt['number']}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise ValueError("Matthews candidate directory invalid")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    gate = gate_projection(raw, result)
    gate_bytes = json.dumps(gate, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    gate_path = root / "sources" / f"matthews-partial-gate-attempt-{attempt['number']}.json"
    gate_fd = os.open(gate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(gate_fd, "wb", closefd=False) as stream:
            stream.write(gate_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(gate_fd)
    return {"path": str(destination.relative_to(root)), "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded), "original_path": original_rel, "original_sha256": expected_sha,
            "gate_path": str(gate_path.relative_to(root)), "gate_sha256": hashlib.sha256(gate_bytes).hexdigest(),
            "gate_bytes": len(gate_bytes), "candidate_count": len(result["rows"]),
            "held_count": len(result["held"]), "attempt_rc": attempt["rc"]}


def recheck_candidate(run_dir, manifest, attempt, *, git_identity, target_sha256, settled_rcs=(0, 1)):
    """Rederive a sealed candidate before write/readback on the bound target."""
    root = Path(run_dir)
    config = manifest.get("config") or {}
    checkpoint = (manifest.get("sources") or {}).get("matthews") or {}
    target = (manifest.get("preflight") or {}).get("database_target") or {}
    candidate = attempt.get("matthews_partial_candidate") or {}
    attempts = checkpoint.get("attempts") or []
    if (root.name != manifest.get("run_id") or manifest.get("collector_git_dirty") is not False
            or git_identity() != (manifest.get("collector_git_sha"), False)
            or not re.fullmatch(r"[a-f0-9]{40}", str(manifest.get("collector_git_sha") or ""))
            or not re.fullmatch(r"[a-f0-9]{64}", str(target_sha256 or ""))
            or target.get("algorithm") != "sha256" or target.get("value") != target_sha256
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False or "matthews" not in config.get("sources", [])
            or not attempts or attempts[-1] is not attempt or candidate.get("state") != "sealed_unadmitted"
            or type(attempt.get("rc")) is not int or attempt["rc"] not in settled_rcs
            or candidate.get("attempt_rc") != attempt["rc"]
            or candidate.get("original_path") != attempt.get("rejected_artifact")
            or candidate.get("original_sha256") != attempt.get("rejected_artifact_sha256")):
        raise ValueError("Matthews candidate owner or target changed")
    if (type(attempt.get("number")) is not int or attempt["number"] < 1
            or candidate.get("path") != f"sources/matthews-partial-fill-attempt-{attempt['number']}.json"
            or candidate.get("gate_path") != f"sources/matthews-partial-gate-attempt-{attempt['number']}.json"):
        raise ValueError("Matthews candidate path changed")
    relative = Path(candidate.get("original_path") or "")
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "rejected":
        raise ValueError("Matthews original path changed")
    original = _read_private(root / relative)
    raw_candidate = _read_private(root / candidate["path"])
    raw_gate = _read_private(root / candidate["gate_path"])
    if (hashlib.sha256(original).hexdigest() != candidate["original_sha256"]
            or hashlib.sha256(raw_candidate).hexdigest() != candidate.get("sha256")
            or len(raw_candidate) != candidate.get("bytes")
            or hashlib.sha256(raw_gate).hexdigest() != candidate.get("gate_sha256")
            or len(raw_gate) != candidate.get("gate_bytes")):
        raise ValueError("Matthews sealed evidence changed")
    recomputed = derive(original, generation_id=manifest["run_id"], generation_started_at=manifest["started_at"],
                        attempt_started_at=attempt["started_at"], attempt_finished_at=attempt["finished_at"])
    if (json.loads(raw_candidate) != recomputed or candidate.get("candidate_count") != len(recomputed["rows"])
            or candidate.get("held_count") != len(recomputed["held"])
            or json.loads(raw_gate) != gate_projection(original, recomputed)):
        raise ValueError("Matthews candidate differs from source")
    if (_read_private(root / relative) != original or _read_private(root / candidate["path"]) != raw_candidate
            or _read_private(root / candidate["gate_path"]) != raw_gate
            or git_identity() != (manifest["collector_git_sha"], False)):
        raise ValueError("Matthews evidence or code changed during recheck")
    return recomputed


def gate_projection(raw, candidate):
    """Create an explicitly incomplete gate view, never an ingest artifact."""
    if not isinstance(raw, bytes) or not isinstance(candidate, dict):
        raise TypeError("invalid Matthews gate input")
    source = json.loads(raw)
    if candidate.get("source_artifact_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("Matthews gate source changed")
    rows = candidate.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Matthews gate rows missing")
    expected = {row["external_id"]: row for row in rows}
    if len(expected) != len(rows):
        raise ValueError("Matthews gate duplicate staged identity")
    emitted = {}
    references = candidate.get("accepted_observations")
    if not isinstance(references, list) or len(references) != len(rows):
        raise ValueError("Matthews gate accepted membership missing")
    entries = {entry["transaction"]: entry for entry in source["sources"]}
    seen = set()
    for ref in references:
        if (not isinstance(ref, dict) or set(ref) != {"transaction", "kind", "ordinal"}
                or ref["transaction"] not in entries or ref["kind"] not in {"retained", "listing"}
                or type(ref["ordinal"]) is not int or ref["ordinal"] < 0):
            raise ValueError("Matthews gate accepted membership invalid")
        tx, kind, ordinal = ref["transaction"], ref["kind"], ref["ordinal"]
        key = (tx, kind, ordinal)
        if key in seen:
            raise ValueError("Matthews gate duplicate membership")
        seen.add(key)
        if kind == "retained":
            entries_for_tx = entries[tx].get("retainedObservations") or []
            if ordinal >= len(entries_for_tx):
                raise ValueError("Matthews gate held ordinal missing")
            item = entries_for_tx[ordinal]
            if not isinstance(item, dict) or item.get("schema") != "matthews-completed-parse-observation/v1":
                raise ValueError("Matthews gate observation type differs")
            row = item.get("observation")
        else:
            if ordinal >= len(source["listings"]):
                raise ValueError("Matthews gate listing ordinal missing")
            row = source["listings"][ordinal]
            if not isinstance(row, dict) or row.get("transactionMode") != tx:
                raise ValueError("Matthews gate listing transaction differs")
        if not isinstance(row, dict) or row.get("id") not in expected:
            raise ValueError("Matthews gate membership identity differs")
        staged = stage_observations([{"schema": "matthews-completed-parse-observation/v1",
                                      "sourceKey": "matthews", "observation": row}],
            candidate["generation_id"], candidate["started_at"], candidate["finished_at"])[0]
        if staged != expected[row["id"]] or row["id"] in emitted:
            raise ValueError("Matthews gate staged identity differs")
        emitted[row["id"]] = {**row, "sourceKey": "matthews", "transactionMode": tx}
    if set(emitted) != set(expected):
        raise ValueError("Matthews gate membership differs")
    meta = dict(source["runMeta"])
    meta.pop("evidenceOnly", None)
    meta.pop("evidencePurpose", None)
    meta["matthewsPartialFill"] = {"schema": "matthews-partial-gate/v1", "coverage": "partial_unknown",
        "sourceArtifactSha256": candidate["source_artifact_sha256"],
        "candidateSha256": hashlib.sha256(json.dumps(candidate,ensure_ascii=False,separators=(",", ":"),allow_nan=False).encode()).hexdigest()}
    return {"runMeta": meta, "sources": [
        {"sourceKey": "matthews", "transaction": tx, "supported": True, "truncated": True,
         "error": "matthews_partial_incomplete", "listingsCollected": sum(row["transactionMode"] == tx for row in emitted.values())}
        for tx in ("sale", "lease")], "listings": list(emitted.values()),
        "totalListings": len(emitted), "brokers": source["brokers"]}


def admit_gate(artifact_path, run_dir, manifest, report, *, git_identity, target_sha256, phase, settled_rcs=(0, 1)):
    """Qualify only the bound partial projection under an existing live gate."""
    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts

    if phase not in ("source", "aggregate"):
        raise ValueError("invalid Matthews gate phase")
    checkpoint = manifest["sources"]["matthews"]
    attempts = checkpoint.get("attempts") or []
    if not attempts:
        raise ValueError("Matthews gate attempt missing")
    attempt = attempts[-1]
    data = recheck_candidate(run_dir, manifest, attempt, git_identity=git_identity,
                             target_sha256=target_sha256, settled_rcs=settled_rcs)
    candidate = attempt["matthews_partial_candidate"]
    artifact = Path(artifact_path)
    if (artifact != Path(run_dir) / candidate["gate_path"]
            or checkpoint.get("artifact", {}).get("sha256") != candidate["gate_sha256"]
            or checkpoint.get("artifact", {}).get("matthews_partial") is not True):
        raise ValueError("Matthews gate artifact binding differs")
    current, errors, sources, _, rejects = count_artifacts([str(artifact)], True)
    if (sources != {"matthews"} or errors.get("matthews") != "matthews_partial_incomplete"
            or rejects != 0 or current.get("matthews") != len(data["rows"])):
        raise ValueError("Matthews gate counts differ")
    validate_report_scope(report, manifest, "matthews", artifact)
    summary, gate = report["summary"], report["per_source"]["matthews"]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != len(data["rows"])):
        raise ValueError("Matthews gate scope differs")
    if gate.get("verdict") == "ok_additive_verified_subset":
        raw_pair = gate.get("raw_verdict"), gate.get("raw_reason")
    else:
        raw_pair = gate.get("verdict"), gate.get("reason")
    allowed = {("first_seen", "no baseline row; cannot gate (first sight); a source pass also errored this run"),
               ("hold", "source pass error: matthews_partial_incomplete")}
    if raw_pair not in allowed or gate.get("mark_missing_safe") is not False:
        raise ValueError("Matthews gate reason differs")
    binding = {"schema": "matthews-existing-parent-partial-v1", "scope": "matthews_existing_parent_partial_v1",
               "source_key": "matthews", "artifact_sha256": candidate["gate_sha256"],
               "candidate_sha256": candidate["sha256"], "source_artifact_sha256": candidate["original_sha256"],
               "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
               "database_target_sha256": target_sha256, "candidate_count": len(data["rows"]),
               "source_held_count": len(data["held"]), "whole_source_coverage": False}
    if phase == "aggregate" and checkpoint.get("gate", {}).get("current_inventory_proof") != binding:
        raise ValueError("Matthews source gate binding changed")
    gate.update(raw_verdict=raw_pair[0], raw_reason=raw_pair[1], verdict="ok_additive_verified_subset",
                reason="Matthews existing-parent peers admitted as partial; unresolved observations remain held",
                mark_missing_safe=False, admission_scope=binding["scope"], current_inventory_proof=binding)
    for key in ("hold_sources", "mark_missing_safe_sources"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != "matthews"]
    if isinstance(summary.get("mark_missing_safe_brokerages"), list):
        summary["mark_missing_safe_brokerages"] = [value for value in summary["mark_missing_safe_brokerages"] if value != "matthews"]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get("matthews"), dict):
        report["per_brokerage"]["matthews"]["mark_missing_safe"] = False
    if isinstance(summary.get("verdict_counts"), dict):
        counts = {}
        for value in report["per_source"].values():
            verdict = value.get("verdict")
            counts[verdict] = counts.get(verdict, 0) + 1
        summary["verdict_counts"] = counts
    report["scope"] = {"kind": binding["scope"] if phase == "source" else "collector_registry_verified_subset",
                       "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return binding
