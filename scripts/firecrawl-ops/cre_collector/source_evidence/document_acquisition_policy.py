"""Offline operator-policy preflight for unverified document candidates."""
import copy
import ipaddress
import os
import re
from urllib.parse import urlsplit

from cre_ingest import SOURCE_TO_BROKERAGE
from document_text import poppler_modes

from source_evidence import acquisition, document_candidates, events

SCHEMA = "cre-document-acquisition-policy/v1"
SCHEMA_V2 = "cre-document-acquisition-policy/v2"
SCHEMA_V3 = "cre-document-acquisition-policy/v3"
SCHEMA_V4 = "cre-document-acquisition-policy/v4"
MAX_OUTPUT = 16 * 1024 * 1024
LIMITS = {
    "max_documents": (1, 1000),
    "max_document_bytes": (1, 100 * 1024 * 1024),
    "max_total_bytes": (1, 1024 * 1024 * 1024),
    "document_timeout_seconds": (1, 60),
    "run_timeout_seconds": (1, 3600),
    "max_redirects": (0, 5),
}
JLL = {"jll", "jll-main", "jll-investor"}


def _host(value):
    if (not isinstance(value, str) or len(value) > 253 or not value.isascii()
            or value != value.lower() or value.endswith(".")
            or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)*", value)
            or any(len(part) > 63 for part in value.split("."))
            or value == "localhost" or value.endswith(".localhost")):
        raise ValueError("invalid acquisition host")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # Numeric-only dotted hosts can be interpreted differently by clients.
        if all(part.isdigit() for part in value.split(".")):
            raise ValueError("ambiguous numeric acquisition host")
    else:
        if not address.is_global:
            raise ValueError("nonpublic acquisition host")
    return value


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def candidate_refusal(candidate, allowed_hosts):
    """Evaluate the shared source URL eligibility without a document-count cap."""
    parsed = urlsplit(candidate["url"])
    if parsed.scheme not in ("http", "https"):
        return "unsupported_scheme"
    if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
        return "nondefault_port"
    if parsed.hostname not in allowed_hosts:
        return "host_not_allowed"
    if not any(occurrence["resolution"]["kind"] == "absolute_source_reference"
               for occurrence in candidate["occurrences"]):
        return "relative_only"
    return None


def validate_policy(policy_raw, *, expected_policy_sha256, target_sha256, collector_git_sha):
    """Validate a bounded operator assertion without acquisition or I/O."""
    if not _hex(expected_policy_sha256, 64) or not _hex(target_sha256, 64) or not _hex(collector_git_sha, 40):
        raise ValueError("invalid asserted policy, target or collector binding")
    if not isinstance(policy_raw, bytes) or len(policy_raw) > 16384 or events.sha256(policy_raw) != expected_policy_sha256:
        raise ValueError("policy bytes or digest mismatch")
    policy, literals = acquisition.decode_json(policy_raw, max_bytes=16384)
    fields = {"schema_version", "purpose", "data_scope", "collector_git_sha", "target_sha256",
              "source_keys", "allowed_hosts", "limits", "original_retention"}
    if isinstance(policy, dict) and policy.get("schema_version") in (SCHEMA_V2, SCHEMA_V3, SCHEMA_V4):
        fields |= {"pdf_complement"}
        complement = policy.get("pdf_complement")
        if (not isinstance(complement, dict) or set(complement) != {"mode", "pdftotext_path"}
                or complement.get("mode") not in poppler_modes.MODES
                or not isinstance(complement.get("pdftotext_path"), str)
                or not os.path.isabs(complement["pdftotext_path"])):
            raise ValueError("invalid PDF complement policy")
    if isinstance(policy, dict) and policy.get("schema_version") in (SCHEMA_V3, SCHEMA_V4):
        fields.add("sparse_ocr")
        ocr = policy.get("sparse_ocr")
        if (not isinstance(ocr, dict) or set(ocr) != {"mode", "strategy", "max_pages"}
                or ocr.get("mode") != "sparse_ocr_v1" or ocr.get("strategy") not in ("no_text", "all_pages")
                or type(ocr.get("max_pages")) is not int or not 1 <= ocr["max_pages"] <= 1000):
            raise ValueError("invalid sparse OCR policy")
    if isinstance(policy, dict) and policy.get("schema_version") == SCHEMA_V4:
        from source_evidence import document_priority
        fields.add("selection_order")
        if policy.get("selection_order") != document_priority.MODE:
            raise ValueError("invalid document selection order")
    if (literals or not isinstance(policy, dict) or set(policy) != fields or policy["schema_version"] not in (SCHEMA, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4)
            or policy["purpose"] != "private_public_listing_document_text"
            or policy["data_scope"] != "public_listing_source"
            or policy["collector_git_sha"] != collector_git_sha or policy["target_sha256"] != target_sha256
            or policy["original_retention"] not in ("temporary_only", "keep_unresolved_until_review")):
        raise ValueError("invalid acquisition policy or binding")
    sources = policy["source_keys"]
    hosts = policy["allowed_hosts"]
    limits = policy["limits"]
    if (not isinstance(sources, list) or not sources or any(not isinstance(s, str) for s in sources)
            or len(sources) != len(set(sources)) or any(s not in SOURCE_TO_BROKERAGE or s in JLL for s in sources)
            or not isinstance(hosts, dict) or set(hosts) != set(sources)
            or not isinstance(limits, dict) or set(limits) != set(LIMITS)):
        raise ValueError("invalid source, host or limits scope")
    for source in sources:
        entries = hosts[source]
        if not isinstance(entries, list) or not entries or any(not isinstance(h, str) for h in entries) or len(entries) != len(set(entries)):
            raise ValueError("invalid allowed host list")
        for entry in entries:
            _host(entry)
    for key, (minimum, maximum) in LIMITS.items():
        if type(limits[key]) is not int or not minimum <= limits[key] <= maximum:
            raise ValueError("invalid acquisition limit")
    if policy["schema_version"] in (SCHEMA_V2, SCHEMA_V3, SCHEMA_V4):
        from source_evidence import (
            doc_complement_process,
            doc_extract_process,
            doc_fetch_process,
            document_pipeline,
        )
        minimum_phase = (limits["document_timeout_seconds"] + doc_fetch_process.STARTUP_GRACE_SECONDS
                         + doc_fetch_process.REAP_GRACE_SECONDS + document_pipeline.EXTRACTION_SECONDS
                         + doc_extract_process.REAP_GRACE_SECONDS + doc_complement_process.TIMEOUT_SECONDS
                         + doc_extract_process.REAP_GRACE_SECONDS)
        if policy["schema_version"] in (SCHEMA_V3, SCHEMA_V4):
            from source_evidence import doc_sparse_ocr_process
            minimum_phase += doc_sparse_ocr_process.TIMEOUT_SECONDS + doc_extract_process.REAP_GRACE_SECONDS
        if limits["run_timeout_seconds"] < minimum_phase:
            raise ValueError("run timeout cannot accommodate the complementary parser phase")
    return policy


def prepare(plan, candidates, policy_raw, *, expected_policy_sha256, target_sha256, collector_git_sha):
    """Return a bounded selection; this function performs no acquisition."""
    policy = validate_policy(policy_raw, expected_policy_sha256=expected_policy_sha256,
                             target_sha256=target_sha256, collector_git_sha=collector_git_sha)
    sources, hosts, limits = policy["source_keys"], policy["allowed_hosts"], policy["limits"]

    events.validate_content_policy(plan)
    actual = document_candidates.discover(plan)
    if events.encoded(actual) != events.encoded(candidates):
        raise ValueError("candidate manifest disagrees with source evidence")
    source = plan["source_key"]
    if source not in sources:
        raise ValueError("source is not allowed by policy")
    output = {"schema_version": "cre-document-acquisition-plan/v1", "policy_sha256": expected_policy_sha256,
              "candidate_manifest_sha256": events.sha256(events.encoded(candidates)),
              "source_evidence_plan_sha256": plan["plan_sha256"], "capture_id": candidates["capture_id"],
              "source_key": source, "collector_git_sha": collector_git_sha, "target_sha256": target_sha256,
              "allowed_hosts": hosts[source], "limits": limits, "original_retention": policy["original_retention"],
              "documents": [], "exclusions": [], "network_execution": "not_performed",
              "destination_verification": "pending_each_request", "publisher": "unverified",
              "publicness": "unverified", "authority": "operator_asserted_class_policy_not_publisher_authentication",
              "cumulative_byte_budget": "not_verified_before_acquisition"}
    if policy["schema_version"] in (SCHEMA_V2, SCHEMA_V3, SCHEMA_V4):
        output["pdf_complement"] = copy.deepcopy(policy["pdf_complement"])
    if policy["schema_version"] in (SCHEMA_V3, SCHEMA_V4):
        output["sparse_ocr"] = copy.deepcopy(policy["sparse_ocr"])
    priority = None
    if policy["schema_version"] == SCHEMA_V4:
        from source_evidence import document_priority
        output["selection_order"] = policy["selection_order"]
        priority = document_priority.witnesses(plan, candidates, collector_git_sha=collector_git_sha,
                                               target_sha256=target_sha256)
    size = len(events.encoded(output))
    indices = list(range(len(candidates["candidates"])))
    if priority is not None:
        indices.sort(key=lambda index: priority[index] is None)
    for index in indices:
        candidate = candidates["candidates"][index]
        reason = candidate_refusal(candidate, hosts[source])
        if reason is None and len(output["documents"]) >= limits["max_documents"]:
            reason = "document_count_limit"
        if reason:
            entry = {"candidate_index": index, "reason": reason}
            output["exclusions"].append(entry)
        else:
            entry = {"candidate_index": index, "candidate_sha256": events.sha256(events.encoded(candidate)),
                     "candidate": copy.deepcopy(candidate)}
            output["documents"].append(entry)
        if priority is not None:
            entry["priority"] = {"class": "property" if priority[index] is not None else "unprioritized",
                                 "witness": copy.deepcopy(priority[index])}
        size += len(events.encoded(entry)) + 1
        if size > MAX_OUTPUT:
            raise ValueError("acquisition selection exceeds limit")
    if len(events.encoded(output)) > MAX_OUTPUT:
        raise ValueError("acquisition selection exceeds limit")
    return output
