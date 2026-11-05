"""Validate bounded Colliers partial detail observations before staging.

This module does not authorize writes: caller must bind the original page spool,
settled owner, artifact, code, target and both checkpoint gates.
"""
import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

_HASH = re.compile(r"^[0-9a-f]{64}$")


def _time(value):
    if not isinstance(value, str):
        raise TypeError("partial observation clock missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("partial observation clock invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("partial observation clock is naive")
    return parsed.astimezone(UTC)


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
    return {key: state[key] for key in keys} | {
        "generation": run_dir.name, "generation_started_at": manifest["started_at"], "attempt": attempt_number,
        "started_at": attempt["started_at"], "finished_at": attempt["finished_at"],
        "producer_sha": current_code_sha, "database_target_sha256": target["value"],
    }


def verify_detail_record(item, generation, attempt, started_at, finished_at):
    """Validate one private native detail against its exact partial listing."""
    if not isinstance(item, dict) or set(item) != {"listing", "source", "decodedDetail", "nativeBase64"}:
        raise ValueError("partial detail record invalid")
    listing, source, raw = item["listing"], item["source"], item["decodedDetail"]
    if not isinstance(listing, dict) or not isinstance(source, dict) or not isinstance(raw, str):
        raise TypeError("partial detail content invalid")
    encoded_native = item["nativeBase64"]
    if not isinstance(encoded_native, str) or len(encoded_native) > ((16 * 1024 * 1024 + 2) // 3) * 4:
        raise ValueError("partial native bytes invalid")
    try:
        native = base64.b64decode(encoded_native, validate=True)
        decoded_native = native.decode("utf-8-sig")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("partial native bytes invalid") from exc
    if (len(native) > 16 * 1024 * 1024 or base64.b64encode(native).decode("ascii") != encoded_native
            or decoded_native != raw or hashlib.sha256(native).hexdigest() != source.get("nativeSha256")):
        raise ValueError("partial native binding differs")
    expected_fields = {"generation", "attempt", "start", "page", "pageSize", "offset", "inventoryObservedAt",
                       "detailObservedAt", "mapProjectId", "listSha256", "mapSha256", "detailTokenSha256", "decodedDetailSha256", "nativeSha256"}
    if set(source) != expected_fields or source["generation"] != generation or type(attempt) is not int or type(source["attempt"]) is not int or source["attempt"] != attempt:
        raise ValueError("partial generation or attempt differs")
    if any(type(source[key]) is not int or source[key] < (0 if key == "offset" else 1) for key in ("start", "page", "pageSize", "offset")) or source["offset"] >= source["pageSize"]:
        raise ValueError("partial source position invalid")
    if any(not isinstance(source[key], str) or not _HASH.fullmatch(source[key]) for key in ("listSha256", "mapSha256", "detailTokenSha256", "decodedDetailSha256", "nativeSha256")):
        raise ValueError("partial source digest invalid")
    if len(raw.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("partial detail exceeds bound")
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != source["decodedDetailSha256"]:
        raise ValueError("partial detail digest differs")
    if not (_time(started_at) <= _time(source["inventoryObservedAt"]) <= _time(finished_at)
            <= _time(source["detailObservedAt"])):
        raise ValueError("partial observation clock differs")
    try:
        detail = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("partial detail JSON invalid") from exc
    if not isinstance(detail, dict) or detail.get("success") is False:
        raise ValueError("partial detail source response invalid")
    summary = detail.get("ProjectSummary")
    visibility = summary.get("AttributeVisibility") if isinstance(summary, dict) else None
    observed_id = visibility.get("ProjectId") if isinstance(visibility, dict) else None
    if observed_id is None and isinstance(summary, dict):
        observed_id = summary.get("ProjectId")
    source_id = source["mapProjectId"]
    provenance = listing.get("freshnessProvenance")
    proof = listing.get("colliersPartialEvidence")
    mapped_detail = listing.get("colliersSalesTrackerDetail")
    if (not isinstance(source_id, str) or not source_id or observed_id is None or isinstance(observed_id, bool)
            or not isinstance(observed_id, (str, int, float)) or str(observed_id).strip() != source_id
            or listing.get("sourceKey") != "colliers" or listing.get("transactionMode") != "sale"
            or listing.get("id") != source_id or listing.get("brokerIds") != []
            or listing.get("preserveChildCollections") is not True
            or listing.get("inventoryObservedAt") != source["inventoryObservedAt"]
            or listing.get("detailObservedAt") != source["detailObservedAt"]
            or not isinstance(provenance, dict) or provenance.get("generationId") != generation
            or provenance.get("method") != "colliers_salestracker_slp_detail"
            or not isinstance(proof, dict) or proof != {"schema": "colliers-partial-detail-v1", **source}
            or not isinstance(mapped_detail, dict) or mapped_detail.get("projectId") != source_id):
        raise ValueError("partial detail identity differs")
    return {"external_id": source_id, "position": source["start"] + source["offset"],
            "list_sha256": source["listSha256"], "map_sha256": source["mapSha256"],
            "detail_sha256": source["decodedDetailSha256"]}


def stage_detail_record(item, generation, attempt, started_at, finished_at, artifact_finished_at):
    """Bind verified native identity to the actual canonical staging mapper."""
    from urllib.parse import parse_qs, urlsplit

    from cre_ingest import to_row

    verified = verify_detail_record(item, generation, attempt, started_at, finished_at)
    artifact_finish = _time(artifact_finished_at)
    if _time(item["source"]["detailObservedAt"]) > artifact_finish:
        raise ValueError("partial detail after artifact finished")
    if artifact_finish > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("partial artifact finish exceeds clock-skew allowance")
    listing = item["listing"]
    url = listing.get("url")
    if not isinstance(url, str):
        raise TypeError("partial source URL missing")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "my.rcm1.com" or parsed.username is not None
            or parsed.password is not None or parsed.port is not None or parsed.fragment
            or parsed.path.lower() not in {"/handler/landing.aspx", "/handler/modern.aspx"}):
        raise ValueError("partial source URL invalid")
    tokens = parse_qs(parsed.query, keep_blank_values=True).get("pv", [])
    if (len(tokens) != 1 or not tokens[0]
            or hashlib.sha256(tokens[0].encode("utf-8")).hexdigest() != item["source"]["detailTokenSha256"]):
        raise ValueError("partial source token differs")
    row = to_row(listing, {}, artifact_finished_at)
    if (not isinstance(row, dict) or row.get("slug") != "colliers"
            or row.get("external_id") != verified["external_id"]
            or row["external_id"].startswith("main:")
            or row.get("source_url") != url or row.get("transaction_type") != "sale"):
        raise ValueError("partial staged identity differs")
    return {**{key: row.get(key) for key in ("slug", "external_id", "source_url", "canonical_url", "transaction_type")},
            "inventory_at": item["source"]["inventoryObservedAt"], "detail_at": item["source"]["detailObservedAt"]}


# Bound the additional per-listing member, not pre-existing parent raw_data.
# Overflow is a held record; the sealed private evidence remains available.
MAX_NATIVE_HISTORY_BYTES = 4 * 1024 * 1024
NATIVE_HISTORY_KEY = "colliersNativeObservations"


class PartialHistoryHold(ValueError):
    """A prior or incoming source observation cannot be preserved safely."""


def merge_native_observation(prior_raw, item, generation, attempt, started_at, finished_at):
    """Return a bounded member preserving each distinct source-native body.

    The caller must authenticate source/page bytes, bind the exact DB preimage,
    and preserve this reserved member across a later full ingest.
    """
    verified = verify_detail_record(item, generation, attempt, started_at, finished_at)
    external_id = verified["external_id"]
    if not isinstance(prior_raw, dict):
        raise PartialHistoryHold("prior raw_data is not an object")
    previous = prior_raw.get(NATIVE_HISTORY_KEY)
    if previous is None and NATIVE_HISTORY_KEY in prior_raw:
        raise PartialHistoryHold("reserved native history is unknown")
    if previous is None:
        previous = {"schema": "colliers-native-history/v1", "sourceKey": "colliers",
                    "externalId": external_id, "entries": []}
    if (not isinstance(previous, dict) or set(previous) != {"schema", "sourceKey", "externalId", "entries"}
            or previous["schema"] != "colliers-native-history/v1" or previous["sourceKey"] != "colliers"
            or previous["externalId"] != external_id or not isinstance(previous["entries"], list)):
        raise PartialHistoryHold("reserved native history is unknown")
    # Copy by value: never mutate the caller's DB snapshot.
    serialized_previous = json.dumps(previous, ensure_ascii=False, separators=(",", ":"))
    if len(serialized_previous.encode("utf-8")) > MAX_NATIVE_HISTORY_BYTES:
        raise PartialHistoryHold("native history exceeds bounded storage")
    history = json.loads(serialized_previous)
    entries, seen = history["entries"], set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"source", "nativeBase64", "lastObservedAt"}
                or not isinstance(entry["source"], dict)):
            raise PartialHistoryHold("prior native observation is unknown")
        source = entry["source"]
        expected_source = {"generation", "attempt", "start", "page", "pageSize", "offset",
                           "inventoryObservedAt", "detailObservedAt", "mapProjectId", "listSha256",
                           "mapSha256", "detailTokenSha256", "decodedDetailSha256", "nativeSha256"}
        hashes = ("listSha256", "mapSha256", "detailTokenSha256", "decodedDetailSha256", "nativeSha256")
        positions = ("start", "page", "pageSize", "offset")
        if (set(source) != expected_source or source["mapProjectId"] != external_id
                or any(not isinstance(source[key], str) or not _HASH.fullmatch(source[key]) for key in hashes)
                or source["nativeSha256"] in seen or not isinstance(source["generation"], str)
                or not source["generation"] or type(source["attempt"]) is not int or source["attempt"] < 1
                or any(type(source[key]) is not int or source[key] < (0 if key == "offset" else 1) for key in positions)
                or source["offset"] >= source["pageSize"]):
            raise PartialHistoryHold("prior native observation is unknown")
        try:
            encoded = entry["nativeBase64"]
            if not isinstance(encoded, str) or len(encoded) > ((MAX_NATIVE_HISTORY_BYTES + 2) // 3) * 4:
                raise ValueError("invalid native encoding")
            native = base64.b64decode(encoded, validate=True)
            decoded = native.decode("utf-8-sig")
            document = json.loads(decoded)
            summary = document.get("ProjectSummary") if isinstance(document, dict) else None
            visibility = summary.get("AttributeVisibility") if isinstance(summary, dict) else None
            observed = visibility.get("ProjectId") if isinstance(visibility, dict) else None
            if observed is None and isinstance(summary, dict):
                observed = summary.get("ProjectId")
            if (base64.b64encode(native).decode("ascii") != encoded
                    or hashlib.sha256(native).hexdigest() != source["nativeSha256"]
                    or hashlib.sha256(decoded.encode("utf-8")).hexdigest() != source["decodedDetailSha256"]
                    or observed is None or isinstance(observed, bool) or str(observed).strip() != external_id
                    or _time(source["inventoryObservedAt"]) > _time(source["detailObservedAt"])
                    or _time(entry["lastObservedAt"]) < _time(source["detailObservedAt"])):
                raise ValueError("invalid native binding")
        except (TypeError, ValueError, UnicodeDecodeError, binascii.Error, RecursionError) as exc:
            raise PartialHistoryHold("prior native observation is unknown") from exc
        seen.add(source["nativeSha256"])
    incoming_at = _time(item["source"]["detailObservedAt"])
    if any(incoming_at < _time(entry["lastObservedAt"]) for entry in entries):
        raise PartialHistoryHold("incoming native observation is stale")
    existing = next((entry for entry in entries if entry["source"]["nativeSha256"] == item["source"]["nativeSha256"]), None)
    if existing is None and any(incoming_at <= _time(entry["lastObservedAt"]) for entry in entries):
        raise PartialHistoryHold("incoming native observation has ambiguous ordering")
    if existing is None:
        entries.append({"source": item["source"], "nativeBase64": item["nativeBase64"],
                        "lastObservedAt": item["source"]["detailObservedAt"]})
    elif incoming_at > _time(existing["lastObservedAt"]):
        existing["lastObservedAt"] = item["source"]["detailObservedAt"]
    if len(json.dumps(history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_NATIVE_HISTORY_BYTES:
        raise PartialHistoryHold("native history exceeds bounded storage")
    return history
