"""Derive source-scoped NAI inventory recovery without granting write authority.

The coordinator and source gate must bind the original bytes, owner, source
attempt, code and target before any candidate derived here can be admitted.
"""
import hashlib
import json
import os
import re
import stat
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from cre_nai_post_partial import _encode_json, _timestamp, partition_posts

SCHEMA = "nai-post-inventory-recovery/v1"
_ACCESS = re.compile(r"(?:\b(?:401|403|429)\b|unauthori[sz]ed|forbidden|rate.?limit|retry.after|challenge|captcha)", re.I)
_FRESHNESS_ERROR = re.compile(r"nai-global/(?:sale|lease) (?:freshness metric detailErrors must be zero|lacks detail observations)")


def derive(original: bytes, generation: str, generation_started: str, attempt_started: str,
           attempt_finished: str, *, now, max_age_hours=24):
    """Reproduce a partial publication partition from one exact source body."""
    data = json.loads(original)
    exact = json.loads(original, parse_float=Decimal)
    if not isinstance(data, dict) or data.get("schema") is not None:
        raise ValueError("NAI recovery original schema invalid")
    meta = data.get("runMeta")
    freshness = meta.get("freshness") if isinstance(meta, dict) else None
    if (not isinstance(meta, dict) or meta.get("mode") != "full"
            or meta.get("transactions") != ["sale", "lease"] or "maxItemsPerSource" not in meta
            or meta.get("maxItemsPerSource") is not None
            or not isinstance(freshness, dict) or freshness.get("generationId") != generation
            or _timestamp(freshness.get("generationStartedAt")) != _timestamp(generation_started)):
        raise ValueError("NAI recovery source scope differs")
    start, finish = _timestamp(meta.get("startedAt")), _timestamp(meta.get("finishedAt"))
    if not (_timestamp(generation_started) <= _timestamp(attempt_started) <= start <= finish
            <= _timestamp(attempt_finished) and finish <= now + timedelta(minutes=5)):
        raise ValueError("NAI recovery source clocks differ")
    rows, entries = data.get("listings"), data.get("sources")
    if (not isinstance(rows, list) or not rows or data.get("totalListings") != len(rows)
            or not isinstance(data.get("brokers"), list) or not isinstance(entries, list) or len(entries) != 2):
        raise ValueError("NAI recovery source shape differs")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("NAI recovery source entry invalid")
        tx = entry.get("transaction")
        if (entry.get("sourceKey") != "nai-global" or tx not in {"sale", "lease"} or tx in seen
                or entry.get("supported") is not True or entry.get("truncated") is not False or entry.get("error")
                or type(entry.get("listingsCollected")) is not int or entry["listingsCollected"] < 0):
            raise ValueError("NAI recovery source pass is not settled complete")
        seen.add(tx)
    if sum(e["listingsCollected"] for e in entries) != len(rows):
        raise ValueError("NAI recovery source count differs")
    if any(sum(1 for row in rows if isinstance(row, dict) and row.get("transactionMode") == entry["transaction"])
           > entry["listingsCollected"] for entry in entries):
        raise ValueError("NAI recovery transaction counts differ")
    inventory_only = False
    for row in rows:
        if not isinstance(row, dict):
            continue  # Preserve it in the original and let the partition hold it.
        detail_error = row.get("detailError")
        if detail_error and (not isinstance(detail_error, str) or _ACCESS.search(detail_error)):
            raise ValueError("NAI recovery access outcome is not qualified")
        provenance = row.get("freshnessProvenance")
        if isinstance(provenance, dict) and provenance.get("detailScope") == "inventory_only":
            inventory_only = True
    if not inventory_only:
        raise ValueError("NAI recovery lacks an inventory-only observation")
    partition = partition_posts(rows, generation, capture_start=max(start, now - timedelta(hours=max_age_hours)),
                                capture_finish=finish, exact_listings=exact["listings"])
    # Use the same singleton staging precheck as the later candidate validator.
    # Keep invalid peers in the sealed original and in the explicit held set;
    # do not let one malformed mapped row abort otherwise identifiable posts.
    from cre_checkpoint_refresh import ArtifactValidationError, compute_staged_stats

    initially_held = {item["index"] for item in partition["held"]}
    rejected_ids = set()
    for index, row in enumerate(rows):
        if index in initially_held:
            continue
        try:
            stats = compute_staged_stats({"runMeta": meta, "brokers": data["brokers"], "listings": [row]})
            valid = stats["rejected_by_ingest"] == 0 and stats["staged_unique"] == 1
        except (ArtifactValidationError, TypeError, ValueError):
            valid = False
        if not valid:
            partition["held"].append({"index": index, "reason": "invalid_projection"})
            rejected_ids.add(row["id"])
    partition["held"].sort(key=lambda item: item["index"])
    partition["staged"] = [item for item in partition["staged"] if item["external_id"] not in rejected_ids]
    if not partition["staged"]:
        raise ValueError("NAI recovery has no identified source posts")
    held_indices = {item["index"] for item in partition["held"]}
    selected = [row for index, row in enumerate(exact["listings"]) if index not in held_indices]
    candidate = {**exact, "schema": SCHEMA, "listings": selected, "totalListings": len(selected),
                 "naiRecovery": {"originalSha256": hashlib.sha256(original).hexdigest(),
                                 "originalCount": len(rows), "sourceHeld": partition["held"]}}
    return (_encode_json(candidate) + "\n").encode("ascii"), partition


def validate_structure(data, generation, generation_started, attempt_started, *, now, max_age_hours):
    """Validate candidate structure; original custody is checked at the gate."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("NAI recovery candidate schema differs")
    receipt, meta = data.get("naiRecovery"), data.get("runMeta")
    if (not isinstance(receipt, dict) or set(receipt) != {"originalSha256", "originalCount", "sourceHeld"}
            or not isinstance(receipt.get("originalSha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["originalSha256"])
            or type(receipt.get("originalCount")) is not int or receipt["originalCount"] < 1
            or not isinstance(receipt.get("sourceHeld"), list)
            or not isinstance(meta, dict) or not isinstance(meta.get("freshness"), dict)
            or meta.get("mode") != "full"
            or meta.get("transactions") != ["sale", "lease"] or "maxItemsPerSource" not in meta
            or meta.get("maxItemsPerSource") is not None
            or (meta.get("freshness") or {}).get("generationId") != generation
            or _timestamp((meta.get("freshness") or {}).get("generationStartedAt")) != _timestamp(generation_started)):
        raise ValueError("NAI recovery candidate scope differs")
    start, finish = _timestamp(meta.get("startedAt")), _timestamp(meta.get("finishedAt"))
    if not (_timestamp(generation_started) <= _timestamp(attempt_started) <= start <= finish
            <= now + timedelta(minutes=5)):
        raise ValueError("NAI recovery candidate clocks differ")
    rows = data.get("listings")
    if not isinstance(rows, list) or not rows or data.get("totalListings") != len(rows):
        raise ValueError("NAI recovery candidate rows differ")
    held = receipt["sourceHeld"]
    if (any(not isinstance(item, dict) or set(item) != {"index", "reason"}
            or type(item["index"]) is not int or not 0 <= item["index"] < receipt["originalCount"]
            or item["reason"] not in {"unqualified_source_post", "duplicate_post_id", "conflicting_source_alias", "invalid_projection"}
            for item in held) or [item["index"] for item in held] != sorted({item["index"] for item in held})
            or len(rows) + len(held) != receipt["originalCount"]):
        raise ValueError("NAI recovery candidate partition differs")
    partition = partition_posts(rows, generation, capture_start=max(start, now - timedelta(hours=max_age_hours)),
                                capture_finish=finish)
    if partition["held"] or len(partition["staged"]) != len(rows):
        raise ValueError("NAI recovery selected rows differ")
    return start, finish


def prepare_owned(run_dir, manifest, owner, *, now):
    """Publish a new private candidate only from the exact settled source body."""
    import cre_checkpoint_refresh as refresh
    from cre_code_handoff_git import read_file

    if not isinstance(owner, refresh.SharedLock):
        raise TypeError("NAI recovery owner invalid")
    owner._require_authority()
    if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
        raise ValueError("NAI recovery lock differs")
    config, checkpoint = manifest["config"], manifest["sources"]["nai-global"]
    attempts = checkpoint.get("attempts") or []
    if (not attempts or config.get("pipeline") is not True or config.get("additive") is not True
            or config.get("mark_missing") is not False or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0):
        raise ValueError("NAI recovery configuration differs")
    attempt = attempts[-1]
    number = attempt.get("number")
    if (type(number) is not int or number < 1 or type(attempt.get("rc")) is not int or attempt["rc"] != 0
            or not isinstance(attempt.get("error"), str) or not _FRESHNESS_ERROR.fullmatch(attempt["error"])
            or attempt.get("rejected_artifact") != f"rejected/nai-global-attempt-{number}.json"):
        raise ValueError("NAI recovery source attempt is not eligible")
    producer, dirty = refresh.git_identity()
    if dirty or manifest.get("collector_git_dirty") is not False or producer != manifest.get("collector_git_sha"):
        raise ValueError("NAI recovery producer differs")
    root = Path(run_dir)
    original = root / attempt["rejected_artifact"]
    st = original.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
            or stat.S_IMODE(st.st_mode) != 0o600):
        raise ValueError("NAI recovery original custody invalid")
    raw = read_file(original)
    if hashlib.sha256(raw).hexdigest() != attempt.get("rejected_artifact_sha256"):
        raise ValueError("NAI recovery original digest differs")
    try:
        encoded, _ = derive(raw, manifest["run_id"], manifest["started_at"], attempt["started_at"],
                            attempt["finished_at"], now=now)
    except (KeyError, TypeError, ValueError, OverflowError):
        # A settled source-local refusal leaves the original private artifact
        # untouched; ownership and custody failures above remain global.
        return None
    owner._require_authority()
    if refresh.git_identity() != (producer, False) or read_file(original) != raw:
        raise ValueError("NAI recovery source or producer changed")
    target = root / "sources" / "nai-global.json"
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        os.fsync(directory)
    finally:
        os.close(directory)
    owner._require_authority()
    identity = target.stat()
    checkpoint["candidate"] = {"path": "sources/nai-global.json", "attempt_started_at": attempt["started_at"],
        "attempt_number": number, "file_identity": [identity.st_dev, identity.st_ino, identity.st_size,
                                                       identity.st_mtime_ns, identity.st_ctime_ns], "bytes": identity.st_size}
    checkpoint["state"] = "candidate"
    refresh.save_manifest(root, manifest)
    return target
