"""Build a private source-qualified Franklin partial artifact from exact input bytes."""
import copy
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cre_franklin_site_projection import project
from cre_franklin_site_sql import stage
from cre_ingest import parse_strict_freshness_timestamp


def derive(raw, binding, env):
    """Return the site subset and account for original Buildout peers as pending.

    The owner must retain the original bytes, bind this derived output and
    independently authorize its partial claims before passing it to ingest.
    """
    projected = project(raw, binding["generation_id"], env)
    if projected["artifact_sha256"] != binding["artifact_sha256"]:
        raise ValueError("Franklin original artifact digest differs")
    original = json.loads(raw)
    meta = original.get("runMeta") or {}
    fresh = meta.get("freshness") or {}
    if (meta.get("mode") != "full" or meta.get("transactions") != ["sale", "lease"]
            or meta.get("maxItemsPerSource") is not None or meta.get("terminalCaptureFailure")
            or meta.get("startedAt") != binding["started_at"] or meta.get("finishedAt") != binding["finished_at"]
            or fresh.get("generationId") != binding["generation_id"]
            or fresh.get("generationStartedAt") != binding["generation_started_at"]
            or fresh.get("requireFreshDetails") is not True):
        raise ValueError("Franklin original scope differs")
    entries = original["sources"]
    if len(entries) != 2 or {entry.get("transaction") for entry in entries} != {"sale", "lease"}:
        raise ValueError("Franklin original transaction scope differs")
    subset = projected["projection"]
    if not subset["listings"]:
        raise ValueError("Franklin has no identifiable writable site cards")
    listings = copy.deepcopy(subset["listings"])
    for listing in listings:
        observation = listing["franklinSiteObservation"]
        listing["inventoryObservedAt"] = observation["observedAt"]
        listing["freshnessProvenance"] = {
            "generationId": binding["generation_id"],
            "method": "franklin_site_card",
            "cacheDisposition": "live",
            "detailScope": "partial_site_card",
        }
    sources = []
    for original_entry in entries:
        tx = original_entry["transaction"]
        count = sum(row["transactionMode"] == tx for row in listings)
        sources.append({
            "sourceKey": "franklin-street", "transaction": tx, "supported": True,
            "truncated": True, "listingsCollected": count,
            "error": "source_incomplete" if original_entry.get("error") else "buildout_peers_pending",
        })
    derived_meta = copy.deepcopy(meta)
    derived_meta.pop("evidenceOnly", None)
    derived_meta.pop("evidencePurpose", None)
    derived_meta.pop("franklinSidecar", None)
    derived_meta["franklinSitePartial"] = {
        "schema": "franklin-site-partial-v1",
        "sourceArtifactSha256": binding["artifact_sha256"],
        "coverage": "partial_unknown",
        "buildoutPending": subset["buildout"]["pendingListings"],
        "heldCardObservations": len(subset["held"]),
        "identityUncertainty": subset["identityUncertainty"],
    }
    return {"runMeta": derived_meta, "sources": sources, "listings": listings,
            "brokers": [], "totalListings": len(listings)}

def validate_derived(data, generation_id, generation_started_at, attempt_started_at, *, now=None, max_age_hours=24):
    """Check a derived site's structure; this does not authenticate its original."""
    if not isinstance(data, dict) or not isinstance(data.get("runMeta"), dict):
        raise TypeError("Franklin derived artifact invalid")
    meta = data["runMeta"]
    marker = meta.get("franklinSitePartial")
    fresh = meta.get("freshness") or {}
    if (not isinstance(marker, dict) or marker.get("schema") != "franklin-site-partial-v1"
            or marker.get("coverage") != "partial_unknown"
            or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("sourceArtifactSha256", "")))
            or type(marker.get("buildoutPending")) is not int or marker["buildoutPending"] < 0
            or type(marker.get("heldCardObservations")) is not int or marker["heldCardObservations"] < 0
            or (marker.get("identityUncertainty") or {}).get("siteToBuildout") != "unresolved"
            or (marker.get("identityUncertainty") or {}).get("possibleDuplicateProperties") is not True
            or fresh.get("generationId") != generation_id
            or fresh.get("requireFreshDetails") is not True
            or meta.get("mode") != "full" or meta.get("transactions") != ["sale", "lease"]
            or meta.get("maxItemsPerSource") is not None):
        raise ValueError("Franklin derived scope invalid")
    current = now or datetime.now(UTC)
    if isinstance(generation_started_at, datetime):
        generation_started_at = generation_started_at.isoformat()
    if isinstance(attempt_started_at, datetime):
        attempt_started_at = attempt_started_at.isoformat()
    generation_start = parse_strict_freshness_timestamp(generation_started_at, field="generation_started_at")
    if parse_strict_freshness_timestamp(fresh.get("generationStartedAt"), field="generationStartedAt") != generation_start:
        raise ValueError("Franklin derived generation clock differs")
    attempt_start = parse_strict_freshness_timestamp(attempt_started_at, field="attempt_started_at")
    started = parse_strict_freshness_timestamp(meta.get("startedAt"), field="startedAt")
    finished = parse_strict_freshness_timestamp(meta.get("finishedAt"), field="finishedAt")
    if not generation_start <= attempt_start <= started <= finished <= current + timedelta(minutes=5):
        raise ValueError("Franklin derived attempt clock invalid")
    rows = data.get("listings")
    if not isinstance(rows, list) or not rows or data.get("totalListings") != len(rows) or data.get("brokers") != []:
        raise ValueError("Franklin derived rows invalid")
    staged = stage(rows)
    for row in rows:
        observed = parse_strict_freshness_timestamp(row.get("inventoryObservedAt"), field="inventoryObservedAt")
        provenance = row.get("freshnessProvenance") or {}
        if (observed < max(generation_start, current - timedelta(hours=max_age_hours))
                or observed > finished + timedelta(minutes=5)
                or row["franklinSiteObservation"]["observedAt"] != row["inventoryObservedAt"]
                or provenance != {"generationId": generation_id, "method": "franklin_site_card",
                                  "cacheDisposition": "live", "detailScope": "partial_site_card"}):
            raise ValueError("Franklin derived observation invalid")
    sources = data.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        raise ValueError("Franklin derived source entries invalid")
    for tx in ("sale", "lease"):
        found = [item for item in sources if isinstance(item, dict) and item.get("transaction") == tx]
        if (len(found) != 1 or found[0].get("sourceKey") != "franklin-street"
                or found[0].get("supported") is not True or found[0].get("truncated") is not True
                or found[0].get("error") not in ("source_incomplete", "buildout_peers_pending")
                or found[0].get("listingsCollected") != sum(row["transactionMode"] == tx for row in rows)):
            raise ValueError("Franklin derived source entry differs")
    return staged


def prepare_candidate(run_dir, manifest, attempt, original_relative, original_sha256, env, *, git_identity, now=None):
    """Publish a private preliminary candidate from one settled rejected attempt.

    This is not source admission; the caller must retain the original and the
    existing gate and claims path must rederive it before any listing write.
    """
    root = Path(run_dir)
    if (root.name != manifest.get("run_id") or not isinstance(original_relative, str) or not isinstance(original_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", original_sha256)
            or manifest.get("collector_git_dirty") is not False
            or git_identity() != (manifest.get("collector_git_sha"), False)
            or manifest.get("config", {}).get("transactions") != ["sale", "lease"]
            or manifest.get("config", {}).get("max_items") != 0
            or manifest.get("config", {}).get("additive") is not True
            or manifest.get("config", {}).get("mark_missing") is not False
            or manifest.get("config", {}).get("status_activation") is not False
            or "franklin-street" not in manifest.get("config", {}).get("sources", [])
            or not manifest.get("sources", {}).get("franklin-street", {}).get("attempts")
            or manifest["sources"]["franklin-street"]["attempts"][-1] is not attempt
            or type(attempt.get("rc")) is not int or not attempt.get("finished_at")):
        raise ValueError("Franklin candidate owner invalid")
    relative = Path(original_relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Franklin original path invalid")
    original = root / relative
    if not original.is_relative_to(root) or original.is_symlink():
        raise ValueError("Franklin original path invalid")
    from cre_code_handoff_git import read_file

    info = original.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("Franklin original custody invalid")
    raw = read_file(original)
    if hashlib.sha256(raw).hexdigest() != original_sha256:
        raise ValueError("Franklin original digest changed")
    meta = json.loads(raw).get("runMeta") or {}
    binding = {"generation_id": manifest["run_id"], "artifact_sha256": original_sha256,
               "generation_started_at": manifest["started_at"],
               "started_at": meta.get("startedAt"), "finished_at": meta.get("finishedAt")}
    value = derive(raw, binding, env)
    validate_derived(value, binding["generation_id"], binding["generation_started_at"],
                     attempt["started_at"], now=now)
    if git_identity() != (manifest["collector_git_sha"], False) or read_file(original) != raw:
        raise ValueError("Franklin original or code changed during projection")
    destination = root / "sources" / "franklin-street.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise ValueError("Franklin candidate directory invalid")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    return {"path": str(destination.relative_to(root)), "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded), "original_path": original_relative, "original_sha256": original_sha256,
            "buildout_pending": value["runMeta"]["franklinSitePartial"]["buildoutPending"],
            "held_observations": value["runMeta"]["franklinSitePartial"]["heldCardObservations"]}
