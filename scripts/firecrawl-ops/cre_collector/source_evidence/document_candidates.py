"""Discover unverified document references from a validated evidence plan.

Discovery performs no fetch, source promotion, identity resolution or admission.
"""
import copy
import re
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from buildout_observation_artifact import SECRET_QUERY, credential_label, sensitive_string
from source_evidence import events

MAX_NODES = 500000
MAX_DEPTH = 128
MAX_OCCURRENCES = 10000
MAX_CANDIDATES = 10000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_POINTER_BYTES = 4096
MAX_OCCURRENCE_BYTES = 65536
PAGE_SCHEMAS = {"cre-source-page-context/v1", "foundry-native-page-context/v1"}
DOCUMENT_LABEL = re.compile(r"\b(?:brochures?|offering\s+memorand(?:um|a)|om|flyers?|floor\s*plans?|rent\s*rolls?|"
                            r"risk\s*questionnaires?|financials?|financial\s+statements?|property\s+packages?|documents?)\b", re.I)
IDENTITY_FIELDS = ("id", "externalId", "external_id", "foundryPostId", "propertyId", "sourceKey",
                   "url", "sourceUrl", "requestedUrl", "requestedURL", "inventoryObservedAt", "detailObservedAt", "observedAt")
LABEL_FIELDS = ("name", "title", "text", "docType", "linkType", "role")


def _part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _safe_url(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096 or any(c.isspace() for c in value):
        raise ValueError("invalid_url")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or "\\" in value:
        raise ValueError("invalid_url")
    parsed.port  # Reject malformed ports, without changing the original reference.
    events.http_url(value)
    if sensitive_string(value):
        raise ValueError("credential_url")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True, encoding="utf-8", errors="strict"):
        for _ in range(6):
            lowered = key.lower()
            if lowered in events.CREDENTIAL_QUERY_KEYS or lowered == "key" or SECRET_QUERY.fullmatch(key) or credential_label(key):
                raise ValueError("credential_url")
            decoded = unquote(key, encoding="utf-8", errors="strict")
            if decoded == key:
                break
            key = decoded
        else:
            raise ValueError("credential_url")
    return value


def _safe_metadata(value):
    """Refuse unsafe URLs in copied metadata rather than exposing them in a manifest."""
    pending = [(value, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if visited > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("document discovery metadata exceeds bound")
        if isinstance(current, str):
            for match in events.URL_TOKEN.finditer(current):
                _safe_url(match.group().rstrip(")]},."))
        elif isinstance(current, dict):
            pending.extend((entry, depth + 1) for entry in current.keys())
            pending.extend((entry, depth + 1) for entry in current.values())
        elif isinstance(current, list):
            pending.extend((entry, depth + 1) for entry in current)


def discover(plan):
    """Return private, unverified URL candidates and redacted exclusion reasons."""
    events.validate_content_policy(plan)
    from source_evidence.automatic import _refuse_credential_fields
    _refuse_credential_fields(plan)
    rows = plan["events"]
    result = {"schema_version": "cre-source-document-candidates/v1", "source_key": plan["source_key"],
              "capture_id": rows[0]["capture_id"], "plan_sha256": plan["plan_sha256"],
              "discovery_scope": "explicit_document_channels_and_source_labelled_page_links",
              "coverage": "unverified", "acquisition_enabled": False,
              "candidates": [], "excluded": []}
    by_url = {}
    visited = 0
    occurrence_count = 0
    budget = 1024

    def charge(value):
        nonlocal budget
        size = len(events.encoded(value))
        if size > MAX_OCCURRENCE_BYTES or budget + size + 1 > MAX_MANIFEST_BYTES:
            raise ValueError("document discovery output exceeds bound")
        budget += size + 1

    def emit(reference, item_no, pointer, role, labels, witnesses, base):
        nonlocal occurrence_count
        occurrence_count += 1
        if occurrence_count > MAX_OCCURRENCES or len(pointer.encode("utf-8")) > MAX_POINTER_BYTES:
            raise ValueError("document discovery occurrence exceeds bound")
        resolved = reference
        resolution = {"kind": "absolute_source_reference", "verification": "unverified"}
        try:
            if not isinstance(reference, str) or not reference:
                raise ValueError("invalid_reference")
            parsed = urlsplit(reference)
            if not parsed.scheme:
                if base is None:
                    raise ValueError("relative_without_unambiguous_base")
                _safe_url(base)
                resolved = urljoin(base, reference)
                resolution = {"kind": "relative_reference_resolved_against_source_page", "base": base,
                              "verification": "unverified"}
            _safe_url(resolved)
        except (ValueError, UnicodeError):
            excluded = {"item_no": item_no, "json_pointer": pointer, "reason": "unsafe_or_unresolved_reference"}
            charge(excluded)
            result["excluded"].append(excluded)
            return
        occurrence = {"item_no": item_no, "json_pointer": pointer, "raw_reference": reference,
                      "role": role, "labels": copy.deepcopy(labels), "identity_witnesses": copy.deepcopy(witnesses),
                      "observed_at": rows[item_no]["observed_at"], "resolution": resolution,
                      "identity": "unverified", "fact_kind": fact_kind}
        _safe_metadata(labels)
        _safe_metadata(witnesses)
        charge(occurrence)
        if resolved not in by_url:
            if len(by_url) >= MAX_CANDIDATES:
                raise ValueError("document discovery candidate count exceeds bound")
            candidate = {"url": resolved, "publicness": "unverified", "reachability": "unverified",
                         "format": "unverified", "coverage": "unverified", "fetch_authority": False,
                         "possible_duplicate_bytes": "unverified", "occurrences": []}
            charge({key: value for key, value in candidate.items() if key != "occurrences"})
            by_url[resolved] = candidate
            result["candidates"].append(candidate)
        by_url[resolved]["occurrences"].append(occurrence)

    for row in rows:
        item_no = row["item_no"]
        fact_kind = row["facts"][0]["kind"] if row["facts"] else None
        stack = [(row["facts"], "/facts", 0, [], None, False)]
        while stack:
            value, pointer, depth, witnesses, base, in_page = stack.pop()
            visited += 1
            if visited > MAX_NODES or depth > MAX_DEPTH or len(pointer.encode("utf-8")) > MAX_POINTER_BYTES:
                raise ValueError("document discovery traversal exceeds bound")
            if isinstance(value, dict):
                fields = {key: value[key] for key in IDENTITY_FIELDS if key in value and
                          (value[key] is None or isinstance(value[key], (str, int, float, bool)))}
                if fields:
                    witnesses = [*witnesses, {"json_pointer": pointer, "fields": fields, "verification": "unverified"}]
                sibling = value.get("observation")
                if "event" in value and isinstance(sibling, dict):
                    sibling_fields = {key: sibling[key] for key in IDENTITY_FIELDS if key in sibling and
                                      (sibling[key] is None or isinstance(sibling[key], (str, int, float, bool)))}
                    if sibling_fields:
                        witnesses = [*witnesses, {"json_pointer": pointer + "/observation", "fields": sibling_fields,
                                                   "verification": "unverified"}]
                if isinstance(value.get("schema"), str) and value["schema"] in PAGE_SCHEMAS:
                    in_page = True
                    bases = [value[key] for key in ("sourceUrl", "requestedUrl", "requestedURL") if isinstance(value.get(key), str)]
                    base = bases[0] if bases and all(item == bases[0] for item in bases) else None
                for key in ("brochures", "documents", "links"):
                    channel = value.get(key)
                    if not isinstance(channel, list):
                        continue
                    for index, entry in enumerate(channel):
                        entry_path = f"{pointer}/{_part(key)}/{index}"
                        if isinstance(entry, str) and key in ("brochures", "documents"):
                            emit(entry, item_no, entry_path, key, {}, witnesses, base)
                        elif isinstance(entry, dict):
                            labels = {label: entry[label] for label in LABEL_FIELDS if label in entry}
                            explicit = key != "links" or entry.get("linkType") == "document"
                            if key == "links" and in_page and not explicit:
                                explicit = any(isinstance(labels.get(label), str) and DOCUMENT_LABEL.search(labels[label])
                                               for label in ("name", "title", "text", "role"))
                            if explicit:
                                for url_key in ("url", "href"):
                                    if url_key in entry:
                                        emit(entry[url_key], item_no, f"{entry_path}/{url_key}", key, labels, witnesses, base)
                for key in ("pdf_url", "urlDocument"):
                    if key not in value:
                        continue
                    candidate = value[key]
                    if isinstance(candidate, str):
                        emit(candidate, item_no, f"{pointer}/{_part(key)}", key, {}, witnesses, base)
                    elif isinstance(candidate, list):
                        for index, part in enumerate(candidate):
                            if isinstance(part, str):
                                emit(part, item_no, f"{pointer}/{_part(key)}/{index}", key, {}, witnesses, base)
                for key in sorted(value, reverse=True):
                    stack.append((value[key], f"{pointer}/{_part(key)}", depth + 1, witnesses, base, in_page))
            elif isinstance(value, list):
                for index in range(len(value) - 1, -1, -1):
                    stack.append((value[index], f"{pointer}/{index}", depth + 1, witnesses, base, in_page))
    if len(events.encoded(result)) > MAX_MANIFEST_BYTES:
        raise ValueError("document discovery manifest exceeds bound")
    return result
