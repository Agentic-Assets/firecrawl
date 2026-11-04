"""Discover unverified document references from a validated evidence plan.

Discovery performs no fetch, source promotion, identity resolution or admission.
"""
import copy
import re
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from buildout_observation_artifact import (
    SECRET_QUERY,
    credential_label,
    sensitive_string,
)

from source_evidence import events

MAX_NODES = 500000
MAX_DEPTH = 128
MAX_OCCURRENCES = 10000
MAX_CANDIDATES = 10000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_POINTER_BYTES = 4096
MAX_OCCURRENCE_BYTES = 65536
PAGE_SCHEMAS = {"cre-source-page-context/v1", "foundry-native-page-context/v1"}
ENRICHMENT_SCHEMAS = {"cre-generic-enrichment-observation/v1", "cre-buildout-enrichment-observation/v1"}
SCRAPE_URL_ATTRIBUTES = {"href", "src", "url", "data-video-url"}
DOCUMENT_LABEL = re.compile(r"\b(?:brochures?|offering\s+memorand(?:um|a)|om|flyers?|floor\s*plans?|rent\s*rolls?|"
                            r"risk\s*questionnaires?|financials?|financial\s+statements?|property\s+packages?|documents?)\b", re.IGNORECASE)
IDENTITY_FIELDS = ("id", "externalId", "external_id", "foundryPostId", "propertyId", "sourceKey",
                   "url", "sourceUrl", "requestedUrl", "requestedURL", "inventoryObservedAt", "detailObservedAt", "observedAt",
                   "transaction", "capturedAt", "captureClock", "qualification")
LABEL_FIELDS = ("name", "title", "text", "docType", "linkType", "role", "aria-label", "download", "type", "rel")
DOCUMENT_SUFFIXES = frozenset({".pdf", ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot", ".pptx",
                               ".pptm", ".ppsx", ".ppsm", ".xls", ".xlsx", ".xlsm", ".xlsb",
                               ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv"})
DOCUMENT_MIME = frozenset({
    "application/pdf", "application/msword", "application/rtf", "text/rtf", "text/csv",
    "application/epub+zip", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
})


def _document_suffix(reference):
    if not isinstance(reference, str):
        return False
    try:
        path = urlsplit(reference).path.lower()
    except ValueError:
        return False
    return any(path.endswith(suffix) for suffix in DOCUMENT_SUFFIXES)


def _document_mime(value):
    return isinstance(value, str) and value.split(";", 1)[0].strip().lower() in DOCUMENT_MIME


def _page_base(page):
    if not isinstance(page, dict) or not isinstance(page.get("schema"), str) or page["schema"] not in PAGE_SCHEMAS:
        return None
    bases = [page[key] for key in ("sourceUrl", "requestedUrl", "requestedURL") if isinstance(page.get(key), str)]
    return bases[0] if bases and all(item == bases[0] for item in bases) else None


def _part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _safe_url(value, *, allow_fragment=False):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("invalid_url")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or ("#" in value and not allow_fragment) or "\\" in value):
        raise ValueError("invalid_url")
    _ = parsed.port  # Reject malformed ports, without changing the original reference.
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


def _safe_metadata(value, *, decoded_source_key=None):
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
                url = _safe_url(match.group().rstrip(")]},."), allow_fragment=True)
                if decoded_source_key is not None:
                    host = (urlsplit(url).hostname or "").lower().rstrip(".")
                    if host in ("jll.com", "jllproperty.com") or host.endswith((".jll.com", ".jllproperty.com")):
                        raise ValueError("JLL URL requires its source-specific withheld-content adapter")
        elif isinstance(current, dict):
            if decoded_source_key is not None:
                for key, item in current.items():
                    if (isinstance(key, str) and re.sub(r"[^a-z0-9]", "", key.lower()) == "sourcekey"
                            and isinstance(item, str)):
                        declared = item.strip().lower().replace("_", "-")
                        if declared in ("jll", "jll-main", "jll-investor", "avison-young") and declared != decoded_source_key:
                            raise ValueError("conflicting protected source declaration")
            pending.extend((entry, depth + 1) for entry in current)
            pending.extend((entry, depth + 1) for entry in current.values())
        elif isinstance(current, list):
            pending.extend((entry, depth + 1) for entry in current)


def _witness_at(path, fields, script_source):
    witness = {"json_pointer": script_source["pointer"] if script_source else path,
               "fields": fields, "verification": "unverified"}
    if script_source is not None:
        witness["script_json_pointer"] = path
        if script_source["numeric_literals"]:
            witness["script_numeric_literals"] = copy.deepcopy(script_source["numeric_literals"])
            witness["script_numeric_encoding"] = "json_pointer_to_literal_null_placeholder_v1"
    return witness


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

    def diagnostic(item_no, pointer, reason, script_source=None):
        nonlocal occurrence_count
        occurrence_count += 1
        if (occurrence_count > MAX_OCCURRENCES or len(pointer.encode("utf-8")) > MAX_POINTER_BYTES
                or (script_source is not None and len(script_source["pointer"].encode("utf-8")) > MAX_POINTER_BYTES)):
            raise ValueError("document discovery occurrence exceeds bound")
        entry = {"item_no": item_no, "json_pointer": script_source["pointer"] if script_source else pointer, "reason": reason}
        if script_source is not None:
            entry["script_json_pointer"] = pointer
        charge(entry)
        result["excluded"].append(entry)

    def emit(reference, item_no, pointer, role, labels, witnesses, base, script_source=None):
        nonlocal occurrence_count
        occurrence_count += 1
        if (occurrence_count > MAX_OCCURRENCES or len(pointer.encode("utf-8")) > MAX_POINTER_BYTES
                or (script_source is not None and len(script_source["pointer"].encode("utf-8")) > MAX_POINTER_BYTES)):
            raise ValueError("document discovery occurrence exceeds bound")
        resolved = reference
        resolution = {"kind": "absolute_source_reference", "verification": "unverified"}
        try:
            if not isinstance(reference, str) or not reference or "#" in reference:
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
            excluded = {"item_no": item_no, "json_pointer": script_source["pointer"] if script_source else pointer,
                        "reason": "unsafe_or_unresolved_reference"}
            if script_source is not None:
                excluded["script_json_pointer"] = pointer
            charge(excluded)
            result["excluded"].append(excluded)
            return
        occurrence = {"item_no": item_no, "json_pointer": script_source["pointer"] if script_source else pointer, "raw_reference": reference,
                      "role": role, "labels": copy.deepcopy(labels), "identity_witnesses": copy.deepcopy(witnesses),
                      "observed_at": rows[item_no]["observed_at"], "resolution": resolution,
                      "identity": "unverified", "fact_kind": fact_kind}
        if script_source is not None:
            occurrence["script_json_pointer"] = pointer
            if script_source["numeric_literals"]:
                occurrence["script_numeric_literals"] = copy.deepcopy(script_source["numeric_literals"])
                occurrence["script_numeric_encoding"] = "json_pointer_to_literal_null_placeholder_v1"
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
        stack = [(row["facts"], "/facts", 0, [], None, False, False, None, False)]
        while stack:
            value, pointer, depth, witnesses, base, in_page, is_scraped_doc, script_source, page_complete = stack.pop()
            visited += 1
            if visited > MAX_NODES or depth > MAX_DEPTH or len(pointer.encode("utf-8")) > MAX_POINTER_BYTES:
                raise ValueError("document discovery traversal exceeds bound")
            if isinstance(value, dict):
                fields = {key: value[key] for key in IDENTITY_FIELDS if key in value and
                          (value[key] is None or isinstance(value[key], (str, int, float, bool)))}
                if fields:
                    witnesses = [*witnesses, _witness_at(pointer, fields, script_source)]
                sibling = value.get("observation")
                if "event" in value and isinstance(sibling, dict):
                    sibling_fields = {key: sibling[key] for key in IDENTITY_FIELDS if key in sibling and
                                      (sibling[key] is None or isinstance(sibling[key], (str, int, float, bool)))}
                    if sibling_fields:
                        witnesses = [*witnesses, _witness_at(pointer + "/observation", sibling_fields, script_source)]
                is_enrichment = isinstance(value.get("schema"), str) and value["schema"] in ENRICHMENT_SCHEMAS
                if (is_enrichment
                        and isinstance(value.get("claim"), dict)):
                    claim_fields = {key: value["claim"][key] for key in IDENTITY_FIELDS if key in value["claim"] and
                                    (value["claim"][key] is None or isinstance(value["claim"][key], (str, int, float, bool)))}
                    if claim_fields:
                        witnesses = [*witnesses, _witness_at(pointer + "/claim", claim_fields, script_source)]
                    requested = value.get("requestedPageUrl")
                    if isinstance(requested, str):
                        witnesses = [*witnesses, _witness_at(pointer, {"requestedPageUrl": requested}, script_source)]
                if isinstance(value.get("schema"), str) and value["schema"] in PAGE_SCHEMAS:
                    in_page = True
                    base = _page_base(value)
                    omissions = value.get("omissions")
                    page_complete = (isinstance(omissions, dict) and type(omissions.get("characters")) is int
                                     and omissions["characters"] == 0)
                    scripts = value.get("scripts")
                    if script_source is None and isinstance(scripts, list):
                        from source_evidence import acquisition
                        for index, script in enumerate(scripts):
                            script_path = f"{pointer}/scripts/{index}/text"
                            if not isinstance(script, dict) or not isinstance(script.get("type"), str):
                                continue
                            if script["type"].split(";", 1)[0].strip().lower() not in ("application/json", "application/ld+json"):
                                continue
                            complete = script.get("textComplete") is True or ("textComplete" not in script and page_complete)
                            if not complete or not isinstance(script.get("text"), str):
                                diagnostic(item_no, script_path, "script_text_incomplete_or_unverified")
                                continue
                            try:
                                raw = script["text"].encode("utf8")
                                decoded, literals = acquisition.decode_json(raw, max_bytes=256 * 1024)
                            except (ValueError, UnicodeError):
                                diagnostic(item_no, script_path, "script_json_invalid_or_unsupported")
                                continue
                            _refuse_credential_fields(decoded)
                            _safe_metadata(decoded, decoded_source_key=plan["source_key"])
                            stack.append((decoded, "", depth + 1, witnesses, base, False, False,
                                          {"pointer": script_path, "numeric_literals": literals}, False))
                for key in ("brochures", "documents", "links", "documentLinks"):
                    channel = value.get(key)
                    if not isinstance(channel, list) or (key == "documentLinks" and not in_page):
                        continue
                    for index, entry in enumerate(channel):
                        entry_path = f"{pointer}/{_part(key)}/{index}"
                        if isinstance(entry, str) and key in ("brochures", "documents"):
                            emit(entry, item_no, entry_path, key, {}, witnesses, base, script_source)
                        elif isinstance(entry, str) and key == "links" and _document_suffix(entry):
                            if in_page and not page_complete:
                                diagnostic(item_no, entry_path, "reference_incomplete_or_unverified", script_source)
                            else:
                                emit(entry, item_no, entry_path, key, {}, witnesses, base, script_source)
                        elif isinstance(entry, dict):
                            labels = {label: entry[label] for label in LABEL_FIELDS if label in entry}
                            integrity = entry.get("referenceIntegrity")
                            if in_page and key in ("links", "documentLinks"):
                                status = integrity.get("href") if isinstance(integrity, dict) else None
                                if status == "truncated" or (entry.get("href") is not None and status != "complete" and not page_complete):
                                    diagnostic(item_no, f"{entry_path}/href", "reference_incomplete_or_unverified", script_source)
                                    continue
                            explicit = key in ("brochures", "documents") or entry.get("linkType") == "document"
                            if key in ("links", "documentLinks") and not explicit:
                                explicit = any(isinstance(labels.get(label), str) and DOCUMENT_LABEL.search(labels[label])
                                               for label in ("name", "title", "text", "role", "aria-label"))
                                explicit = (explicit or _document_mime(entry.get("type"))
                                            or (key == "links" and isinstance(entry.get("download"), str))
                                            or any(_document_suffix(entry.get(name)) for name in ("url", "href")))
                            if explicit:
                                for url_key in ("url", "href"):
                                    if url_key in entry:
                                        if in_page and key in ("links", "documentLinks") and url_key == "url":
                                            status = integrity.get("url") if isinstance(integrity, dict) else None
                                            if status == "truncated" or (entry[url_key] is not None and status != "complete" and not page_complete):
                                                diagnostic(item_no, f"{entry_path}/url", "reference_incomplete_or_unverified", script_source)
                                                continue
                                        emit(entry[url_key], item_no, f"{entry_path}/{url_key}", key, labels, witnesses, base, script_source)
                attributes = value.get("attributes")
                if is_scraped_doc and isinstance(attributes, list):
                    for index, block in enumerate(attributes):
                        if (not isinstance(block, dict) or not isinstance(block.get("selector"), str)
                                or not isinstance(block.get("attribute"), str)
                                or block["attribute"].lower() not in SCRAPE_URL_ATTRIBUTES
                                or not isinstance(block.get("values"), list)):
                            continue
                        labels = {key: block[key] for key in ("selector", "attribute") if key in block}
                        for offset, reference in enumerate(block["values"]):
                            if _document_suffix(reference):
                                emit(reference, item_no, f"{pointer}/attributes/{index}/values/{offset}",
                                     "attributes", labels, witnesses, base, script_source)
                for key in ("pdf_url", "urlDocument"):
                    if key not in value:
                        continue
                    candidate = value[key]
                    if isinstance(candidate, str):
                        emit(candidate, item_no, f"{pointer}/{_part(key)}", key, {}, witnesses, base, script_source)
                    elif isinstance(candidate, list):
                        for index, part in enumerate(candidate):
                            if isinstance(part, str):
                                emit(part, item_no, f"{pointer}/{_part(key)}/{index}", key, {}, witnesses, base, script_source)
                for key in sorted(value, reverse=True):
                    scraped_child = is_enrichment and key == "scrapedDoc" and isinstance(value[key], dict)
                    child_base = _page_base(value.get("pageContext")) if scraped_child else base
                    child_witnesses = witnesses
                    if scraped_child and isinstance(value.get("pageContext"), dict):
                        page = value["pageContext"]
                        if isinstance(page.get("schema"), str) and page["schema"] in PAGE_SCHEMAS:
                            page_fields = {name: page[name] for name in IDENTITY_FIELDS if name in page and
                                           (page[name] is None or isinstance(page[name], (str, int, float, bool)))}
                            if page_fields:
                                child_witnesses = [*witnesses, _witness_at(pointer + "/pageContext", page_fields, script_source)]
                    stack.append((value[key], f"{pointer}/{_part(key)}", depth + 1, child_witnesses,
                                  child_base, in_page, scraped_child, script_source, page_complete))
            elif isinstance(value, list):
                for index in range(len(value) - 1, -1, -1):
                    stack.append((value[index], f"{pointer}/{index}", depth + 1, witnesses, base, in_page, False,
                                  script_source, page_complete))
    if len(events.encoded(result)) > MAX_MANIFEST_BYTES:
        raise ValueError("document discovery manifest exceeds bound")
    return result
