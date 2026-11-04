"""Derive source-scoped location priority from sealed collector records.

This is ordering evidence only; it does not decide access, document type,
visibility, listing identity or whether a URL is safe to acquire.
"""
import re
from datetime import datetime

from source_evidence import events

MODE = "property_occurrences_first_v1"
SCHEMA = "cre-document-priority-witness/v1"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PROPERTY_BASES = {"property_attachment_table", "property_banner_download"}


def _records(plan, *, collector_git_sha, target_sha256):
    if not isinstance(plan, dict) or plan.get("source_key") != "transwestern":
        return {}
    rows = plan.get("events")
    if not isinstance(rows, list) or not rows:
        return {}
    header = rows[0].get("facts", []) if isinstance(rows[0], dict) else []
    if (not isinstance(header, list) or len(header) != 1 or not isinstance(header[0], dict)
            or header[0].get("kind") not in
            ("retained_capture_context", "retained_capture_context_v2")):
        return {}
    value = header[0].get("value", {})
    if not isinstance(value, dict):
        return {}
    provenance, context = value.get("provenance"), value.get("context")
    if not isinstance(provenance, dict) or not isinstance(context, dict):
        return {}
    binding = provenance.get("automaticPolicy", {})
    run = context.get("runMeta", {})
    run_freshness = run.get("freshness", {}) if isinstance(run, dict) else {}
    if (not isinstance(binding, dict) or binding.get("collectorGitSha") != collector_git_sha
            or binding.get("targetSha256") != target_sha256
            or not isinstance(binding.get("artifactFileSha256"), str)
            or not HEX64.fullmatch(binding["artifactFileSha256"])
            or not isinstance(binding.get("generation"), str) or not binding["generation"]
            or type(binding.get("attempt")) is not int or binding["attempt"] < 1):
        return {}
    if (not isinstance(run_freshness, dict) or run_freshness.get("generationId") != binding["generation"]
            or run_freshness.get("requireFreshDetails") is not True):
        return {}
    try:
        epoch = lambda x: datetime.fromisoformat(events.timestamp(x))
        start, run_start, finish = (epoch(run_freshness.get("generationStartedAt")), epoch(run.get("startedAt")),
                                    epoch(run.get("finishedAt")))
        if not start <= run_start <= finish:
            return {}
    except (TypeError, ValueError):
        return {}
    result = {}
    for event in rows[1:]:
        facts = event.get("facts", [])
        if len(facts) != 1 or facts[0].get("kind") != "source_record":
            continue
        row = facts[0].get("value")
        if not isinstance(row, dict) or row.get("sourceKey") != "transwestern":
            continue
        role = row.get("transwesternDocumentMediaObservations")
        details = row.get("detailScrape")
        freshness = row.get("freshnessProvenance")
        building = row.get("transwesternBuildingFacts")
        if not all(isinstance(x, dict) for x in (role, details, freshness, building)):
            continue
        source_url, observed, body_hash = role.get("sourceUrl"), role.get("observedAt"), role.get("bodySha256")
        if (role.get("schema") != "transwestern-document-media-roles/v1" or role.get("state") != "inspected"
                or not isinstance(source_url, str) or source_url != row.get("url") or source_url != details.get("url")
                or not isinstance(observed, str) or observed != row.get("detailObservedAt")
                or observed != building.get("observedAt") or source_url != building.get("sourceUrl")
                or not isinstance(body_hash, str) or not HEX64.fullmatch(body_hash)
                or body_hash != building.get("bodySha256")
                or freshness.get("detailScope") != "detail_page"
                or freshness.get("method") != "transwestern_detail_page" or freshness.get("cacheDisposition") != "live"
                or freshness.get("generationId") != binding["generation"]
                or any(type(role.get(key)) is not int or role[key] != 0
                       for key in ("omittedCount", "skippedCandidateCount", "skippedLinkEntryCount"))):
            continue
        try:
            inventory = epoch(row.get("inventoryObservedAt"))
            if not start <= inventory <= epoch(observed) <= finish or epoch(observed) < run_start:
                continue
        except (TypeError, ValueError):
            continue
        occurrences = role.get("occurrences", [])
        if (not isinstance(occurrences, list) or len(occurrences) > 1000
                or type(role.get("occurrenceCount")) is not int or role["occurrenceCount"] != len(occurrences)
                or type(role.get("candidateCount")) is not int
                or type(role.get("eligibleLinkCandidateCount")) is not int):
            continue
        locations = {}
        for ordinal, occurrence in enumerate(occurrences):
            if (not isinstance(occurrence, dict) or not isinstance(occurrence.get("url"), str)
                    or occurrence.get("role") not in ("property", "sitewide", "unknown")
                    or occurrence.get("channel") not in ("document", "media", "link")):
                locations = None
                break
            locations.setdefault(occurrence["url"], []).append((ordinal, occurrence))
        if locations is None:
            continue
        link_urls = {item["url"] for item in occurrences if item["channel"] == "link"}
        if role["candidateCount"] != len(locations) or role["eligibleLinkCandidateCount"] != len(link_urls):
            continue
        result[event["item_no"]] = (binding, role, events.sha256(events.encoded(role)), locations)
    return result


def witnesses(plans, candidates, *, collector_git_sha, target_sha256):
    """Return one bounded deterministic witness per candidate, or None.

    Caller must first validate plans and rederive the exact candidate manifest.
    """
    plans = plans if isinstance(plans, list) else [plans]
    by_plan = {plan["plan_sha256"]: (plan, _records(plan, collector_git_sha=collector_git_sha,
                                                     target_sha256=target_sha256)) for plan in plans}
    single = len(plans) == 1
    result = []
    for candidate in candidates["candidates"]:
        witness = None
        for occurrence in candidate["occurrences"]:
            plan_sha = plans[0]["plan_sha256"] if single and "plan_sha256" not in occurrence else occurrence.get("plan_sha256")
            entry = by_plan.get(plan_sha)
            if entry is None or occurrence.get("fact_kind") != "source_record":
                continue
            pointer = occurrence.get("json_pointer")
            if not isinstance(pointer, str) or not re.fullmatch(r"/facts/0/value/(?:documents|links)/\d+/url", pointer):
                continue
            plan, records = entry
            if occurrence.get("capture_id", plan["events"][0]["capture_id"]) != plan["events"][0]["capture_id"]:
                continue
            item_no = occurrence.get("item_no")
            record = records.get(item_no)
            if record is None:
                continue
            binding, role, role_sha256, locations = record
            same_url = locations.get(candidate["url"], [])
            property_matches = [(index, item) for index, item in same_url
                                if item.get("role") == "property" and item.get("basis") in PROPERTY_BASES
                                and item.get("channel") == ("document" if "/documents/" in pointer else "link")
                                and item.get("element") == "a" and item.get("attribute") == "href"]
            if not property_matches or witness is not None:
                continue
            index, location = property_matches[0]
            witness = {"schema": SCHEMA, "plan_sha256": plan_sha,
                       "capture_id": plan["events"][0]["capture_id"], "item_no": item_no,
                       "candidate_pointer": occurrence["json_pointer"], "role_ordinal": index,
                       "role_sha256": role_sha256,
                       "artifact_file_sha256": binding["artifactFileSha256"],
                       "generation": binding["generation"], "attempt": binding["attempt"],
                       "source_url": role["sourceUrl"], "body_sha256": role["bodySha256"],
                       "observed_at": role["observedAt"], "basis": location["basis"],
                       "custody": "collector_binding_asserted_not_independently_verified"}
        result.append(witness)
    return result
