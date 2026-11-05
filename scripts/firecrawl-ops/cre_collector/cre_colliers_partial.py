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
    # Source and detail stamps come from different monotonic-anchored
    # processes.  Their actual ordering is established by sealed-source
    # custody and the owned worker, not by comparing their wall clocks.
    if not (_time(started_at) <= _time(source["inventoryObservedAt"]) <= _time(finished_at)
            and _time(source["detailObservedAt"]) >= _time(started_at)):
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


class PartialHistoryHold(ValueError):
    """A prior or incoming source observation cannot be preserved safely."""


def native_observation(item, generation, attempt, started_at, finished_at):
    """Prepare one authenticated observation; old database history stays in PostgreSQL."""
    verify_detail_record(item, generation, attempt, started_at, finished_at)
    return {"source": item["source"], "nativeBase64": item["nativeBase64"],
            "lastObservedAt": item["source"]["detailObservedAt"]}

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
 SELECT x.ordinality AS input_order, c.* FROM jsonb_array_elements({payload}::jsonb) WITH ORDINALITY AS x(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(x.value)
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
     'title',own.title,'address',own.address,'city',own.city,'state',own.state,'zip',own.zip,
     'description',own.description,'size_sf',own.size_sf,'lot_size_sf',own.lot_size_sf,'units',own.units,
     'parent_sha256',encode(sha256(convert_to(to_jsonb(own)::text,'UTF8')),'hex'),
     'parent_static_sha256',encode(sha256(convert_to((to_jsonb(own) - ARRAY['updated_at','raw_data','title','address','city','state','zip','description','size_sf','lot_size_sf','units'])::text,'UTF8')),'hex'),
     'raw_data_sha256',encode(sha256(convert_to(COALESCE(own.raw_data::text,''),'UTF8')),'hex'),
     'raw_data_is_object',jsonb_typeof(own.raw_data)='object',
     'native_history_present',COALESCE(own.raw_data ? 'colliersNativeObservations',false),
     'native_history_valid',CASE WHEN NOT COALESCE(own.raw_data ? 'colliersNativeObservations',false) THEN true
       ELSE COALESCE(jsonb_typeof(own.raw_data->'colliersNativeObservations')='object'
         AND ((own.raw_data->'colliersNativeObservations') - 'schema' - 'sourceKey' - 'externalId' - 'entries')='{{}}'::jsonb
         AND own.raw_data#>>'{{colliersNativeObservations,schema}}'='colliers-native-history/v1'
         AND own.raw_data#>>'{{colliersNativeObservations,sourceKey}}'='colliers'
         AND own.raw_data#>>'{{colliersNativeObservations,externalId}}'=s.external_id
         AND jsonb_typeof(own.raw_data#>'{{colliersNativeObservations,entries}}')='array', false) END) END,
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
    if not isinstance(preimage, list) or len(preimage) != len(staged):
        raise TypeError("partial preimage invalid")
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


def prepare_partial_rows(items, staged, held, preimage, generation, attempt, started_at,
                         finished_at, artifact_finished_at, *, activate_status=False):
    """Build the exact writable/held partition from authenticated inputs."""
    from cre_ingest import _status_from_signal, to_row

    _validate_staged(staged)
    if type(activate_status) is not bool:
        raise ValueError("invalid status activation")
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
        observation = None
        if external_id not in held:
            try:
                if parent is not None and parent.get("raw_data_is_object") is not True:
                    raise PartialHistoryHold("prior raw_data is unknown")
                if parent is not None and parent.get("native_history_valid") is not True:
                    raise PartialHistoryHold("reserved native history is unknown")
                observation = native_observation(item, generation, attempt, started_at, finished_at)
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
        rows.append(projection | {"partial_observation": observation,
                                  "status": explicit_status if activate_status else None})
    if seen != set(expected):
        raise ValueError("partial staged membership differs")
    return rows, history_held


def build_partial_sql(items, staged, held, preimage, generation, attempt, started_at,
                      finished_at, artifact_finished_at, job_id, *, activate_status=False):
    """Build one guarded partial transaction; caller owns source, gates and target."""
    import uuid

    from cre_ingest import sql_lit

    job = str(uuid.UUID(job_id))
    rows, history_held = prepare_partial_rows(items, staged, held, preimage, generation, attempt,
                                              started_at, finished_at, artifact_finished_at,
                                              activate_status=activate_status)
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
 raw_data jsonb,scraped_at timestamptz,partial_observation jsonb,status text)
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
 raw_data=CASE WHEN t.raw_data ? 'colliersNativeObservations' AND EXISTS (
   SELECT 1 FROM jsonb_array_elements(t.raw_data#>'{{colliersNativeObservations,entries}}') e
   WHERE e=s.partial_observation)
   THEN t.raw_data ELSE jsonb_set(t.raw_data,'{{colliersNativeObservations}}',
   CASE WHEN t.raw_data ? 'colliersNativeObservations'
     THEN jsonb_set(t.raw_data->'colliersNativeObservations','{{entries}}',
       (t.raw_data#>'{{colliersNativeObservations,entries}}') || jsonb_build_array(s.partial_observation))
     ELSE jsonb_build_object('schema','colliers-native-history/v1','sourceKey','colliers',
       'externalId',s.external_id,'entries',jsonb_build_array(s.partial_observation)) END) END
FROM _src s JOIN _colliers_location loc ON loc.brokerage_id=s.brokerage_id AND loc.external_id=s.external_id
WHERE t.id=loc.id;
INSERT INTO credeals.cre_listings
 (brokerage_id,external_id,source_url,canonical_url,status,transaction_type,title,address,city,state,zip,description,
  size_sf,lot_size_sf,units,scraped_at,raw_data)
SELECT s.brokerage_id,s.external_id,s.source_url,s.canonical_url,s.status,s.transaction_type,s.title,s.address,s.city,s.state,s.zip,
 s.description,s.size_sf,s.lot_size_sf,s.units,s.scraped_at,
 jsonb_build_object('sourceKey','colliers','id',s.external_id,'colliersNativeObservations',
   jsonb_build_object('schema','colliers-native-history/v1','sourceKey','colliers',
     'externalId',s.external_id,'entries',jsonb_build_array(s.partial_observation)))
FROM _src s WHERE NOT EXISTS (SELECT 1 FROM credeals.cre_listings t
 WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.external_id);
COMMIT;
"""
    return sql, history_held


def partial_readback_sql(items, staged, held, preimage, generation, attempt, started_at,
                         finished_at, artifact_finished_at, job_id):
    """Check the committed job and exact raw/lifecycle postconditions, read only."""
    import uuid

    from cre_ingest import sql_lit

    job = str(uuid.UUID(job_id))
    rows, history_held = prepare_partial_rows(items, staged, held, preimage, generation, attempt,
                                              started_at, finished_at, artifact_finished_at)
    prior = {item["external_id"]: item for item in preimage}
    held_ids = set(held) | set(history_held)
    expected = [{"row": row, "before": prior[row["external_id"]]} for row in rows if row["external_id"] not in held_ids]
    held_expected = [prior[external_id] for external_id in sorted(held_ids)]
    payload = sql_lit(json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    held_payload = sql_lit(json.dumps(held_expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    return f"""
WITH expected AS (SELECT value->'row' AS r,value->'before' AS p FROM jsonb_array_elements({payload}::jsonb)),
observed AS (
 SELECT e.*, t.id AS listing_id, t.source_url,t.canonical_url,t.status,t.deleted_at,t.scraped_at,t.raw_data,
   t.title,t.address,t.city,t.state,t.zip,t.description,t.size_sf,t.lot_size_sf,t.units,
   encode(sha256(convert_to((to_jsonb(t) - ARRAY['updated_at','raw_data','title','address','city','state','zip','description','size_sf','lot_size_sf','units'])::text,'UTF8')),'hex') AS parent_static_sha256,
   ((e.p#>>'{{listing,address}}' IS NULL OR e.r->>'address' IS NULL OR lower(btrim(e.p#>>'{{listing,address}}'))=lower(btrim(e.r->>'address')))
    AND (e.p#>>'{{listing,city}}' IS NULL OR e.r->>'city' IS NULL OR lower(btrim(e.p#>>'{{listing,city}}'))=lower(btrim(e.r->>'city')))
    AND (e.p#>>'{{listing,state}}' IS NULL OR e.r->>'state' IS NULL OR lower(btrim(e.p#>>'{{listing,state}}'))=lower(btrim(e.r->>'state')))
    AND (e.p#>>'{{listing,zip}}' IS NULL OR e.r->>'zip' IS NULL OR lower(btrim(e.p#>>'{{listing,zip}}'))=lower(btrim(e.r->>'zip')))) AS location_compatible,
   COALESCE(e.p->'listing' <> 'null'::jsonb AND (e.p#>>'{{listing,native_history_present}}')::boolean
     AND encode(sha256(convert_to(t.raw_data::text,'UTF8')),'hex')=e.p#>>'{{listing,raw_data_sha256}}'
     AND EXISTS (SELECT 1 FROM jsonb_array_elements(t.raw_data#>'{{colliersNativeObservations,entries}}') prior
       WHERE prior=e.r->'partial_observation'),false) AS retained_identical,
   CASE WHEN si.id IS NULL THEN NULL ELSE jsonb_build_object('id',si.id,'source_key',si.source_key,'url',si.url,
     'soft_deleted',si.soft_deleted,'observation_present',si.observation_present,'last_enumerated_at',si.last_enumerated_at) END AS source_index
 FROM expected e JOIN credeals.cre_brokerages b ON b.slug='colliers'
 LEFT JOIN credeals.cre_listings t ON t.brokerage_id=b.id AND t.external_id=e.r->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=b.id AND si.external_id=e.r->>'external_id'
), mismatches AS (
 SELECT 1 FROM observed WHERE listing_id IS NULL OR source_url IS DISTINCT FROM r->>'source_url'
 OR raw_data#>>'{{colliersNativeObservations,schema}}' IS DISTINCT FROM 'colliers-native-history/v1'
 OR raw_data#>>'{{colliersNativeObservations,sourceKey}}' IS DISTINCT FROM 'colliers'
 OR raw_data#>>'{{colliersNativeObservations,externalId}}' IS DISTINCT FROM r->>'external_id'
 OR (NOT retained_identical AND (raw_data#>'{{colliersNativeObservations,entries}}')->-1 IS DISTINCT FROM r->'partial_observation')
 OR source_index IS DISTINCT FROM NULLIF(p->'source_index','null'::jsonb)
 OR to_jsonb(title) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,title}}','null'::jsonb),NULLIF(r->'title','null'::jsonb))
 OR to_jsonb(description) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,description}}','null'::jsonb),NULLIF(r->'description','null'::jsonb))
 OR to_jsonb(size_sf) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,size_sf}}','null'::jsonb),NULLIF(r->'size_sf','null'::jsonb))
 OR to_jsonb(lot_size_sf) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,lot_size_sf}}','null'::jsonb),NULLIF(r->'lot_size_sf','null'::jsonb))
 OR to_jsonb(units) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,units}}','null'::jsonb),NULLIF(r->'units','null'::jsonb))
 OR to_jsonb(address) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,address}}','null'::jsonb),CASE WHEN location_compatible THEN NULLIF(r->'address','null'::jsonb) END)
 OR to_jsonb(city) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,city}}','null'::jsonb),CASE WHEN location_compatible THEN NULLIF(r->'city','null'::jsonb) END)
 OR to_jsonb(state) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,state}}','null'::jsonb),CASE WHEN location_compatible THEN NULLIF(r->'state','null'::jsonb) END)
 OR to_jsonb(zip) IS DISTINCT FROM COALESCE(NULLIF(p#>'{{listing,zip}}','null'::jsonb),CASE WHEN location_compatible THEN NULLIF(r->'zip','null'::jsonb) END)
 OR (p->'listing' = 'null'::jsonb AND (status IS NOT NULL OR deleted_at IS NOT NULL
     OR canonical_url IS DISTINCT FROM r->>'canonical_url' OR scraped_at IS DISTINCT FROM (r->>'scraped_at')::timestamptz))
 OR (p->'listing' <> 'null'::jsonb AND (
     listing_id::text IS DISTINCT FROM p#>>'{{listing,id}}'
     OR parent_static_sha256 IS DISTINCT FROM p#>>'{{listing,parent_static_sha256}}'
     OR canonical_url IS DISTINCT FROM p#>>'{{listing,canonical_url}}'
     OR status IS DISTINCT FROM p#>>'{{listing,status}}'
     OR to_jsonb(deleted_at) IS DISTINCT FROM NULLIF(p#>'{{listing,deleted_at}}','null'::jsonb)
     OR to_jsonb(scraped_at) IS DISTINCT FROM NULLIF(p#>'{{listing,scraped_at}}','null'::jsonb)
     OR (NOT retained_identical AND encode(sha256(convert_to((CASE WHEN (p#>>'{{listing,native_history_present}}')::boolean
          THEN jsonb_set(raw_data,'{{colliersNativeObservations,entries}}',
             (raw_data#>'{{colliersNativeObservations,entries}}') - (jsonb_array_length(raw_data#>'{{colliersNativeObservations,entries}}') - 1))
          ELSE raw_data - 'colliersNativeObservations' END)::text,'UTF8')),'hex')
        IS DISTINCT FROM p#>>'{{listing,raw_data_sha256}}')))
), held_expected AS (SELECT value AS p FROM jsonb_array_elements({held_payload}::jsonb)), held_mismatches AS (
 SELECT 1 FROM held_expected h JOIN credeals.cre_brokerages b ON b.slug='colliers'
 LEFT JOIN credeals.cre_listings t ON t.brokerage_id=b.id AND t.external_id=h.p->>'external_id'
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=b.id AND si.external_id=h.p->>'external_id'
 WHERE (h.p->'listing'='null'::jsonb AND t.id IS NOT NULL)
 OR (h.p->'listing'<>'null'::jsonb AND (t.id IS NULL OR encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') IS DISTINCT FROM h.p#>>'{{listing,parent_sha256}}'))
 OR (CASE WHEN si.id IS NULL THEN NULL ELSE jsonb_build_object('id',si.id,'source_key',si.source_key,'url',si.url,
     'soft_deleted',si.soft_deleted,'observation_present',si.observation_present,'last_enumerated_at',si.last_enumerated_at) END)
    IS DISTINCT FROM NULLIF(h.p->'source_index','null'::jsonb)
)
SELECT (SELECT count(*) FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b ON b.id=j.brokerage_id
 WHERE b.slug='colliers' AND j.id={sql_lit(job)}::uuid AND j.status='partial'
 AND j.listings_discovered={len(rows)} AND j.listings_scraped={len(rows)} AND j.listings_saved={len(expected)}
 AND j.errors_count=1 AND j.started_at={sql_lit(started_at)}::timestamptz
 AND j.completed_at={sql_lit(artifact_finished_at)}::timestamptz) AS jobs,
 (SELECT count(*) FROM observed) AS observed,
 ((SELECT count(*) FROM mismatches)+(SELECT count(*) FROM held_mismatches)) AS mismatches
""", history_held
