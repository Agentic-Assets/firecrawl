"""Source-scoped projection of complete targeted-enrichment collector artifacts.

The result is unverified evidence, not an ingest authorization. A withholding
receipt requires an independently authenticated seal: null cannot establish that
an original value existed. Unrecognized credentials remain for ordinary gates.
"""
import re

from source_evidence import acquisition, buildout_projection, events, map_projection
from source_evidence.projections import buildout_page

VERSIONS = {source: f"{source}-targeted-enrichment-withholding/v1" for source in buildout_page.VERSIONS}
MAX_BYTES = buildout_page.MAX_BYTES
MAX_RECEIPTS = buildout_page.MAX_RECEIPTS
MAX_ROWS = 10000
LISTING_PATHS = (
    ("extraFacts/detail_tables/provenance/inputSha256", "original_html_hash"),
    ("extraFacts/detail_tables_attempt/provenance/inputSha256", "original_html_hash"),
)
OBSERVATION_PATHS = (
    ("pageContext/tables/provenance/inputSha256", "original_html_hash"),
    ("scrapedDoc/metadata/quokka-content-current-token", "token_named_metadata"),
)
METADATA_PATH = "pageContext/metadata"


def version_for(source_key):
    return VERSIONS.get(source_key) if isinstance(source_key, str) else None


def _scope(document, source_key):
    if version_for(source_key) is None or not isinstance(document, dict):
        raise ValueError("unsupported enrichment projection")
    meta, sources, listings, brokers = (document.get(key) for key in ("runMeta", "sources", "listings", "brokers"))
    if (not isinstance(meta, dict) or meta.get("mode") != "enrich"
            or not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict)
            or sources[0].get("sourceKey") != source_key
            or not isinstance(listings, list) or len(listings) > MAX_ROWS
            or any(not isinstance(row, dict) or row.get("sourceKey") != source_key for row in listings)
            or not isinstance(brokers, list)
            or type(document.get("totalListings")) is not int or document["totalListings"] != len(listings)
            or ("evidenceOnly" in meta and type(meta["evidenceOnly"]) is not bool)
            or ("evidencePurpose" in meta and meta["evidencePurpose"] != "private_unqualified_source_observations")
            or (meta.get("evidenceOnly") is True and listings)):
        raise ValueError("invalid enrichment projection scope")
    observations = sources[0].get("retainedObservations", [])
    if not isinstance(observations, list) or len(observations) + len(listings) > MAX_ROWS:
        raise ValueError("invalid enrichment observation scope")
    for row in observations:
        if not isinstance(row, dict):
            raise ValueError("invalid enrichment observation row")  # noqa: TRY004
        claim = row.get("claim")
        if isinstance(claim, dict) and claim.get("sourceKey") is not None and claim.get("sourceKey") != source_key:
            raise ValueError("conflicting enrichment observation source")
    return listings, observations


def _metadata(row):
    parent, key = buildout_page._target(row, METADATA_PATH)
    value = parent.get(key) if parent is not None else None
    if not isinstance(value, list):
        return []
    if len(value) > 100000:
        raise ValueError("enrichment metadata exceeds bound")
    return value


def _targets(document, source_key):
    listings, observations = _scope(document, source_key)
    for index, row in enumerate(listings):
        record = f"/listings/{index}"
        for path, reason in LISTING_PATHS:
            target, key = buildout_page._target(row, path)
            yield record, path, reason, target, key
    for index, row in enumerate(observations):
        record = f"/sources/0/retainedObservations/{index}"
        for path, reason in OBSERVATION_PATHS:
            target, key = buildout_page._target(row, path)
            yield record, path, reason, target, key
        for offset, entry in enumerate(_metadata(row)):
            if buildout_page._is_token_metadata(entry):
                yield record, f"{METADATA_PATH}/{offset}/content", "token_named_page_metadata", entry, "content"


def _check_collision(pointer, numeric_literals):
    if numeric_literals and any(p == pointer or p.startswith(pointer + "/") or pointer.startswith(p + "/") for p in numeric_literals):
        raise ValueError("enrichment withholding conflicts with numeric literal")


def project(document, *, source_key, numeric_literals=None):
    _scope(document, source_key)
    buildout_projection._bounded(document)
    try:
        if len(events.encoded(document)) > MAX_BYTES:
            raise ValueError("enrichment projection exceeds bound")
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid enrichment projection") from None
    buildout_page._refuse_ambiguous_map_references(document)
    mapped, map_receipt = map_projection.project(document, source_key=source_key,
                                                  _observation_version=buildout_page.MAP_VERSIONS[source_key])
    # Keep the true enrich mode while preventing the projected copy, including
    # a reconstructed export, from being fed into canonical ingest.
    scope_guard_added = "evidencePurpose" not in mapped["runMeta"]
    if scope_guard_added:
        mapped["runMeta"]["evidencePurpose"] = "private_unqualified_source_observations"
    withheld = []
    for record, path, reason, target, key in _targets(mapped, source_key):
        if target is None or key not in target or target[key] is None:
            continue
        value = target[key]
        if reason == "token_named_page_metadata" and not isinstance(value, str):
            continue
        if not isinstance(value, str) or (not value and reason != "token_named_page_metadata"):
            raise ValueError("invalid enrichment withholding value")
        if reason == "original_html_hash" and not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("invalid enrichment hash witness")
        pointer = f"{record}/{path}"
        _check_collision(pointer, numeric_literals)
        target[key] = None
        withheld.append({"pointer": pointer, "recordPointer": record, "reason": reason})
        if len(withheld) > MAX_RECEIPTS:
            raise ValueError("enrichment withholding exceeds bound")
    digest = map_projection.canonical_digest(mapped, numeric_literals or None)
    map_receipt["projectedCanonicalSha256"] = digest
    receipt = {"version": VERSIONS[source_key], "sourceKey": source_key, "withheld": withheld,
               "map": map_receipt, "projectedCanonicalSha256": digest, "scopeGuardAdded": scope_guard_added}
    if len(events.encoded(receipt)) > 1000000:
        raise ValueError("enrichment receipt exceeds bound")
    return mapped, receipt


def prepare(raw, *, source_key):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("enrichment projection input exceeds bound")
    document, literals = acquisition.decode_json(raw, max_bytes=MAX_BYTES)
    projected, receipt = project(document, source_key=source_key, numeric_literals=literals)
    return projected, literals, receipt


def validate(document, receipt, *, source_key, numeric_literals=None):
    _scope(document, source_key)
    if (not isinstance(receipt, dict) or set(receipt) != {"version", "sourceKey", "withheld", "map", "projectedCanonicalSha256", "scopeGuardAdded"}
            or type(receipt.get("scopeGuardAdded")) is not bool
            or "evidencePurpose" not in document["runMeta"]
            or (receipt["scopeGuardAdded"] and document["runMeta"]["evidencePurpose"] != "private_unqualified_source_observations")
            or receipt.get("version") != VERSIONS[source_key] or receipt.get("sourceKey") != source_key
            or not isinstance(receipt.get("withheld"), list) or len(receipt["withheld"]) > MAX_RECEIPTS
            or not isinstance(receipt.get("projectedCanonicalSha256"), str)
            or receipt["projectedCanonicalSha256"] != map_projection.canonical_digest(document, numeric_literals)
            or len(events.encoded(receipt)) > 1000000):
        raise ValueError("invalid enrichment projection receipt")
    if not isinstance(receipt.get("map"), dict) or receipt["map"].get("projectedCanonicalSha256") != receipt["projectedCanonicalSha256"]:
        raise ValueError("invalid enrichment map binding")
    map_projection.validate(document, receipt["map"], source_key=source_key, numeric_literals=numeric_literals,
                            _observation_version=buildout_page.MAP_VERSIONS[source_key])
    eligible = {}
    for order, (record, path, reason, target, key) in enumerate(_targets(document, source_key)):
        if target is not None and key in target and target[key] is None:
            eligible[f"{record}/{path}"] = (record, reason, order)
    previous = -1
    for entry in receipt["withheld"]:
        if not isinstance(entry, dict) or set(entry) != {"pointer", "recordPointer", "reason"}:
            raise ValueError("invalid enrichment withholding receipt")
        pointer = entry.get("pointer")
        if not isinstance(pointer, str) or pointer not in eligible:
            raise ValueError("invalid enrichment withholding pointer")
        record, reason, order = eligible[pointer]
        if entry.get("recordPointer") != record or entry.get("reason") != reason or order <= previous:
            raise ValueError("invalid enrichment withholding binding")
        _check_collision(pointer, numeric_literals)
        previous = order
    reprojected, check = project(document, source_key=source_key, numeric_literals=numeric_literals)
    if reprojected != document or check["withheld"] or check["map"]["replacements"]:
        raise ValueError("enrichment projection is not idempotent")
