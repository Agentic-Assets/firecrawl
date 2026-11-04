"""Irreversible, source-owned projections for Buildout page observations.

Receipts describe only the projected representation. Authenticate the receipt
separately; a null alone cannot prove the original value existed.
"""
import re
from collections import Counter
from html import unescape
from urllib.parse import parse_qsl, unquote, urlsplit

from source_evidence import acquisition, buildout_projection, events, map_projection

VERSIONS = {
    "leibsohn": "leibsohn-page-observation-withholding/v1",
    "cawley-chicago": "cawley-chicago-page-observation-withholding/v1",
    "bradford-allen": "bradford-allen-page-observation-withholding/v1",
}
MAP_VERSIONS = {source: f"{source}-observation-map/v1" for source in VERSIONS}


def version_for(source_key):
    return VERSIONS.get(source_key) if isinstance(source_key, str) else None

MAX_BYTES = 16 * 1024 * 1024
MAX_RECEIPTS = 20_000
METADATA_LABEL = "quokka-content-current-token"
METADATA_PATH = "evidence/pageContext/metadata"
PATHS = (
    ("input/context/nativeContext/pdf_url", "document_reference"),
    ("input/context/originalContext/nativeContext/pdf_url", "document_reference"),
    ("input/context/replay/sdkCaptureSha256", "original_capture_hash"),
    ("evidence/pageContext/tables/provenance/inputSha256", "original_html_hash"),
    ("evidence/scrapedDoc/metadata/quokka-content-current-token", "token_named_metadata"),
)


def _groups(document, source_key):
    if version_for(source_key) is None or not isinstance(document, dict):
        raise ValueError("unsupported observation projection")
    meta, sources = document.get("runMeta"), document.get("sources")
    if (not isinstance(meta, dict) or meta.get("mode") != "observe" or meta.get("evidenceOnly") is not True
            or document.get("listings") != [] or document.get("brokers") != [] or document.get("totalListings") != 0
            or not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict)
            or sources[0].get("sourceKey") != source_key
            or not isinstance(sources[0].get("retainedObservations"), list)
            or len(sources[0]["retainedObservations"]) > 100):
        raise ValueError("invalid observation projection scope")
    return sources[0]["retainedObservations"]


def _target(row, path):
    fields = path.split("/")
    value = row
    for field in fields[:-1]:
        if value is None:
            return None, None
        if not isinstance(value, dict):
            return None, None
        if field not in value:
            return None, None
        value = value[field]
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, None
    return value, fields[-1]


def _token_metadata_entries(row):
    container, key = _target(row, METADATA_PATH)
    entries = container.get(key) if container is not None else None
    return entries if isinstance(entries, list) else []


def _is_token_metadata(entry):
    return isinstance(entry, dict) and any(
        isinstance(entry.get(key), str) and entry[key].casefold() == METADATA_LABEL
        for key in ("name", "property")
    )


def _refuse_ambiguous_map_references(document):
    """Refuse only known-map credential references outside the reviewed grammar."""
    sensitive = events.CREDENTIAL_QUERY_KEYS | {"key"}

    def inspect(url):
        try:
            parts = urlsplit(url)
            names = Counter(name.lower() for query in (parts.query, parts.fragment, *parts.fragment.split("?")[1:])
                            for name, _ in parse_qsl(query, keep_blank_values=True)
                            if name.lower() in sensitive)
            return parts, names
        except ValueError:
            return None, Counter()

    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            original = Counter(match.group().rstrip(".,;:!?)]}") for match in events.URL_TOKEN.finditer(value))
            decoded_html = unescape(unescape(value))
            for match in events.URL_TOKEN.finditer(decoded_html):
                url = match.group().rstrip(".,;:!?)]}")
                parts, names = inspect(url)
                if parts and (parts.hostname or "").lower().rstrip(".") == "maps.googleapis.com" and names:
                    if (original[url] == 0 or parts.scheme != "https" or parts.netloc != "maps.googleapis.com"
                            or parts.path != "/maps/vt" or parts.fragment or re.search(r"%(?![0-9a-fA-F]{2})", url)):
                        raise ValueError("unsupported map credential reference")
                    original[url] -= 1
            def decoded_credential_counts(text):
                counts = Counter()
                for match in events.URL_TOKEN.finditer(text):
                    parts, names = inspect(match.group().rstrip(".,;:!?)]}"))
                    if parts and (parts.hostname or "").lower().rstrip(".") == "maps.googleapis.com":
                        counts.update(names)
                return counts

            initial_counts = decoded_credential_counts(decoded_html)
            decoded_text = decoded_html
            for _ in range(2):
                decoded_text = unquote(decoded_text)
                decoded_counts = decoded_credential_counts(decoded_text)
                if any(count > initial_counts[name] for name, count in decoded_counts.items()):
                    raise ValueError("ambiguous encoded map credential reference")
            for url in (match.group().rstrip(".,;:!?)]}") for match in events.URL_TOKEN.finditer(value)):
                initial, _ = inspect(url)
                if (initial and (initial.hostname or "").lower().rstrip(".") == "maps.googleapis.com"
                        and re.search(r"%(?![0-9a-fA-F]{2})", url)):
                    raise ValueError("malformed map reference")
                decoded = url
                initial_parts, initial_names = inspect(decoded)
                for _ in range(2):
                    decoded = unquote(unescape(decoded))
                    parts, names = inspect(decoded)
                    if not parts or (parts.hostname or "").lower().rstrip(".") != "maps.googleapis.com" or not names:
                        continue
                    if (not initial_parts or (initial_parts.hostname or "").lower().rstrip(".") != "maps.googleapis.com"
                            or any(count > initial_names[name] for name, count in names.items())):
                        raise ValueError("ambiguous encoded map credential reference")


def project(document, *, source_key, numeric_literals=None):
    _groups(document, source_key)
    buildout_projection._bounded(document)
    try:
        if len(events.encoded(document)) > MAX_BYTES:
            raise ValueError("observation projection exceeds bound")
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid observation projection") from None
    _refuse_ambiguous_map_references(document)
    mapped, map_receipt = map_projection.project(document, source_key=source_key, _observation_version=MAP_VERSIONS[source_key])
    rows = _groups(mapped, source_key)
    withheld = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("invalid observation projection row")  # noqa: TRY004
        for path, reason in PATHS:
            target, key = _target(row, path)
            if target is None or key not in target or target[key] is None:
                continue
            value = target[key]
            if reason == "document_reference":
                if not buildout_projection._recognized(value):
                    continue
            elif not isinstance(value, str) or not value:
                raise ValueError("invalid observation withholding value")
            elif reason in ("original_capture_hash", "original_html_hash") and not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("invalid observation hash witness")
            pointer = f"/sources/0/retainedObservations/{index}/{path}"
            if numeric_literals and any(p == pointer or p.startswith(pointer + "/") or pointer.startswith(p + "/") for p in numeric_literals):
                raise ValueError("observation withholding conflicts with numeric literal")
            target[key] = None
            withheld.append({"pointer": pointer, "recordPointer": f"/sources/0/retainedObservations/{index}", "reason": reason})
            if len(withheld) > MAX_RECEIPTS:
                raise ValueError("observation withholding exceeds bound")
        for meta_index, entry in enumerate(_token_metadata_entries(row)):
            if not _is_token_metadata(entry) or entry.get("content") is None or "content" not in entry:
                continue
            if not isinstance(entry["content"], str):
                continue
            pointer = f"/sources/0/retainedObservations/{index}/{METADATA_PATH}/{meta_index}/content"
            if numeric_literals and any(p == pointer or p.startswith(pointer + "/") or pointer.startswith(p + "/") for p in numeric_literals):
                raise ValueError("observation withholding conflicts with numeric literal")
            entry["content"] = None
            withheld.append({"pointer": pointer, "recordPointer": f"/sources/0/retainedObservations/{index}", "reason": "token_named_page_metadata"})
            if len(withheld) > MAX_RECEIPTS:
                raise ValueError("observation withholding exceeds bound")
    digest = map_projection.canonical_digest(mapped, numeric_literals or None)
    map_receipt["projectedCanonicalSha256"] = digest
    receipt = {"version": VERSIONS[source_key], "sourceKey": source_key, "withheld": withheld, "map": map_receipt,
               "projectedCanonicalSha256": digest}
    if len(events.encoded(receipt)) > 1_000_000:
        raise ValueError("observation receipt exceeds bound")
    return mapped, receipt


def prepare(raw, *, source_key):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("observation projection input exceeds bound")
    document, literals = acquisition.decode_json(raw, max_bytes=MAX_BYTES)
    projected, receipt = project(document, source_key=source_key, numeric_literals=literals)
    return projected, literals, receipt


def validate(document, receipt, *, source_key, numeric_literals=None):
    rows = _groups(document, source_key)
    if (not isinstance(receipt, dict) or set(receipt) != {"version", "sourceKey", "withheld", "map", "projectedCanonicalSha256"}
            or receipt.get("version") != VERSIONS[source_key] or receipt.get("sourceKey") != source_key
            or not isinstance(receipt.get("withheld"), list) or len(receipt["withheld"]) > MAX_RECEIPTS
            or not isinstance(receipt.get("projectedCanonicalSha256"), str)
            or receipt["projectedCanonicalSha256"] != map_projection.canonical_digest(document, numeric_literals)):
        raise ValueError("invalid observation projection receipt")
    if len(events.encoded(receipt)) > 1_000_000:
        raise ValueError("observation receipt exceeds bound")
    if not isinstance(receipt.get("map"), dict) or receipt["map"].get("projectedCanonicalSha256") != receipt["projectedCanonicalSha256"]:
        raise ValueError("invalid observation map binding")
    map_projection.validate(document, receipt["map"], source_key=source_key, numeric_literals=numeric_literals,
                            _observation_version=MAP_VERSIONS[source_key])
    expected = []
    allowed = {path: reason for path, reason in PATHS}
    for entry in receipt["withheld"]:
        if not isinstance(entry, dict) or set(entry) != {"pointer", "recordPointer", "reason"}:
            raise ValueError("invalid observation withholding receipt")
        match = re.fullmatch(r"/sources/0/retainedObservations/(0|[1-9][0-9]*)/(.+)", str(entry.get("pointer", "")))
        if not match or len(match[1]) > 3 or int(match[1]) >= len(rows):
            raise ValueError("invalid observation withholding pointer")
        index, path = int(match[1]), match[2]
        if entry.get("recordPointer") != f"/sources/0/retainedObservations/{index}":
            raise ValueError("invalid observation record pointer")
        if numeric_literals and any(p == entry["pointer"] or p.startswith(entry["pointer"] + "/") or entry["pointer"].startswith(p + "/") for p in numeric_literals):
            raise ValueError("observation withholding conflicts with numeric literal")
        meta = re.fullmatch(r"evidence/pageContext/metadata/(0|[1-9][0-9]*)/content", path)
        if meta:
            if len(meta[1]) > 5 or entry.get("reason") != "token_named_page_metadata":
                raise ValueError("invalid observation withholding pointer")
            entries = _token_metadata_entries(rows[index])
            meta_index = int(meta[1])
            if meta_index >= len(entries) or not _is_token_metadata(entries[meta_index]):
                raise ValueError("invalid observation metadata label")
            target, key = entries[meta_index], "content"
            order = (index, len(allowed), meta_index)
        else:
            if allowed.get(path) != entry.get("reason"):
                raise ValueError("invalid observation withholding pointer")
            target, key = _target(rows[index], path)
            order = (index, list(allowed).index(path), 0)
        if target is None or key not in target or target[key] is not None:
            raise ValueError("observation withholding target mismatch")
        if expected and order <= expected[-1]:
            raise ValueError("invalid observation withholding order")
        expected.append(order)
    reprojected, check = project(document, source_key=source_key, numeric_literals=numeric_literals)
    if reprojected != document or check["withheld"] or check["map"]["replacements"]:
        raise ValueError("observation projection is not idempotent")
