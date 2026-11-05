"""Validate bounded Colliers partial detail observations before staging.

This module does not authorize writes: caller must bind the original page spool,
settled owner, artifact, code, target and both checkpoint gates.
"""
import base64
import binascii
import hashlib
import json
import math
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


def _native_project_id(value):
    # Match JavaScript Number.isSafeInteger/String across the replay boundary.
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if abs(value) > 9007199254740991:
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return str(int(value))


def verify_detail_record(item, generation, attempt, started_at, finished_at):
    """Validate one private native detail against its exact partial listing."""
    if (not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", generation)
            or not isinstance(item, dict) or set(item) != {"listing", "source", "decodedDetail", "nativeBase64"}):
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
    if (not isinstance(source_id, str) or re.search(r"[\x00-\x1f\x7f]", source_id)):
        raise ValueError("partial source identity invalid")
    try:
        source_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("partial source identity invalid") from exc
    provenance = listing.get("freshnessProvenance")
    proof = listing.get("colliersPartialEvidence")
    mapped_detail = listing.get("colliersSalesTrackerDetail")
    if (not isinstance(source_id, str) or not source_id or observed_id is None or isinstance(observed_id, bool)
            or _native_project_id(observed_id) != source_id
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
    if (len(tokens) != 1 or not tokens[0] or re.search(r"[\x00-\x1f\x7f]", tokens[0])
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
                    or _native_project_id(observed) != external_id
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

def _validate_staged(staged):
    if not isinstance(staged, list) or not staged:
        raise ValueError("partial staging is empty")
    expected = {"slug", "external_id", "source_url", "canonical_url", "transaction_type", "inventory_at", "detail_at"}
    ids, urls = set(), set()
    for row in staged:
        if (not isinstance(row, dict) or set(row) != expected or row["slug"] != "colliers"
                or row["transaction_type"] != "sale"
                or not isinstance(row["external_id"], str) or not row["external_id"] or row["external_id"].startswith("main:")
                or not isinstance(row["source_url"], str) or not row["source_url"]
                or (row["canonical_url"] is not None and
                    (not isinstance(row["canonical_url"], str) or not row["canonical_url"]))
                or row["external_id"] in ids or row["source_url"] in urls):
            raise ValueError("partial staging identity invalid")
        if _time(row["detail_at"]) < _time(row["inventory_at"]):
            raise ValueError("partial staging clock invalid")
        ids.add(row["external_id"])
        urls.add(row["source_url"])


def identity_probe_sql(staged):
    """Classify exact current-target conflicts; never infer missing source rows."""
    from cre_ingest import sql_lit

    _validate_staged(staged)
    payload = sql_lit(json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return f"""
WITH candidate AS (
 SELECT * FROM jsonb_to_recordset({payload}::jsonb)
 AS c(slug text, external_id text, source_url text, canonical_url text, transaction_type text,
      inventory_at timestamptz, detail_at timestamptz)
), scoped AS (
 SELECT b.id AS brokerage_id, c.* FROM candidate c
 JOIN credeals.cre_brokerages b ON b.slug=c.slug AND b.slug='colliers'
), conflicts AS (
 SELECT DISTINCT s.external_id FROM scoped s
 LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.external_id
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=s.brokerage_id AND si.external_id=s.external_id
 WHERE EXISTS (SELECT 1 FROM scoped peer WHERE peer.external_id<>s.external_id AND (
       (s.canonical_url IS NOT NULL AND (peer.canonical_url=s.canonical_url OR peer.source_url=s.canonical_url))
       OR (peer.canonical_url IS NOT NULL AND peer.canonical_url=s.source_url)))
    OR (own.id IS NOT NULL AND (own.source_url IS DISTINCT FROM s.source_url OR own.deleted_at IS NOT NULL
        OR (own.status IS DISTINCT FROM 'active' AND NOT (
          own.status IS NULL AND own.raw_data#>>'{{colliersNativeObservations,schema}}'='colliers-native-history/v1'
          AND own.raw_data#>>'{{colliersNativeObservations,sourceKey}}'='colliers'
          AND own.raw_data#>>'{{colliersNativeObservations,externalId}}'=s.external_id))
        OR own.scraped_at > s.detail_at OR own.updated_at > s.detail_at
        OR own.external_id LIKE 'main:%'
        OR NOT EXISTS (SELECT 1 FROM unnest(ARRAY[
          own.raw_data#>>'{{latestInventoryObservation,sourceKey}}', own.raw_data#>>'{{latestInventoryObservation,primary,sourceKey}}',
          own.raw_data#>>'{{latestInventoryObservation,secondary_pass,sourceKey}}', own.raw_data->>'sourceKey',
          own.raw_data#>>'{{primary,sourceKey}}', own.raw_data#>>'{{secondary_pass,sourceKey}}']) hint
          WHERE btrim(hint)='colliers')
        OR EXISTS (SELECT 1 FROM unnest(ARRAY[
          own.raw_data#>>'{{latestInventoryObservation,sourceKey}}', own.raw_data#>>'{{latestInventoryObservation,primary,sourceKey}}',
          own.raw_data#>>'{{latestInventoryObservation,secondary_pass,sourceKey}}', own.raw_data->>'sourceKey',
          own.raw_data#>>'{{primary,sourceKey}}', own.raw_data#>>'{{secondary_pass,sourceKey}}']) hint
          WHERE NULLIF(btrim(hint),'') IS NOT NULL AND btrim(hint)<>'colliers')))
    OR (si.id IS NOT NULL AND (si.source_key IS DISTINCT FROM 'colliers' OR si.url IS DISTINCT FROM s.source_url OR si.soft_deleted IS TRUE
        OR si.observation_present IS FALSE OR si.last_enumerated_at > s.inventory_at))
    OR EXISTS (SELECT 1 FROM credeals.cre_listings other
       WHERE other.brokerage_id=s.brokerage_id AND other.deleted_at IS NULL
         AND (own.id IS NULL OR other.id<>own.id)
         AND (other.source_url=s.source_url OR other.canonical_url=s.source_url
              OR (s.canonical_url IS NOT NULL AND (other.canonical_url=s.canonical_url OR other.source_url=s.canonical_url))))
)
SELECT (SELECT count(*) FROM candidate) AS staged, (SELECT count(*) FROM scoped) AS joined,
 COALESCE((SELECT json_agg(external_id ORDER BY external_id)::text FROM conflicts), '[]') AS held,
 COALESCE((SELECT jsonb_agg(jsonb_build_object('external_id',s.external_id,
   'listing', CASE WHEN own.id IS NULL THEN NULL ELSE jsonb_build_object('id',own.id,'source_url',own.source_url,
     'canonical_url',own.canonical_url,'status',own.status,'deleted_at',own.deleted_at,'scraped_at',own.scraped_at,'updated_at',own.updated_at,
     'raw_data',own.raw_data) END,
   'source_index', CASE WHEN si.id IS NULL THEN NULL ELSE jsonb_build_object('id',si.id,'source_key',si.source_key,'url',si.url,
     'soft_deleted',si.soft_deleted,'observation_present',si.observation_present,'last_enumerated_at',si.last_enumerated_at) END)
   ORDER BY s.external_id) FROM scoped s
   LEFT JOIN credeals.cre_listings own ON own.brokerage_id=s.brokerage_id AND own.external_id=s.external_id
   LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=s.brokerage_id AND si.external_id=s.external_id), '[]'::jsonb) AS preimage
"""


def transaction_guard_sql(staged, held, preimage, extra_held=()):
    """Recheck the partition under the existing ingest transaction and lock."""
    from cre_ingest import lifecycle_transaction_lock_sql, sql_lit

    _validate_staged(staged)
    ids = {row["external_id"] for row in staged}
    if not isinstance(held, list) or held != sorted(set(held)) or not set(held) <= ids:
        raise ValueError("partial held partition invalid")
    extra_held = set(extra_held)
    if not extra_held <= ids or extra_held & set(held):
        raise ValueError("partial extra held partition invalid")
    all_held = sorted(set(held) | extra_held)
    if len(all_held) == len(staged):
        raise ValueError("partial partition has no writable members")
    if not isinstance(preimage, list) or len(preimage) != len(staged):
        raise ValueError("partial preimage invalid")
    payload = sql_lit(json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    held_payload = sql_lit(json.dumps(held, separators=(",", ":")))
    preimage_payload = sql_lit(json.dumps(preimage, sort_keys=True, separators=(",", ":")))
    all_held_payload = sql_lit(json.dumps(all_held, separators=(",", ":")))
    return f"""
{lifecycle_transaction_lock_sql()}
CREATE TEMP TABLE _colliers_partial_members ON COMMIT DROP AS
 SELECT b.id AS brokerage_id, m.* FROM jsonb_to_recordset({payload}::jsonb)
 AS m(slug text, external_id text, source_url text, canonical_url text, transaction_type text,
      inventory_at timestamptz, detail_at timestamptz)
 JOIN credeals.cre_brokerages b ON b.slug=m.slug AND b.slug='colliers';
DO $$ BEGIN
 PERFORM si.id FROM credeals.cre_source_index si JOIN _colliers_partial_members m
  ON si.brokerage_id=m.brokerage_id AND si.external_id=m.external_id
  ORDER BY si.id FOR UPDATE OF si;
 PERFORM own.id FROM credeals.cre_listings own JOIN _colliers_partial_members m
  ON own.brokerage_id=m.brokerage_id AND own.external_id=m.external_id
  ORDER BY own.id FOR UPDATE OF own;
END $$;
CREATE TEMP TABLE _colliers_partial_probe ON COMMIT DROP AS {identity_probe_sql(staged)};
DO $$ BEGIN
 IF (SELECT count(*) FROM _colliers_partial_members) <> {len(staged)}
 OR (SELECT count(*) FROM _src) <> {len(staged)}
 OR (SELECT count(DISTINCT (brokerage_id, external_id)) FROM _src) <> {len(staged)}
 OR (SELECT count(*) FROM _src s JOIN _colliers_partial_members m
     ON s.brokerage_id=m.brokerage_id AND s.external_id=m.external_id
     AND s.source_url=m.source_url AND s.canonical_url IS NOT DISTINCT FROM m.canonical_url
     AND s.transaction_type=m.transaction_type WHERE s.raw_data->>'sourceKey'='colliers'
     AND s.raw_data->>'id'=m.external_id
     AND (s.raw_data->>'inventoryObservedAt')::timestamptz=m.inventory_at
     AND (s.raw_data->>'detailObservedAt')::timestamptz=m.detail_at) <> {len(staged)}
 OR (SELECT staged FROM _colliers_partial_probe) <> {len(staged)}
 OR (SELECT joined FROM _colliers_partial_probe) <> {len(staged)}
 OR (SELECT held::jsonb FROM _colliers_partial_probe) <> {held_payload}::jsonb
 OR (SELECT preimage FROM _colliers_partial_probe) <> {preimage_payload}::jsonb
 THEN RAISE EXCEPTION 'Colliers partial identity partition changed'; END IF;
END $$;
DELETE FROM _src s USING _colliers_partial_members m WHERE s.brokerage_id=m.brokerage_id
 AND s.external_id=m.external_id AND m.external_id IN (SELECT jsonb_array_elements_text({all_held_payload}::jsonb));
UPDATE credeals.cre_scrape_jobs j SET status='partial',
 listings_saved=(SELECT count(*) FROM _src s WHERE s.brokerage_id=j.brokerage_id)
 FROM _jobmeta jm WHERE jm.slug='colliers' AND j.id=jm.job_id;
"""


def build_partial_sql(items, staged, held, preimage, generation, attempt, started_at,
                      finished_at, artifact_finished_at, job_id, *, activate_status=False):
    """Build one guarded partial transaction; caller owns source, gates and target."""
    import uuid

    from cre_ingest import _status_from_signal, sql_lit, to_row

    _validate_staged(staged)
    if type(activate_status) is not bool:
        raise ValueError("invalid status activation")
    job = str(uuid.UUID(job_id))
    if not isinstance(items, list) or len(items) != len(staged):
        raise ValueError("partial artifact partition differs")
    expected = {row["external_id"]: row for row in staged}
    if not isinstance(preimage, list) or len(preimage) != len(staged):
        raise ValueError("partial preimage differs")
    prior = {row["external_id"]: row for row in preimage if isinstance(row, dict) and "external_id" in row}
    if set(prior) != set(expected):
        raise ValueError("partial preimage differs")
    rows, history_held, seen = [], {}, set()
    for item in items:
        member = stage_detail_record(item, generation, attempt, started_at, finished_at, artifact_finished_at)
        external_id = member["external_id"]
        if external_id in seen or expected.get(external_id) != member:
            raise ValueError("partial staged membership differs")
        seen.add(external_id)
        row = to_row(item["listing"], {}, artifact_finished_at)
        parent = prior[external_id].get("listing")
        raw_before = {} if parent is None else parent.get("raw_data")
        history = None
        if external_id not in held:
            try:
                history = merge_native_observation(raw_before, item, generation, attempt, started_at, finished_at)
            except PartialHistoryHold as exc:
                history_held[external_id] = str(exc)
        # Only the already-supported status gate may allow an explicit source signal.
        explicit_status = _status_from_signal(item["listing"].get("status") or item["listing"].get("statusBadge"))
        projection = {key: row.get(key) for key in
                      ("slug", "external_id", "source_url", "canonical_url", "transaction_type", "title",
                       "address", "city", "state", "zip", "description", "size_sf", "lot_size_sf", "units",
                       "scraped_at")}
        # PostgreSQL text/jsonb cannot represent NUL or invalid UTF-8. Keep
        # source bytes in native history and omit only an unsafe scalar.
        for key in ("title", "address", "city", "state", "zip", "description"):
            value = projection[key]
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    projection[key] = None
                if "\x00" in value:
                    projection[key] = None
        for key in ("size_sf", "lot_size_sf", "units"):
            if isinstance(projection[key], float) and not math.isfinite(projection[key]):
                projection[key] = None
        units = projection["units"]
        if units is not None:
            if isinstance(units, bool) or not isinstance(units, (int, float)) or not float(units).is_integer() or not 0 < units <= 2147483647:
                projection["units"] = None
            else:
                projection["units"] = int(units)
        projection["raw_data"] = {"sourceKey": "colliers", "id": external_id,
                                  "inventoryObservedAt": item["source"]["inventoryObservedAt"],
                                  "detailObservedAt": item["source"]["detailObservedAt"]}
        rows.append(projection | {"partial_history": history,
                                  "status": explicit_status if activate_status else None})
    if seen != set(expected):
        raise ValueError("partial staged membership differs")
    payload = sql_lit(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    guard = transaction_guard_sql(staged, held, preimage, history_held)
    # Existing source-index and child rows are untouched. Raw bytes live under a
    # reserved base64 member so generic recursive extraction flags cannot scan
    # their contents. Caller must ensure later full refresh preserves it.
    sql = f"""
BEGIN;
CREATE TEMP TABLE _jobmeta (slug text, job_id uuid) ON COMMIT DROP;
INSERT INTO _jobmeta VALUES ('colliers', {sql_lit(job)}::uuid);
INSERT INTO credeals.cre_scrape_jobs (id,brokerage_id,status,started_at,completed_at,listings_discovered,listings_scraped,listings_saved,errors_count,notes)
SELECT {sql_lit(job)}::uuid,b.id,'partial',{sql_lit(started_at)}::timestamptz,{sql_lit(artifact_finished_at)}::timestamptz,
 {len(rows)},{len(rows)},{len(rows)},1,'Colliers source failure: bounded partial detail evidence'
FROM credeals.cre_brokerages b WHERE b.slug='colliers';
CREATE TEMP TABLE _src ON COMMIT DROP AS
SELECT b.id AS brokerage_id, r.* FROM jsonb_to_recordset({payload}::jsonb)
AS r(slug text,external_id text,source_url text,canonical_url text,transaction_type text,title text,
 address text,city text,state text,zip text,description text,size_sf numeric,lot_size_sf numeric,units integer,
 raw_data jsonb,scraped_at timestamptz,partial_history jsonb,status text)
JOIN credeals.cre_brokerages b ON b.slug=r.slug AND b.slug='colliers';
{guard}
CREATE TEMP TABLE _colliers_location ON COMMIT DROP AS
SELECT t.id,t.brokerage_id,t.external_id,
 ((t.address IS NULL OR s.address IS NULL OR lower(btrim(t.address))=lower(btrim(s.address)))
  AND (t.city IS NULL OR s.city IS NULL OR lower(btrim(t.city))=lower(btrim(s.city)))
  AND (t.state IS NULL OR s.state IS NULL OR lower(btrim(t.state))=lower(btrim(s.state)))
  AND (t.zip IS NULL OR s.zip IS NULL OR lower(btrim(t.zip))=lower(btrim(s.zip)))) AS compatible
FROM credeals.cre_listings t JOIN _src s ON t.brokerage_id=s.brokerage_id AND t.external_id=s.external_id;
UPDATE credeals.cre_listings t SET
 title=COALESCE(t.title,s.title),
 address=CASE WHEN loc.compatible
              THEN COALESCE(t.address,s.address) ELSE t.address END,
 city=CASE WHEN loc.compatible
              THEN COALESCE(t.city,s.city) ELSE t.city END,
 state=CASE WHEN loc.compatible
              THEN COALESCE(t.state,s.state) ELSE t.state END,
 zip=CASE WHEN loc.compatible
              THEN COALESCE(t.zip,s.zip) ELSE t.zip END,
 description=COALESCE(t.description,s.description),size_sf=COALESCE(t.size_sf,s.size_sf),
 lot_size_sf=COALESCE(t.lot_size_sf,s.lot_size_sf),units=COALESCE(t.units,s.units),
 raw_data=t.raw_data || jsonb_build_object('colliersNativeObservations',s.partial_history)
FROM _src s JOIN _colliers_location loc ON loc.brokerage_id=s.brokerage_id AND loc.external_id=s.external_id
WHERE t.id=loc.id;
INSERT INTO credeals.cre_listings
 (brokerage_id,external_id,source_url,canonical_url,status,transaction_type,title,address,city,state,zip,description,
  size_sf,lot_size_sf,units,scraped_at,raw_data)
SELECT s.brokerage_id,s.external_id,s.source_url,s.canonical_url,s.status,s.transaction_type,s.title,s.address,s.city,s.state,s.zip,
 s.description,s.size_sf,s.lot_size_sf,s.units,s.scraped_at,
 jsonb_build_object('sourceKey','colliers','id',s.external_id,'colliersNativeObservations',s.partial_history)
FROM _src s WHERE NOT EXISTS (SELECT 1 FROM credeals.cre_listings t
 WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.external_id);
COMMIT;
"""
    return sql, history_held


def validate_window_artifact(data, source_receipt, evidence_receipt):
    """Bind a trusted settled worker result to its exact source and target.

    Caller must authenticate the manifest/lock, child settlement and artifact
    bytes; this function does not independently replay either sidecar.
    """
    keys = {"schema", "sourceKey", "binding", "detailFinishedAt", "producerSha", "targetSha256",
            "source", "sourceOutcome", "coverage", "cursor", "proposedCursor", "selected", "total",
            "heldOther", "unattempted", "evidence", "listings", "held"}
    if not isinstance(data, dict) or set(data) != keys or not isinstance(source_receipt, dict):
        raise ValueError("partial worker artifact shape invalid")
    binding = {"generation": source_receipt.get("generation"), "attempt": source_receipt.get("attempt"),
               "startedAt": source_receipt.get("started_at"), "finishedAt": source_receipt.get("finished_at")}
    expected_source = {"bytes": source_receipt.get("bytes"), "sha256": source_receipt.get("sha256"),
                       "mainSha256": source_receipt.get("main_sha256")}
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
    if not (_time(binding["finishedAt"]) <= _time(data["detailFinishedAt"]) <= datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("partial worker finish invalid")
    staged = [stage_detail_record(item, binding["generation"], binding["attempt"], binding["startedAt"],
                                   binding["finishedAt"], data["detailFinishedAt"]) for item in data["listings"]]
    if staged:
        _validate_staged(staged)
    return staged
