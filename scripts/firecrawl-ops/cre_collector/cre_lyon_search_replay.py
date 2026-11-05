"""Replay authenticated Lyon partial pages; this module does not grant admission."""
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit

_FILTERS = ("all", "sale", "lease")
_STATUS = ("all-status", "for-sale", "for-lease")
_ENDPOINTS = {
    name: "https://lyonstahl.com/wp-admin/admin-ajax.php?action=FilterProperties&markets=all-markets"
          f"&Status={status}&propertyType=all-property-types"
    for name, status in zip(_FILTERS, _STATUS, strict=True)
}


def _identity(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("invalid source URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "lyonstahl.com" or parsed.fragment:
        raise ValueError("invalid source URL")
    match = re.fullmatch(r"/properties/([^/]+)/?", parsed.path, re.IGNORECASE)
    if match and not parsed.query:
        return {"kind": "property_path", "value": match.group(1)}
    try:
        params = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("invalid source query") from exc
    ids = [value for key, value in params if key == "p"]
    if (parsed.path == "/" and len(ids) == 1 and re.fullmatch(r"[1-9][0-9]*", ids[0])
            and len(params) == 2 and set(params) == {("post_type", "properties"), ("p", ids[0])}):
        return {"kind": "wordpress_post", "value": ids[0]}
    raise ValueError("invalid source identity")


def replay_search(raw, generation, attempt, *, summary=None):
    """Replay in-memory fixture bytes; custody callers should stream lines."""
    if not isinstance(raw, bytes) or not raw.endswith(b"\n"):
        raise ValueError("invalid Lyon spool")
    return replay_search_lines(iter(raw.splitlines()), generation, attempt, summary=summary)


def replay_search_lines(lines, generation, attempt, *, summary=None):
    """Build records from settled private spool lines authenticated by caller.

    Malformed page bodies and nonidentifiable cards stay held in the original
    spool. Neither page coverage nor source completion is implied.
    """
    def bad_constant(_value):
        raise ValueError("invalid JSON constant")
    lines = iter(lines)
    try:
        header = json.loads(next(lines), parse_constant=bad_constant)
    except StopIteration as exc:
        raise ValueError("empty Lyon spool") from exc
    if header != {"schema": "lyon-stahl-partial-pages/v1", "sourceKey": "lyon-stahl",
                  "generation": generation, "attempt": attempt}:
        raise ValueError("Lyon spool binding differs")
    records, held, seen_filters = {}, [], set()
    for raw_line in lines:
        line = json.loads(raw_line, parse_constant=bad_constant)
        if not isinstance(line, dict) or set(line) != {"kind", "transaction", "pass", "page", "observedAt", "documents"}:
            raise ValueError("invalid Lyon spool record")
        docs = line["documents"]
        if not isinstance(docs, list) or len(docs) != 1 or not isinstance(docs[0], dict):
            raise ValueError("invalid Lyon snapshot")
        page = docs[0]
        if set(page) != {"filter", "observedAt", "responseText", "endpoint", "responseTextSha256"}:
            raise ValueError("invalid Lyon snapshot")
        name = page["filter"]
        if (name not in _FILTERS or name in seen_filters or line["kind"] != "lyon_search_response"
                or line["transaction"] != "sale" or type(line["pass"]) is not int
                or line["pass"] != _FILTERS.index(name) + 1 or type(line["page"]) is not int or line["page"] != 1
                or line["observedAt"] != page["observedAt"] or page["endpoint"] != _ENDPOINTS[name]
                or not isinstance(page["responseText"], str)
                or hashlib.sha256(page["responseText"].encode("utf-8")).hexdigest() != page["responseTextSha256"]):
            raise ValueError("Lyon snapshot binding differs")
        seen_filters.add(name)
        try:
            rows = json.loads(page["responseText"], parse_constant=bad_constant)
            if not isinstance(rows, list):
                raise ValueError("expected array")
        except (ValueError, RecursionError):
            held.append({"filter": name, "reason": "invalid_response"})
            continue
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict):
                    raise ValueError("invalid card")
                url = row.get("permalink")
                identity = _identity(url)
            except (TypeError, ValueError):
                held.append({"filter": name, "index": index, "reason": "invalid_identity"})
                continue
            record = records.setdefault(url, {"url": url, "sourceIdentity": identity, "occurrences": []})
            record["occurrences"].append({"filter": name, "index": index, "observedAt": page["observedAt"],
                                          "responseTextSha256": page["responseTextSha256"], "row": row})
    identities = {}
    for url, record in records.items():
        identity = record["sourceIdentity"]
        key = ("wordpress:" if identity["kind"] == "wordpress_post" else "") + identity["value"]
        identities.setdefault(key, []).append(url)
    accepted = []
    for record in records.values():
        identity = record["sourceIdentity"]
        filters = [entry["filter"] for entry in record["occurrences"]]
        key = ("wordpress:" if identity["kind"] == "wordpress_post" else "") + identity["value"]
        if len(identities[key]) > 1 or len(filters) != len(set(filters)):
            held.append({"url": record["url"], "reason": "identity_conflict"})
        else:
            accepted.append(record)
    if summary is not None:
        failures = summary["failures"]
        by_filter = {}
        for failure in failures:
            name = failure["filter"]
            if name in by_filter:
                raise ValueError("duplicate Lyon source failure")
            by_filter[name] = failure["reason"]
        invalid = {item["filter"] for item in held if item["reason"] == "invalid_response"}
        acquired_failure = {name for name, reason in by_filter.items() if reason == "acquisition_failed"}
        invalid_failure = {name for name, reason in by_filter.items() if reason == "invalid_response"}
        if (invalid != invalid_failure or seen_filters & acquired_failure
                or seen_filters | acquired_failure != set(_FILTERS)
                or summary["candidates"] != len(records)
                or summary["held"] != sum(item["reason"] == "invalid_identity" for item in held)):
            raise ValueError("Lyon source summary and replay differ")
    return accepted, held


def validate_source_artifact(data):
    """Check source outcome before replay; caller authenticates custody and owner."""
    if not isinstance(data, dict) or not isinstance(data.get("runMeta"), dict):
        raise ValueError("invalid Lyon source artifact")
    meta = data["runMeta"]
    if (meta.get("terminalCaptureFailure") is not None or meta.get("evidenceOnly") is not True
            or data.get("listings") != [] or not isinstance(data.get("sources"), list)):
        raise ValueError("Lyon source artifact not eligible")
    entries = [entry for entry in data["sources"] if isinstance(entry, dict)
               and entry.get("sourceKey") == "lyon-stahl" and entry.get("transaction") == "sale"]
    if len(entries) != 1:
        raise ValueError("Lyon source scope differs")
    entry = entries[0]
    observations = entry.get("retainedObservations")
    if (entry.get("error") != "Error: Lyon search is partial and requires source-bound admission"
            or not isinstance(observations, list) or len(observations) != 1):
        raise ValueError("Lyon source outcome differs")
    summary = observations[0]
    if (not isinstance(summary, dict) or set(summary) != {"schema", "coverage", "stopped", "failures", "candidates", "held"}
            or summary["schema"] != "lyon-search-summary-v1" or summary["coverage"] != "partial_unknown"
            or summary["stopped"] is not False or not isinstance(summary["failures"], list)
            or type(summary["candidates"]) is not int or summary["candidates"] < 0
            or type(summary["held"]) is not int or summary["held"] < 0):
        raise ValueError("Lyon source summary differs")
    for failure in summary["failures"]:
        if (not isinstance(failure, dict) or failure.get("filter") not in _FILTERS
                or failure.get("reason") not in {"acquisition_failed", "invalid_response"}):
            raise ValueError("Lyon source failure not eligible")
    return summary
