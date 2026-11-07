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

from cre_ingest import parse_strict_freshness_timestamp
from cre_matthews_partial_fill import stage_observations


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
    candidates, held = [], []
    errors = 0
    for tx, entry in by_tx.items():
        if entry.get("sourceKey") != "matthews" or entry.get("supported") is not True:
            raise ValueError("Matthews source receipt differs")
        error = entry.get("error")
        selected = [row for row in listings if isinstance(row, dict) and row.get("transactionMode") == tx]
        if error:
            errors += 1
            match = re.fullmatch(
                rf"Error: Matthews strict {tx} partial: ([0-9]+) temporary redirect\(s\) held; ([0-9]+) detail parse or identity failure\(s\) held", error
            ) if isinstance(error, str) else None
            if not match or selected:
                raise ValueError("Matthews partial failure is not a settled supported case")
            observations = entry.get("retainedObservations")
            if not isinstance(observations, list) or not observations:
                raise ValueError("Matthews partial observations missing")
            redirects = sum(isinstance(item, dict) and item.get("schema") == "matthews-detail-redirect-observation/v1"
                            and item.get("outcome") == "held_temporary_redirect" and item.get("httpStatus") == 307
                            and item.get("requestedTransaction") == tx for item in observations)
            failures = sum(isinstance(item, dict) and item.get("schema") == "matthews-detail-observation/v1"
                           for item in observations)
            if (redirects, failures) != (int(match.group(1)), int(match.group(2))) or redirects + failures == 0:
                raise ValueError("Matthews held observations differ from failure receipt")
            for ordinal, observation in enumerate(observations):
                if isinstance(observation, dict) and observation.get("schema") == "matthews-completed-parse-observation/v1":
                    candidates.append((tx, ordinal, observation))
                else:
                    held.append({"transaction": tx, "ordinal": ordinal, "reason": "source_observation_held"})
        else:
            if type(entry.get("listingsCollected")) is not int or entry["listingsCollected"] != len(selected):
                raise ValueError("Matthews successful transaction count differs")
            for ordinal, row in enumerate(selected):
                if row.get("sourceKey") != "matthews":
                    raise ValueError("Matthews successful source key differs")
                candidates.append((tx, ordinal, {"schema": "matthews-completed-parse-observation/v1",
                                                 "sourceKey": "matthews", "observation": row}))
    if not errors or sum(1 for row in listings if isinstance(row, dict) and row.get("transactionMode") in by_tx) != len(listings):
        raise ValueError("Matthews source is not a supported partial artifact")
    accepted = []
    for tx, ordinal, item in candidates:
        try:
            observed = item.get("observation") or {}
            if observed.get("transactionType") != ("Sale" if tx == "sale" else "Lease"):
                raise ValueError("Matthews transaction mode differs")
            one = stage_observations([item], generation_id, meta["startedAt"], meta["finishedAt"])[0]
        except (ValueError, TypeError, KeyError, OverflowError):
            held.append({"transaction": tx, "ordinal": ordinal, "reason": "invalid_completed_observation"})
            continue
        accepted.append((tx, ordinal, one))
    identities = {}
    urls = {}
    for _, _, row in accepted:
        identities[row["external_id"]] = identities.get(row["external_id"], 0) + 1
        urls[row["source_url"]] = urls.get(row["source_url"], 0) + 1
    rows = []
    for tx, ordinal, row in accepted:
        if identities[row["external_id"]] != 1 or urls[row["source_url"]] != 1:
            held.append({"transaction": tx, "ordinal": ordinal, "reason": "duplicate_identity"})
        else:
            rows.append(row)
    if not rows:
        raise ValueError("Matthews has no individually valid peers")
    return {"schema": "matthews-partial-fill-candidate/v1", "source_key": "matthews",
            "source_artifact_sha256": hashlib.sha256(raw).hexdigest(), "generation_id": generation_id,
            "generation_started_at": generation_started_at, "attempt_started_at": attempt_started_at,
            "attempt_finished_at": attempt_finished_at, "started_at": meta["startedAt"],
            "finished_at": meta["finishedAt"], "rows": rows, "held": held,
            "coverage": "partial_unknown", "mark_missing_safe": False}


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
    return {"path": str(destination.relative_to(root)), "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded), "original_path": original_rel, "original_sha256": expected_sha,
            "candidate_count": len(result["rows"]), "held_count": len(result["held"]), "attempt_rc": attempt["rc"]}


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
            or candidate.get("path") != f"sources/matthews-partial-fill-attempt-{attempt['number']}.json"):
        raise ValueError("Matthews candidate path changed")
    relative = Path(candidate.get("original_path") or "")
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "rejected":
        raise ValueError("Matthews original path changed")
    original = _read_private(root / relative)
    raw_candidate = _read_private(root / candidate["path"])
    if (hashlib.sha256(original).hexdigest() != candidate["original_sha256"]
            or hashlib.sha256(raw_candidate).hexdigest() != candidate.get("sha256")
            or len(raw_candidate) != candidate.get("bytes")):
        raise ValueError("Matthews sealed evidence changed")
    recomputed = derive(original, generation_id=manifest["run_id"], generation_started_at=manifest["started_at"],
                        attempt_started_at=attempt["started_at"], attempt_finished_at=attempt["finished_at"])
    if (json.loads(raw_candidate) != recomputed or candidate.get("candidate_count") != len(recomputed["rows"])
            or candidate.get("held_count") != len(recomputed["held"])):
        raise ValueError("Matthews candidate differs from source")
    if (_read_private(root / relative) != original or _read_private(root / candidate["path"]) != raw_candidate
            or git_identity() != (manifest["collector_git_sha"], False)):
        raise ValueError("Matthews evidence or code changed during recheck")
    return recomputed
