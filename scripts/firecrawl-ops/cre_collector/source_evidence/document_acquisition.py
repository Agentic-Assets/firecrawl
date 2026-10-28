"""Pure validation of document acquisition and AnyDoc outcome bindings.

The receipt is a caller assertion, not publisher authentication or proof of
network behavior. This module performs no download, file or database I/O.
"""
import copy
from datetime import datetime
from urllib.parse import urlsplit

from source_evidence import document_acquisition_policy, document_candidates, events

SCHEMA = "cre-document-acquisition-context/v1"
RECEIPT_KEYS = {"started_at", "finished_at", "request_url", "final_url", "http_status", "hops",
                "outcome", "failure_code", "body_sha256", "body_bytes", "body_representation",
                "completion", "deadline_enforcement"}
FAILURES = {"unsafe_url", "host_not_allowed", "nondefault_port", "dns_failed", "destination_not_public",
            "connection_target_mismatch", "redirect_limit", "redirect_missing_location", "http_status",
            "unsupported_content_encoding", "invalid_content_length", "body_too_large", "incomplete_body",
            "unsupported_transfer_encoding", "ambiguous_transfer_framing",
            "deadline_exceeded", "deadline_or_socket_timeout", "transport_error", "connection_close_failed"}
REDIRECTS = {301, 302, 303, 307, 308}
REPRESENTATION = "http_entity_bytes_after_transfer_framing_identity_content_encoding"


def validate_selection(plan, candidates, policy_raw, selection, *, expected_policy_sha256,
                       target_sha256, collector_git_sha):
    expected = document_acquisition_policy.prepare(
        plan, candidates, policy_raw, expected_policy_sha256=expected_policy_sha256,
        target_sha256=target_sha256, collector_git_sha=collector_git_sha)
    if events.encoded(selection) != events.encoded(expected):
        raise ValueError("document selection disagrees with rederived policy")
    return expected


def _scope_url(value, hosts):
    document_candidates._safe_url(value)
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in hosts
            or parsed.port not in (None, 443 if parsed.scheme == "https" else 80)):
        raise ValueError("document receipt URL outside selected scope")


def _receipt_checked(receipt, body, candidate, selection):
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS
            or receipt.get("body_representation") != REPRESENTATION
            or receipt.get("deadline_enforcement") != "external_worker_required"
            or receipt.get("outcome") not in ("acquired", "failed")
            or not isinstance(receipt.get("hops"), list)
            or len(receipt["hops"]) > selection["limits"]["max_redirects"] + 1):
        raise ValueError("document receipt shape mismatch")
    start = events.timestamp(receipt.get("started_at"))
    finish = events.timestamp(receipt.get("finished_at"))
    if datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(finish.replace("Z", "+00:00")):
        raise ValueError("document acquisition clock reversed")
    request = receipt["request_url"]
    if request is not None:
        if request != candidate["url"]:
            raise ValueError("document request does not match selected URL")
        _scope_url(request, selection["allowed_hosts"])
    for i, hop in enumerate(receipt["hops"]):
        if (not isinstance(hop, dict) or set(hop) != {"url", "status"}
                or type(hop["status"]) is not int or not 100 <= hop["status"] <= 599):
            raise ValueError("document response hop mismatch")
        _scope_url(hop["url"], selection["allowed_hosts"])
        if i == 0 and hop["url"] != request:
            raise ValueError("document first hop mismatch")
        if i + 1 < len(receipt["hops"]) and hop["status"] not in REDIRECTS:
            raise ValueError("document nonredirect precedes another hop")
    if receipt["hops"]:
        last = receipt["hops"][-1]
        if receipt["final_url"] != last["url"] or receipt["http_status"] != last["status"]:
            raise ValueError("document final response mismatch")
    elif receipt["final_url"] is not None or receipt["http_status"] is not None:
        raise ValueError("document response lacks a hop")
    if receipt["outcome"] == "acquired":
        if (not isinstance(body, bytes) or request is None or receipt["http_status"] != 200
                or receipt["completion"] != "complete" or receipt["failure_code"] is not None
                or type(receipt["body_bytes"]) is not int or receipt["body_bytes"] != len(body)
                or len(body) > selection["limits"]["max_document_bytes"]
                or len(body) > selection["limits"]["max_total_bytes"]
                or receipt["body_sha256"] != events.sha256(body)):
            raise ValueError("document acquired body mismatch")
    elif (body is not None or receipt["completion"] != "incomplete" or receipt["failure_code"] not in FAILURES
          or receipt["body_sha256"] is not None or receipt["body_bytes"] is not None):
        raise ValueError("document failure receipt mismatch")


def build_context(plan, candidates, policy_raw, selection, candidate_index, receipt, body, *,
                  expected_policy_sha256, target_sha256, collector_git_sha):
    """Recompute selection and bind the actual attempted receipt to one candidate."""
    selected = validate_selection(plan, candidates, policy_raw, selection,
                                  expected_policy_sha256=expected_policy_sha256,
                                  target_sha256=target_sha256, collector_git_sha=collector_git_sha)
    if type(candidate_index) is not int or candidate_index < 0:
        raise ValueError("invalid document candidate index")
    matches = [item for item in selected["documents"] if item["candidate_index"] == candidate_index]
    if len(matches) != 1:
        raise ValueError("document candidate is not selected")
    candidate = matches[0]["candidate"]
    _receipt_checked(receipt, body, candidate, selected)
    return {"schema_version": SCHEMA, "source_key": selected["source_key"],
            "source_evidence_plan_sha256": selected["source_evidence_plan_sha256"],
            "candidate_manifest_sha256": selected["candidate_manifest_sha256"],
            "policy_sha256": selected["policy_sha256"], "target_sha256": selected["target_sha256"],
            "collector_git_sha": selected["collector_git_sha"], "candidate_index": candidate_index,
            "candidate_sha256": matches[0]["candidate_sha256"], "candidate": copy.deepcopy(candidate),
            "receipt": copy.deepcopy(receipt), "publisher": "unverified", "visibility": "unverified",
            "destination_verification": "caller_asserted_not_independently_verified",
            "redirect_location_binding": "not_observed_in_receipt", "cumulative_byte_budget": "not_verified",
            "requested_original_retention": selected["original_retention"], "original_retention": "not_asserted"}


def bind_extraction(context, artifact):
    """Bind AnyDoc output to a completed acquisition; no source identity is inferred."""
    if (not isinstance(context, dict) or context.get("schema_version") != SCHEMA
            or not isinstance(context.get("receipt"), dict) or context["receipt"].get("outcome") != "acquired"
            or not isinstance(artifact, dict) or artifact.get("schema_version") != "cre-document-text/v1"):
        raise ValueError("document extraction lacks an acquired context")
    receipt = context["receipt"]
    source = artifact.get("source")
    actual = ({key: source.get(key) for key in ("url", "sha256", "byte_length", "observed_at")}
              if isinstance(source, dict) else None)
    expected = {"url": receipt["final_url"], "sha256": receipt["body_sha256"],
                "byte_length": receipt["body_bytes"], "observed_at": receipt["finished_at"]}
    if actual is None or events.encoded(actual) != events.encoded(expected):
        raise ValueError("document extraction disagrees with acquired bytes or clock")
    plan = events.plan(artifact, source_key=context["source_key"])
    events.validate_content_policy(plan)
    return {"schema_version": "cre-document-extraction-binding/v1",
            "acquisition_context_sha256": events.sha256(events.encoded(context)),
            "document_plan_sha256": plan["plan_sha256"], "document_capture_id": plan["events"][0]["capture_id"],
            "source_identity": "unverified", "original_retention": "not_asserted",
            "extraction_execution": "caller_asserted_metadata_not_reexecuted"}, plan
