"""Pure same-source, multi-capture document discovery and policy selection."""

import copy

from source_evidence import document_acquisition_policy, document_candidates, events

SCHEMA = "cre-source-document-candidate-batch/v1"
SELECTION_SCHEMA = "cre-document-acquisition-plan/v2"
MAX_PLANS = 129
MAX_PLANS_BYTES = 64 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MAX_CANDIDATES = 10000
MAX_OCCURRENCES = 10000


def _discover(plans):
    if not isinstance(plans, list) or not 1 <= len(plans) <= MAX_PLANS:
        raise ValueError("invalid document batch plan count")
    total = 0
    for plan in plans:
        total += len(events.encoded(plan))
        if total > MAX_PLANS_BYTES:
            raise ValueError("document batch plans exceed bound")
    output = {"schema_version": SCHEMA, "source_key": None, "source_evidence_plans": [],
              "source_evidence_set_sha256": None, "candidates": [], "excluded": [],
              "coverage": "unverified", "acquisition_enabled": False}
    manifests = []
    seen_plans, seen_captures = set(), set()
    by_url = {}
    occurrence_count = 0
    size = 1024
    for plan in plans:
        found = document_candidates.discover(plan)
        source = found["source_key"]
        plan_sha, capture = found["plan_sha256"], found["capture_id"]
        if output["source_key"] is None:
            output["source_key"] = source
        if source != output["source_key"] or plan_sha in seen_plans or capture in seen_captures:
            raise ValueError("document batch has mixed source or duplicate capture")
        seen_plans.add(plan_sha)
        seen_captures.add(capture)
        entry = {"plan_sha256": plan_sha, "capture_id": capture,
                 "candidate_manifest_sha256": events.sha256(events.encoded(found))}
        output["source_evidence_plans"].append(entry)
        size += len(events.encoded(entry)) + 1
        manifests.append(found)
        for original in found["candidates"]:
            url = original["url"]
            if url not in by_url:
                if len(by_url) >= MAX_CANDIDATES:
                    raise ValueError("document batch candidate count exceeds bound")
                candidate = {key: copy.deepcopy(value) for key, value in original.items() if key != "occurrences"}
                candidate["occurrences"] = []
                size += len(events.encoded(candidate)) + 1
                by_url[url] = candidate
                output["candidates"].append(candidate)
            for occurrence in original["occurrences"]:
                occurrence_count += 1
                if occurrence_count > MAX_OCCURRENCES:
                    raise ValueError("document batch occurrence count exceeds bound")
                enriched = {**copy.deepcopy(occurrence), "plan_sha256": plan_sha, "capture_id": capture}
                size += len(events.encoded(enriched)) + 1
                if size > MAX_OUTPUT:
                    raise ValueError("document batch manifest exceeds bound")
                by_url[url]["occurrences"].append(enriched)
        for excluded in found["excluded"]:
            occurrence_count += 1
            if occurrence_count > MAX_OCCURRENCES:
                raise ValueError("document batch occurrence count exceeds bound")
            item = {**copy.deepcopy(excluded), "plan_sha256": plan_sha, "capture_id": capture}
            size += len(events.encoded(item)) + 1
            if size > MAX_OUTPUT:
                raise ValueError("document batch manifest exceeds bound")
            output["excluded"].append(item)
    output["source_evidence_set_sha256"] = events.sha256(events.encoded(
        [entry["plan_sha256"] for entry in output["source_evidence_plans"]]))
    if len(events.encoded(output)) > MAX_OUTPUT:
        raise ValueError("document batch manifest exceeds bound")
    return output, manifests


def discover(plans):
    """Discover exact URLs and preserve all capture-specific source occurrences."""
    return _discover(plans)[0]


def prepare(plans, candidates, policy_raw, *, expected_policy_sha256, target_sha256, collector_git_sha):
    """Revalidate every source plan and apply one global policy and document cap."""
    actual, manifests = _discover(plans)
    if events.encoded(actual) != events.encoded(candidates):
        raise ValueError("batch candidate manifest disagrees with source evidence")
    bindings = {"expected_policy_sha256": expected_policy_sha256, "target_sha256": target_sha256,
                "collector_git_sha": collector_git_sha}
    first = None
    for plan, manifest in zip(plans, manifests):
        validated = document_acquisition_policy.prepare(plan, manifest, policy_raw, **bindings)
        if first is None:
            first = validated
    output = {key: copy.deepcopy(first[key]) for key in ("policy_sha256", "source_key", "collector_git_sha",
              "target_sha256", "allowed_hosts", "limits", "original_retention", "network_execution",
              "destination_verification", "publisher", "publicness", "authority", "cumulative_byte_budget")}
    if "pdf_complement" in first:
        output["pdf_complement"] = copy.deepcopy(first["pdf_complement"])
    output.update(schema_version=SELECTION_SCHEMA,
                  source_evidence_plans=copy.deepcopy(actual["source_evidence_plans"]),
                  source_evidence_set_sha256=actual["source_evidence_set_sha256"],
                  candidate_manifest_sha256=events.sha256(events.encoded(actual)), documents=[], exclusions=[])
    size = len(events.encoded(output))
    for index, candidate in enumerate(actual["candidates"]):
        reason = document_acquisition_policy.candidate_refusal(candidate, output["allowed_hosts"])
        if reason is None and len(output["documents"]) >= output["limits"]["max_documents"]:
            reason = "document_count_limit"
        if reason:
            entry = {"candidate_index": index, "reason": reason}
            output["exclusions"].append(entry)
        else:
            entry = {"candidate_index": index, "candidate_sha256": events.sha256(events.encoded(candidate)),
                     "candidate": copy.deepcopy(candidate)}
            output["documents"].append(entry)
        size += len(events.encoded(entry)) + 1
        if size > MAX_OUTPUT:
            raise ValueError("document batch selection exceeds bound")
    if len(events.encoded(output)) > MAX_OUTPUT:
        raise ValueError("document batch selection exceeds bound")
    return output
