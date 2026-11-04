"""Irreversibly withhold a narrowly witnessed Buildout document reference.

The null and receipt describe a withheld value, never a fetchable replacement.
Unknown credential-bearing content is left unchanged for ordinary admission
checks. A receipt must be independently authenticated by its caller: a null
alone cannot prove whether an original value was present.
"""
import copy
import re
from urllib.parse import urlsplit

from source_evidence import acquisition, buildout_scope, events, map_projection

VERSION = "buildout-document-reference-withholding/v1"
REASON = "credential-bearing-document-reference"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_NODES = 500_000
MAX_DEPTH = 128
MAX_ROWS = 10_000
MAX_RECEIPTS = 20_000
MAX_RECEIPT_BYTES = 1_000_000
MAX_URL_BYTES = 4096
MAX_POINTER_BYTES = 4096
PATH = re.compile(r"/docs/[A-Za-z0-9_-]+/[0-9]+\Z")
QUERY = re.compile(r"token=[A-Za-z0-9_-]+\Z")
COLLECTOR_POINTER = re.compile(r"/listings/(0|[1-9][0-9]*)/brochures/(0|[1-9][0-9]*)/url\Z")
NATIVE_POINTER = re.compile(r"/inventory/(0|[1-9][0-9]*)/pdf_url\Z")


def _scope(source_key, representation):
    if (not isinstance(source_key, str) or source_key not in buildout_scope.supported_feed_keys()
            or representation not in ("collector", "native")):
        raise ValueError("unsupported Buildout projection scope")


def _bounded(value):
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("Buildout projection exceeds structural bound")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _recognized(value):
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("utf8")
    except UnicodeError:
        raise ValueError("invalid Buildout document reference encoding") from None
    if len(encoded) > MAX_URL_BYTES:
        raise ValueError("Buildout document reference exceeds bound")
    if not value or any(ord(ch) < 32 or ch.isspace() for ch in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.netloc == "buildout.com" and not url.fragment
                and bool(PATH.fullmatch(url.path)) and bool(QUERY.fullmatch(url.query)))
    except ValueError:
        return False


def _check_receipt_size(receipt):
    for entry in receipt["withheld"]:
        if (not isinstance(entry, dict) or set(entry) != {"pointer", "recordPointer", "reason"}
                or not isinstance(entry.get("pointer"), str) or not isinstance(entry.get("recordPointer"), str)
                or len(entry["pointer"]) > MAX_POINTER_BYTES or len(entry["recordPointer"]) > MAX_POINTER_BYTES
                or entry.get("reason") != REASON):
            raise ValueError("invalid Buildout withholding entry")
    try:
        receipt_size = len(events.encoded(receipt))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid Buildout withholding receipt") from None
    if receipt_size > MAX_RECEIPT_BYTES:
        raise ValueError("Buildout withholding receipt exceeds bound")


def _project(document, source_key, representation, numeric_literals):
    _scope(source_key, representation)
    _bounded(document)
    try:
        size = len(events.encoded(document))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid Buildout projection document") from None
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError("Buildout projection document exceeds bound")
    rows_key = "listings" if representation == "collector" else "inventory"
    rows = document.get(rows_key) if isinstance(document, dict) else None
    if isinstance(rows, list) and len(rows) > MAX_ROWS:
        raise ValueError("Buildout projection rows exceed bound")
    result = copy.deepcopy(document)
    withheld = []

    def apply(container, key, pointer, record_pointer):
        if key not in container or not _recognized(container[key]):
            return
        if len(withheld) >= MAX_RECEIPTS or len(pointer.encode("utf8")) > MAX_POINTER_BYTES:
            raise ValueError("Buildout withholding exceeds bound")
        container[key] = None
        withheld.append({"pointer": pointer, "recordPointer": record_pointer, "reason": REASON})

    projected_rows = result[rows_key] if isinstance(rows, list) else []
    for i, row in enumerate(projected_rows):
        if not isinstance(row, dict):
            continue
        record_pointer = f"/{rows_key}/{i}"
        if representation == "native":
            apply(row, "pdf_url", record_pointer + "/pdf_url", record_pointer)
        else:
            if "brochures" not in row or row["brochures"] is None:
                continue
            brochures = row["brochures"]
            if not isinstance(brochures, list):
                continue
            if len(brochures) > MAX_ROWS:
                raise ValueError("Buildout brochure array exceeds bound")
            for j, item in enumerate(brochures):
                if not isinstance(item, dict):
                    continue
                apply(item, "url", f"{record_pointer}/brochures/{j}/url", record_pointer)
    receipt = {"version": VERSION, "sourceKey": source_key, "representation": representation,
               "withheld": withheld,
               "projectedCanonicalSha256": map_projection.canonical_digest(result, numeric_literals or None)}
    _check_receipt_size(receipt)
    return result, receipt


def project(document, *, source_key, representation="collector"):
    """Return a projected copy and loss receipt; never modify the source."""
    return _project(document, source_key, representation, None)


def prepare(raw, *, source_key, representation="collector"):
    """Decode bounded JSON while preserving exceptional numeric literals."""
    if not isinstance(raw, bytes) or len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Buildout projection input exceeds bound")
    document, literals = acquisition.decode_json(raw, max_bytes=MAX_DOCUMENT_BYTES)
    projected, receipt = _project(document, source_key, representation, literals)
    return projected, literals, receipt


def validate(projected, receipt, *, source_key, representation="collector", numeric_literals=None):
    """Validate shape and projected bytes; authenticate receipt separately."""
    _scope(source_key, representation)
    if (not isinstance(receipt, dict) or set(receipt) != {"version", "sourceKey", "representation", "withheld",
                                                          "projectedCanonicalSha256"}
            or receipt.get("version") != VERSION or receipt.get("sourceKey") != source_key
            or receipt.get("representation") != representation or not isinstance(receipt.get("withheld"), list)
            or len(receipt["withheld"]) > MAX_RECEIPTS
            or not isinstance(receipt.get("projectedCanonicalSha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["projectedCanonicalSha256"])):
        raise ValueError("invalid Buildout withholding receipt")
    _check_receipt_size(receipt)
    reprojected, check = _project(projected, source_key, representation, numeric_literals)
    if (reprojected != projected or check["withheld"]
            or check["projectedCanonicalSha256"] != receipt["projectedCanonicalSha256"]):
        raise ValueError("Buildout projected representation changed")
    previous = None
    for entry in receipt["withheld"]:
        pointer, record = entry["pointer"], entry["recordPointer"]
        if not isinstance(pointer, str) or len(pointer.encode("utf8")) > MAX_POINTER_BYTES or entry["reason"] != REASON:
            raise ValueError("invalid Buildout withholding pointer")
        if numeric_literals and any(literal == pointer or literal.startswith(pointer + "/")
                                    or pointer.startswith(literal + "/") for literal in numeric_literals):
            raise ValueError("Buildout withholding conflicts with numeric literal")
        match = (COLLECTOR_POINTER if representation == "collector" else NATIVE_POINTER).fullmatch(pointer)
        if not match or any(len(group) > 5 for group in match.groups()):
            raise ValueError("invalid Buildout withholding pointer")
        indices = tuple(int(x) for x in match.groups())
        if any(index >= MAX_ROWS for index in indices):
            raise ValueError("invalid Buildout withholding pointer")
        expected_record = f"/{'listings' if representation == 'collector' else 'inventory'}/{indices[0]}"
        if record != expected_record or (previous is not None and indices <= previous):
            raise ValueError("invalid Buildout withholding order")
        previous = indices
        try:
            row = projected["listings" if representation == "collector" else "inventory"][indices[0]]
            if representation == "collector":
                target = row["brochures"][indices[1]]
                key = "url"
            else:
                target, key = row, "pdf_url"
            if not isinstance(target, dict) or key not in target or target[key] is not None:
                raise ValueError("Buildout withholding target mismatch")
        except (KeyError, IndexError, TypeError):
            raise ValueError("Buildout withholding target mismatch") from None
