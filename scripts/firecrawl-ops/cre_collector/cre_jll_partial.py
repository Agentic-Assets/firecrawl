"""Verify JLL Investor partial artifact membership without claiming feed coverage."""

import re
from urllib.parse import urlsplit

from cre_ingest import jll_structured_child_preservation_is_valid, to_row


def verified_members(data, generation_id):
    """Return observed members; omitted candidates remain unknown.

    The caller must separately enforce artifact custody, freshness, and both
    checkpoint gates. Neither the provider's count nor this check asserts that
    an omitted listing disappeared.
    """
    if not isinstance(data, dict) or not isinstance(generation_id, str) or not generation_id:
        raise ValueError("JLL partial generation missing")
    rows = data.get("listings")
    entries = data.get("sources")
    if not isinstance(rows, list) or not rows or not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("JLL partial artifact shape invalid")
    indexed = {entry.get("transaction"): entry for entry in entries if isinstance(entry, dict)}
    if set(indexed) != {"sale", "lease"} or len(indexed) != len(entries):
        raise ValueError("JLL partial source scope invalid")
    sale, lease = indexed["sale"], indexed["lease"]
    if any(entry.get("sourceKey") != "jll-investor" or entry.get("supported") is not True
           or entry.get("error") for entry in entries):
        raise ValueError("JLL partial source failed")
    if sale.get("truncated") is not True or lease.get("truncated") is not False:
        raise ValueError("JLL partial truncation differs")
    if (type(sale.get("listingsCollected")) is not int or sale["listingsCollected"] != len(rows)
            or type(lease.get("listingsCollected")) is not int or lease["listingsCollected"] != 0
            or type(data.get("totalListings")) is not int or data["totalListings"] != len(rows)):
        raise ValueError("JLL partial count differs")
    members = []
    ids, urls = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("JLL partial row invalid")
        search = row.get("jllInvestorSearchRow")
        detail = row.get("jllInvestorDetail")
        provenance = row.get("freshnessProvenance")
        native_id = row.get("id")
        if (not isinstance(search, dict) or not isinstance(detail, dict) or not isinstance(provenance, dict)
                or not isinstance(native_id, str) or not re.fullmatch(r"006[A-Za-z0-9]{15}", native_id)
                or search.get("id") != native_id or detail.get("id") != native_id
                or row.get("sourceKey") != "jll-investor" or row.get("transactionMode") != "sale"
                or "latestJllPartialObservation" in row
                or row.get("transactionType") != "Sale (investment)"
                or row.get("jllInvestorUsSearchMembership") is not True
                or provenance.get("generationId") != generation_id
                or provenance.get("identityMethod") != "provider_listing_id"
                or not jll_structured_child_preservation_is_valid(row)):
            raise ValueError("JLL partial identity or detail proof invalid")
        alias = search.get("alias")
        if not isinstance(alias, str) or not alias or alias != alias.strip() or detail.get("alias") != alias:
            raise ValueError("JLL partial alias differs")
        if alias.startswith("/us/en/listings/"):
            expected_url = "https://invest.jll.com" + alias
        elif alias.lower().startswith(("http://", "https://")):
            expected_url = alias
        else:
            expected_url = "https://invest.jll.com/us/en/listings/" + alias.lstrip("/")
        parsed = urlsplit(expected_url)
        if (parsed.scheme != "https" or parsed.netloc != "invest.jll.com"
                or not parsed.path.startswith("/us/en/listings/") or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or row.get("url") != expected_url or row.get("canonicalUrl") != expected_url
                or native_id in ids or expected_url in urls):
            raise ValueError("JLL partial URL or unique identity invalid")
        ids.add(native_id)
        urls.add(expected_url)
        members.append({"native_id": native_id, "external_id": "investor:" + native_id,
                        "source_url": expected_url})
    return sorted(members, key=lambda item: item["native_id"])


def stage_members(data, generation_id):
    """Bind the producer's rows to the actual canonical staging mapper."""
    members = verified_members(data, generation_id)
    expected = {item["native_id"]: item for item in members}
    brokers = {i: value for i, value in enumerate(data.get("brokers") or [])}
    finished = (data.get("runMeta") or {}).get("finishedAt")
    staged = []
    for listing in data["listings"]:
        row = to_row(listing, brokers, finished)
        member = expected[listing["id"]]
        if (not isinstance(row, dict) or row.get("slug") != "jll"
                or row.get("external_id") != member["external_id"]
                or row.get("source_url") != member["source_url"]
                or row.get("canonical_url") != member["source_url"]
                or row.get("transaction_type") != "sale"):
            raise ValueError("JLL partial staging differs from verified source identity")
        staged.append({key: row.get(key) for key in
                       ("slug", "external_id", "source_url", "canonical_url", "transaction_type")})
    return sorted(staged, key=lambda row: row["external_id"])


def identity_probe_sql(staged):
    """Classify new URL conflicts; unchanged historical pairs are not new conflicts."""
    import json

    from cre_ingest import sql_lit

    payload = sql_lit(json.dumps(staged, sort_keys=True, separators=(",", ":")))
    return f"""
WITH candidate AS (
  SELECT * FROM jsonb_to_recordset({payload}::jsonb)
    AS s(slug text, external_id text, source_url text, canonical_url text, transaction_type text)
), scoped AS (
  SELECT b.id AS brokerage_id, c.*
  FROM candidate c JOIN credeals.cre_brokerages b ON b.slug = c.slug AND b.slug = 'jll'
), conflicts AS (
  SELECT DISTINCT s.external_id
  FROM scoped s
  LEFT JOIN credeals.cre_listings own
    ON own.brokerage_id = s.brokerage_id AND own.external_id = s.external_id
  WHERE NOT (own.id IS NOT NULL AND
             (own.deleted_at IS NOT NULL OR own.source_url IS NOT DISTINCT FROM s.source_url))
    AND EXISTS (
      SELECT 1 FROM credeals.cre_listings other
      WHERE other.brokerage_id = s.brokerage_id
        AND other.deleted_at IS NULL
        AND other.source_url = s.source_url
        AND (own.id IS NULL OR other.id <> own.id)
    )
)
SELECT (SELECT count(*) FROM candidate) AS staged,
       (SELECT count(*) FROM scoped) AS joined,
       COALESCE((SELECT json_agg(external_id ORDER BY external_id)::text FROM conflicts), '[]') AS held
"""


def transaction_guard_sql(staged, held):
    """Recheck the sealed partition before writing; retain held rows in the artifact."""
    import json

    from cre_ingest import sql_lit

    identities = {row["external_id"] for row in staged}
    if (not isinstance(held, list) or held != sorted(set(held))
            or not set(held) <= identities or len(identities) != len(staged)):
        raise ValueError("JLL partial held partition invalid")
    payload = sql_lit(json.dumps(staged, sort_keys=True, separators=(",", ":")))
    held_payload = sql_lit(json.dumps(held, separators=(",", ":")))
    return f"""
CREATE TEMP TABLE _jll_partial_members ON COMMIT DROP AS
SELECT b.id AS brokerage_id, m.*
FROM jsonb_to_recordset({payload}::jsonb)
 AS m(slug text, external_id text, source_url text, canonical_url text, transaction_type text)
JOIN credeals.cre_brokerages b ON b.slug = m.slug AND b.slug = 'jll';
CREATE TEMP TABLE _jll_partial_probe ON COMMIT DROP AS
{identity_probe_sql(staged)};
DO $$
BEGIN
 IF (SELECT count(*) FROM _jll_partial_members) <> {len(staged)}
 OR (SELECT count(*) FROM _src) <> {len(staged)}
 OR (SELECT count(*) FROM _src s JOIN _jll_partial_members m
       ON m.brokerage_id=s.brokerage_id AND m.external_id=s.external_id
       AND m.source_url=s.source_url AND m.canonical_url=s.canonical_url
       AND m.transaction_type=s.transaction_type
       WHERE s.raw_data->>'sourceKey' = 'jll-investor') <> {len(staged)}
 OR (SELECT staged FROM _jll_partial_probe) <> {len(staged)}
 OR (SELECT joined FROM _jll_partial_probe) <> {len(staged)}
 OR (SELECT held::jsonb FROM _jll_partial_probe) <> {held_payload}::jsonb
 THEN RAISE EXCEPTION 'JLL partial identity partition changed'; END IF;
END $$;
DELETE FROM _src s USING _jll_partial_members m
 WHERE s.brokerage_id=m.brokerage_id AND s.external_id=m.external_id
   AND m.external_id IN (SELECT jsonb_array_elements_text({held_payload}::jsonb));
UPDATE credeals.cre_scrape_jobs j
 SET status='partial', listings_saved=(SELECT count(*) FROM _src s WHERE s.brokerage_id=j.brokerage_id)
 FROM _jobmeta jm WHERE jm.slug='jll' AND j.id=jm.job_id;
"""


def readback_sql(staged, held, generation_id, raw_by_external_id):
    """Read back the exact emitted identities, including parents left deleted."""
    import json

    from cre_ingest import sql_lit

    held_set = set(held)
    emitted = [row for row in staged if row["external_id"] not in held_set]
    if (not emitted or len(emitted) + len(held_set) != len(staged)
            or set(raw_by_external_id) != {row["external_id"] for row in staged}):
        raise ValueError("JLL partial readback partition invalid")
    expected = [dict(row, expected_raw=raw_by_external_id[row["external_id"]]) for row in emitted]
    payload = sql_lit(json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    generation = sql_lit(generation_id)
    return f"""
WITH expected AS (
  SELECT * FROM jsonb_to_recordset({payload}::jsonb)
    AS m(slug text, external_id text, source_url text, canonical_url text, transaction_type text, expected_raw jsonb)
), observed AS (
  SELECT e.external_id, e.source_url, e.expected_raw, l.id, l.source_url AS stored_url,
         CASE WHEN l.raw_data->'latestJllPartialObservation'->>'sourceKey' = 'jll-investor'
              THEN l.raw_data->'latestJllPartialObservation' ELSE l.raw_data END AS payload
  FROM expected e
  LEFT JOIN credeals.cre_brokerages b ON b.slug = e.slug AND b.slug = 'jll'
  LEFT JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=e.external_id
), invalid AS (
  SELECT external_id FROM observed
  WHERE id IS NULL OR stored_url IS DISTINCT FROM source_url
    OR payload IS DISTINCT FROM expected_raw
    OR payload->>'sourceKey' IS DISTINCT FROM 'jll-investor'
    OR payload->'freshnessProvenance'->>'generationId' IS DISTINCT FROM {generation}
    OR payload->'jllInvestorDetail'->>'id' IS DISTINCT FROM substring(external_id FROM 10)
)
SELECT (SELECT count(*) FROM expected) AS expected,
       COALESCE((SELECT json_agg(external_id ORDER BY external_id)::text FROM invalid), '[]') AS invalid
"""


def admit(artifact_path, manifest, report, *, current_code_sha, current_git_dirty, phase):
    """Bind a partial artifact and its exact database identity partition to a gate."""
    import hashlib
    import json
    from pathlib import Path

    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts
    from cre_ingest import validate_strict_artifact_freshness

    source = "jll-investor"
    config = manifest["config"]
    if (phase not in ("source", "aggregate") or config.get("additive") is not True
            or config.get("mark_missing") is not False or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or source not in config.get("sources", []) or current_git_dirty
            or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)):
        raise ValueError("JLL partial owner or configuration invalid")
    path = Path(artifact_path)
    info = manifest["sources"][source]["artifact"]
    if path.is_symlink() or not path.is_file():
        raise ValueError("JLL partial artifact unavailable")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != info.get("sha256") or len(raw) != info.get("bytes"):
        raise ValueError("JLL partial artifact changed")
    data = json.loads(raw)
    meta = data.get("runMeta") or {}
    fresh = meta.get("freshness") or {}
    if (meta.get("mode") != "full" or meta.get("transactions") != ["sale", "lease"]
            or meta.get("maxItemsPerSource") is not None
            or fresh.get("generationId") != manifest.get("run_id")
            or fresh.get("generationStartedAt") != manifest.get("started_at")
            or fresh.get("requireFreshDetails") is not True):
        raise ValueError("JLL partial generation differs")
    validate_strict_artifact_freshness(data, require_strict_freshness=True)
    staged = stage_members(data, manifest["run_id"])
    target = manifest["preflight"]["database_target"]
    if (target.get("algorithm") != "sha256"
            or not re.fullmatch(r"[a-f0-9]{64}", target.get("value") or "")):
        raise ValueError("JLL partial database target invalid")
    probe = manifest["sources"][source].get("jll_identity_probe")
    staged_hash = hashlib.sha256(json.dumps(staged, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode()).hexdigest()
    if (not isinstance(probe, dict) or probe.get("artifact_sha256") != digest
            or probe.get("generation_id") != manifest["run_id"]
            or probe.get("producer_sha") != current_code_sha
            or probe.get("database_target_sha256") != target["value"]
            or probe.get("staged_sha256") != staged_hash):
        raise ValueError("JLL partial identity probe changed")
    held = probe.get("held")
    transaction_guard_sql(staged, held)
    if len(held) == len(staged):
        raise ValueError("JLL partial has no verified writable members")
    current, errors, sources, _, rejects = count_artifacts([str(path)], True)
    if (sources != {source} or errors.get(source) != "source pass truncated"
            or rejects != 0 or current.get(source) != len(staged)):
        raise ValueError("JLL partial gate evidence differs")
    validate_report_scope(report, manifest, source, path)
    summary, gate = report["summary"], report["per_source"][source]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False
            or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != len(staged)):
        raise ValueError("JLL partial gate scope invalid")
    verdict = "ok_additive_verified_subset"
    expected_reasons = {
        ("first_seen", "no baseline row; cannot gate (first sight); a source pass also errored this run"),
        ("hold", "source pass error: source pass truncated"),
    }
    if gate.get("verdict") == verdict:
        raw_pair = (gate.get("raw_verdict"), gate.get("raw_reason"))
    else:
        raw_pair = (gate.get("verdict"), gate.get("reason"))
    if raw_pair not in expected_reasons or gate.get("mark_missing_safe") is not False:
        raise ValueError("JLL partial gate reason differs")
    binding = {
        "schema": "jll-investor-partial-v1", "scope": "jll_investor_partial_v1", "source_key": source,
        "artifact_sha256": digest, "producer_sha": current_code_sha, "collector_git_sha": current_code_sha,
        "database_target_sha256": target["value"], "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"], "staged_sha256": staged_hash,
        "held": held, "held_count": len(held), "emitted_count": len(staged) - len(held),
        "excluded_source_identities": "unknown", "whole_source_coverage": False,
    }
    if phase == "aggregate" and manifest["sources"][source].get("gate", {}).get("current_inventory_proof") != binding:
        raise ValueError("JLL partial source gate changed")
    gate.update(raw_verdict=raw_pair[0], raw_reason=raw_pair[1], verdict=verdict,
                reason="verified JLL Investor partial rows admitted additively",
                mark_missing_safe=False, admission_scope="jll_investor_partial_v1",
                current_inventory_proof=binding)
    for key in ("hold_sources", "mark_missing_safe_sources"):
        values = summary.get(key)
        if isinstance(values, list):
            summary[key] = [value for value in values if value != source]
    values = summary.get("mark_missing_safe_brokerages")
    if isinstance(values, list):
        summary["mark_missing_safe_brokerages"] = [value for value in values if value != "jll"]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get("jll"), dict):
        report["per_brokerage"]["jll"]["mark_missing_safe"] = False
    if isinstance(summary.get("verdict_counts"), dict):
        counts = {}
        for source_gate in report["per_source"].values():
            value = source_gate.get("verdict")
            counts[value] = counts.get(value, 0) + 1
        summary["verdict_counts"] = counts
    report["scope"] = {"kind": "jll_investor_partial_v1", "transactions": ["sale", "lease"],
                       "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return binding


def run_identity_probe(artifact_path, artifact_sha256, generation, producer, target_sha256, env_file):
    """Read-only identity partition bound to the exact artifact and database."""
    import hashlib
    import json

    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    if (not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256 or "")
            or not re.fullmatch(r"[0-9a-f]{64}", target_sha256 or "")
            or not re.fullmatch(r"[0-9a-f]{40}", producer or "") or not generation):
        raise ValueError("JLL partial identity probe binding invalid")
    raw = read_file(artifact_path)
    if hashlib.sha256(raw).hexdigest() != artifact_sha256 or git_identity() != (producer, False):
        raise ValueError("JLL partial identity probe input changed")
    rows = stage_members(json.loads(raw), generation)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("JLL partial identity probe target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(rows) + ";\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise RuntimeError("JLL partial identity probe failed")
    data = parse_tsv(result.stdout)
    if (len(data) != 1 or set(data[0]) != {"staged", "joined", "held"}
            or data[0]["staged"] != str(len(rows)) or data[0]["joined"] != str(len(rows))):
        raise ValueError("JLL partial identity probe shape differs")
    held = json.loads(data[0]["held"])
    transaction_guard_sql(rows, held)
    current_url, _ = load_db_url(env_file)
    if (database_target_fingerprint_from_url(current_url).get("value") != target_sha256
            or read_file(artifact_path) != raw or git_identity() != (producer, False)):
        raise ValueError("JLL partial identity probe input or target changed")
    return {"artifact_sha256": artifact_sha256, "generation_id": generation,
            "producer_sha": producer, "database_target_sha256": target_sha256,
            "staged_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                                      ensure_ascii=False).encode()).hexdigest(),
            "held": held}


def ingest_subset_context(artifact_path, envelope, *, current_code_sha, current_git_dirty,
                          expected_database_target_sha256, attempt_id):
    """Reprove both gates and the identity partition at the ingest boundary."""
    import copy
    import json

    if (not isinstance(envelope, dict)
            or set(envelope) != {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
            or not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id)
            or envelope["attempt_id"] != attempt_id):
        raise ValueError("JLL partial claims envelope invalid")
    manifest = copy.deepcopy(envelope["manifest"])
    if manifest["preflight"]["database_target"] != {"algorithm": "sha256", "value": expected_database_target_sha256}:
        raise ValueError("JLL partial target changed")
    binding = envelope["binding"]
    for phase, name in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = copy.deepcopy(envelope[name])
        if (report["per_source"]["jll-investor"].get("current_inventory_proof") != binding
                or admit(artifact_path, manifest, report, current_code_sha=current_code_sha,
                         current_git_dirty=current_git_dirty, phase=phase) != binding):
            raise ValueError("JLL partial gate claim changed")
    data = json.loads(artifact_path.read_bytes())
    staged = stage_members(data, binding["generation_id"])
    held = list(binding["held"])
    transaction_guard_sql(staged, held)
    return {"binding": binding, "staged": staged, "held": held, "attempt_id": attempt_id}


def run_readback(artifact_path, claims, *, expected_database_target_sha256, env_file):
    """Perform a target-bound read-only exact-ID generation check after ingest."""
    import hashlib
    import json

    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    raw = read_file(artifact_path)
    binding = claims["binding"]
    if hashlib.sha256(raw).hexdigest() != binding["artifact_sha256"]:
        raise ValueError("JLL partial readback artifact changed")
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != expected_database_target_sha256:
        raise ValueError("JLL partial readback target differs")
    staged, held = claims["staged"], claims["held"]
    data = json.loads(raw)
    brokers = {i: value for i, value in enumerate(data.get("brokers") or [])}
    finished = (data.get("runMeta") or {}).get("finishedAt")
    raw_by_external_id = {}
    for listing in data["listings"]:
        mapped = to_row(listing, brokers, finished)
        if not isinstance(mapped, dict) or mapped["external_id"] in raw_by_external_id:
            raise ValueError("JLL partial readback source mapping changed")
        raw_by_external_id[mapped["external_id"]] = mapped["raw_data"]
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + readback_sql(staged, held, binding["generation_id"], raw_by_external_id)
           + ";\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode:
        raise RuntimeError("JLL partial readback query failed")
    data = parse_tsv(result.stdout)
    if len(data) != 1 or set(data[0]) != {"expected", "invalid"}:
        raise ValueError("JLL partial readback shape differs")
    invalid = json.loads(data[0]["invalid"])
    if (data[0]["expected"] != str(len(staged) - len(held))
            or not isinstance(invalid, list)
            or invalid != sorted(set(invalid))
            or not set(invalid) <= {row["external_id"] for row in staged} - set(held)):
        raise ValueError("JLL partial readback identities invalid")
    current_url, _ = load_db_url(env_file)
    if (database_target_fingerprint_from_url(current_url).get("value") != expected_database_target_sha256
            or read_file(artifact_path) != raw):
        raise ValueError("JLL partial readback target or artifact changed")
    return {"artifact_sha256": binding["artifact_sha256"], "generation_id": binding["generation_id"],
            "staged_sha256": binding["staged_sha256"], "expected": len(staged) - len(held),
            "held": held, "invalid": invalid, "ok": not invalid}
