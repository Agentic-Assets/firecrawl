"""Narrow, irreversible projection of observed map tile URLs in collector JSON.

The resulting URL is a derived reference and may not be fetchable. The receipt
records where URLs were projected without storing their credential bytes.
No source-content hashes are created. Projection is not a universal secrecy guarantee.
The caller must still run all ordinary content and source-policy checks.
"""
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

from buildout_observation_artifact import URL, pointer_component

VERSION = "faris-google-map-tile-query/v1"
SOURCE_VERSIONS = {"faris-lee": VERSION,
                   "cushman-wakefield": "google-map-tile-query/v1",
                   "nai-global": "google-map-tile-query/v1",
                   "svn": "google-map-tile-query/v1"}
MAX_NODES = 500_000
MAX_RECEIPTS = 20_000
MAX_POINTER_BYTES = 4096
MAX_RECEIPT_BYTES = 1_000_000
MAX_REMOVED_PARAMETERS = 128
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def version_for(source_key):
    """Return the explicitly supported version; policies must still opt in."""
    return SOURCE_VERSIONS.get(source_key)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()



def _url_boundary(value):
    """Split punctuation used as surrounding text, consistently on both paths."""
    trimmed = value.rstrip(")]},.")
    return trimmed, value[len(trimmed):]


def _extend_pointer(path, component):
    if len(component) > MAX_POINTER_BYTES:
        raise ValueError("projection pointer exceeds bound")
    candidate = path + "/" + pointer_component(component)
    if len(candidate.encode("utf-8")) > MAX_POINTER_BYTES:
        raise ValueError("projection pointer exceeds bound")
    return candidate


def _entry_size(entry):
    return len(json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 1

def project(document, *, source_key, _observation_version=None):
    """Return a new artifact and path receipts; do not mutate the input.

    Only the observed HTTPS map tile endpoint and literal key/token query names
    are projected. Other content is left to the existing admission checks.
    """
    version = version_for(source_key)
    if source_key in {"leibsohn", "cawley-chicago", "bradford-allen"} and _observation_version == f"{source_key}-observation-map/v1":
        version = _observation_version
    if version is None:
        raise ValueError("map projection is not authorized for this source")
    receipts = []
    visited = 0
    receipt_bytes = 512

    def walk(value, path, depth):
        nonlocal visited
        visited += 1
        if visited > MAX_NODES or depth > 128:
            raise ValueError("map projection exceeds structural bounds")
        if isinstance(value, dict):
            return {k: walk(v, _extend_pointer(path, k), depth + 1)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, _extend_pointer(path, str(i)), depth + 1) for i, v in enumerate(value)]
        if not isinstance(value, str):
            return value
        occurrence = 0

        def replace(match):
            nonlocal occurrence, receipt_bytes
            raw, suffix = _url_boundary(match.group(0))
            try:
                parts = urlsplit(raw)
                host = parts.hostname
            except ValueError:
                return match.group(0)
            if host is None or host.lower().rstrip(".") != "maps.googleapis.com":
                return match.group(0)
            if (parts.scheme != "https" or parts.netloc != "maps.googleapis.com"
                    or parts.path != "/maps/vt" or parts.fragment
                    or _BAD_ESCAPE.search(parts.query)):
                if source_key == "faris-lee":
                    raise ValueError("unsupported map URL requires separate handling")
                # New source policies project only the observed tile endpoint.
                # Other references stay unchanged for normal admission checks.
                return match.group(0)
            pieces = parts.query.split("&")
            kept, removed = [], []
            for piece in pieces:
                name = piece.partition("=")[0]
                decoded = unquote(name, errors="strict")
                if decoded.lower() in {"key", "token"}:
                    if name not in {"key", "token"} or "=" not in piece:
                        raise ValueError("ambiguous map credential parameter")
                    value = piece.partition("=")[2]
                    pattern = r"[A-Za-z0-9_-]+" if name == "key" else r"[0-9]+"
                    if not re.fullmatch(pattern, value):
                        raise ValueError("unsupported map credential value")
                    if len(removed) >= MAX_REMOVED_PARAMETERS:
                        raise ValueError("map parameter count exceeds bound")
                    removed.append(name)
                else:
                    kept.append(piece)
            current = occurrence
            occurrence += 1
            if not removed:
                return match.group(0)
            if len(receipts) >= MAX_RECEIPTS:
                raise ValueError("map projection exceeds receipt bound")
            projected = raw.split("?", 1)[0] + ("?" + "&".join(kept) if kept else "")
            entry = {"pointer": path, "occurrence": current,
                     "removedParameters": removed, "projectedUrlSha256": _digest(projected)}
            receipt_bytes += _entry_size(entry)
            if receipt_bytes > MAX_RECEIPT_BYTES:
                raise ValueError("map projection receipt exceeds byte bound")
            receipts.append(entry)
            return projected + suffix

        return URL.sub(replace, value)

    result = walk(document, "", 0)
    return result, {"version": version, "sourceKey": source_key, "replacements": receipts}


def canonical_digest(document, numeric_literals=None):
    """Hash canonical JSON while preserving exceptional numeric source literals."""
    from source_evidence import acquisition, events

    raw = events.encoded(document)
    if numeric_literals is not None:
        raw = acquisition.encode_json(json.loads(raw), numeric_literals).encode("utf-8")
        if acquisition.decode_json(raw) != (document, numeric_literals):
            raise ValueError("invalid projection numeric literals")
    return hashlib.sha256(raw).hexdigest()


def prepare(raw, *, source_key):
    """Decode source bytes and return the projected object, literal map, receipt."""
    from source_evidence import acquisition

    document, literals = acquisition.decode_json(raw)
    projected, receipt = project(document, source_key=source_key)
    receipt["projectedCanonicalSha256"] = canonical_digest(projected, literals or None)
    return projected, literals, receipt


def validate(document, receipt, *, source_key, numeric_literals=None, _observation_version=None):
    """Validate projected representation and references without source-content hashes."""
    expected_version = version_for(source_key)
    if source_key in {"leibsohn", "cawley-chicago", "bradford-allen"} and _observation_version == f"{source_key}-observation-map/v1":
        expected_version = _observation_version
    if (not isinstance(receipt, dict) or set(receipt) != {
        "version", "sourceKey", "replacements", "projectedCanonicalSha256"}
        or expected_version is None or receipt["version"] != expected_version
        or receipt["sourceKey"] != source_key
        or not isinstance(receipt["projectedCanonicalSha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["projectedCanonicalSha256"])
        or not isinstance(receipt["replacements"], list) or len(receipt["replacements"]) > MAX_RECEIPTS
        or receipt["projectedCanonicalSha256"] != canonical_digest(document, numeric_literals)):
        raise ValueError("invalid map projection receipt")
    receipt_bytes = 512
    for entry in receipt["replacements"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("pointer"), str)
                or len(entry["pointer"]) > MAX_POINTER_BYTES
                or len(entry["pointer"].encode("utf-8")) > MAX_POINTER_BYTES
                or not isinstance(entry.get("removedParameters"), list)
                or len(entry["removedParameters"]) > MAX_REMOVED_PARAMETERS):
            raise ValueError("map projection receipt exceeds bounds")
        # Reject unknown large fields before serializing an untrusted entry.
        if set(entry) != {"pointer", "occurrence", "removedParameters", "projectedUrlSha256"}:
            raise ValueError("invalid map replacement receipt")
        if (type(entry.get("occurrence")) is not int or entry["occurrence"] < 0
                or entry["occurrence"] > MAX_NODES
                or any(name not in ("key", "token") for name in entry["removedParameters"])
                or not isinstance(entry.get("projectedUrlSha256"), str)
                or len(entry["projectedUrlSha256"]) != 64):
            raise ValueError("invalid map replacement receipt")
        receipt_bytes += _entry_size(entry)
        if receipt_bytes > MAX_RECEIPT_BYTES:
            raise ValueError("map projection receipt exceeds byte bound")
    reprojected, check = project(document, source_key=source_key, _observation_version=_observation_version)
    if reprojected != document or check["replacements"]:
        raise ValueError("projected artifact still contains map credentials")
    used = set()
    url_cache = {}
    for entry in receipt["replacements"]:
        if (not isinstance(entry, dict) or set(entry) != {"pointer", "occurrence", "removedParameters",
                "projectedUrlSha256"}
                or not isinstance(entry["pointer"], str) or not entry["pointer"].startswith("/")
                or type(entry["occurrence"]) is not int or entry["occurrence"] < 0
                or not isinstance(entry["removedParameters"], list) or not entry["removedParameters"]
                or any(name not in ("key", "token") for name in entry["removedParameters"])
                or any(not isinstance(entry[k], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[k])
                       for k in ("projectedUrlSha256",))):
            raise ValueError("invalid map replacement receipt")
        identity = (entry["pointer"], entry["occurrence"])
        if identity in used:
            raise ValueError("duplicate map replacement receipt")
        used.add(identity)
        pointer = entry["pointer"]
        if pointer not in url_cache:
            value = document
            for component in entry["pointer"][1:].split("/"):
                if re.search(r"~(?![01])", component):
                    raise ValueError("invalid projection pointer")
                component = component.replace("~1", "/").replace("~0", "~")
                if isinstance(value, dict) and component in value:
                    value = value[component]
                elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", component) and int(component) < len(value):
                    value = value[int(component)]
                else:
                    raise ValueError("projection pointer does not resolve")
            if not isinstance(value, str):
                raise ValueError("projection pointer does not address text")
            matches = []
            for match in URL.finditer(value):
                text, _ = _url_boundary(match.group(0))
                try:
                    parts = urlsplit(text)
                    if (parts.scheme == "https" and parts.netloc == "maps.googleapis.com"
                            and parts.path == "/maps/vt" and not parts.fragment
                            and not _BAD_ESCAPE.search(parts.query)):
                        matches.append(text)
                except ValueError:
                    continue
            url_cache[pointer] = matches
        matches = url_cache[pointer]
        if entry["occurrence"] >= len(matches) or _digest(matches[entry["occurrence"]]) != entry["projectedUrlSha256"]:
            raise ValueError("projected map URL does not match receipt")
