#!/usr/bin/env python3
"""Offline-only multisource-v1 cohort admission for CRE capacity experiments.

This module deliberately does not scrape, invoke an adapter, or start an
experiment.  It binds reviewed provider receipts into an immutable cohort.
Adapters without an explicit provider not-found classifier stay unsupported for
attrition rather than inheriting a generic HTTP-status rule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import urllib.parse
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CONFIG = Path(__file__).with_name("cre_capacity_multisource_v1.json")
MAX_CONFIG_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_RAW_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_REDACTED_HEADER_VALUE_BYTES = 512
SAFE_REDACTED_HEADERS = frozenset(
    {
        "cache-control",
        "cf-cache-status",
        "cf-ray",
        "content-type",
        "date",
        "retry-after",
        "server",
        "x-request-id",
    }
)
CLASSIFICATIONS = frozenset(
    {
        "eligible_detail",
        "confirmed_current_attrition",
        "detail_unavailable_by_design",
        "invalid_target",
        "challenge_or_throttle",
        "transport_failure",
        "provider_5xx",
        "not_listing_content",
        "parser_failure",
        "unclassified",
    }
)
PLANES = frozenset({"strict_detail", "authoritative_inventory"})
STRATUM_FIELDS = ("transaction_class", "property_type", "page_weight_band")
# This is a versioned prevalidation lane, not a generic registry projection.
# Keep the review matrix adjacent to its verifier so a config edit cannot point
# an admitted cohort at a stale proxy/search host by accident.
EXPECTED_SOURCE_HOSTS = {
    "jll": ("property.jll.com",),
    "jll-investor": ("invest.jll.com",),
    "colliers": ("sales.colliers.com", "my.rcm1.com"),
    "colliers-main": ("colliers.com", "www.colliers.com"),
    "marcus-millichap": ("marcusmillichap.com", "www.marcusmillichap.com"),
    "avison-young": ("www.avisonyoung.us", "pse-api.sharplaunch.com"),
    "savills": ("search.savills.com",),
    "nai-global": ("ab.infabode.com", "infabode.com"),
    "transwestern": ("transwestern.com", "www.transwestern.com"),
    "matthews": ("matthews.com", "www.matthews.com"),
    "foundry-commercial": ("foundrycommercial.com", "www.foundrycommercial.com"),
    "daum-commercial": ("daumcommercial.com", "www.daumcommercial.com"),
    "cbre": ("cbre.com", "www.cbre.com"),
    "cbre-dealflow": ("www.cbredealflow.com",),
    "cushman-wakefield": ("cushmanwakefield.com", "www.cushmanwakefield.com"),
    "newmark": ("nmrk.com", "www.nmrk.com"),
    "svn": ("svn.com",),
    "lee-associates": ("www.lee-associates.com",),
    "srs": ("srsre.com", "www.srsre.com"),
    "bull-realty": ("www.bullrealty.com",),
}
EXPECTED_PLANE_COUNTS = {"strict_detail": 12, "authoritative_inventory": 8}


class MultisourceError(ValueError):
    """The offline cohort evidence is not safe to admit."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _enumeration_identity(
    source_key: str,
    provider_id: str,
    canonical_url: str,
    observed_at: str,
    receipt_sha256: str,
    body_sha256: str,
    total: int,
) -> str:
    """Bind a prevalidation row to the exact freshly enumerated provider target."""
    return _sha256(
        _canonical(
            {
                "source_key": source_key,
                "provider_id": provider_id,
                "canonical_url": canonical_url,
                "observed_at": observed_at,
                "receipt_sha256": receipt_sha256,
                "body_sha256": body_sha256,
                "total": total,
            }
        )
    )


def _read_json(path: Path, maximum: int) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MultisourceError(f"cannot read {path}") from exc
    if not raw or len(raw) > maximum:
        raise MultisourceError(f"invalid size for {path}")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MultisourceError(f"invalid JSON in {path}") from exc


def _private_root(path: Path) -> Path:
    if not path.is_absolute():
        raise MultisourceError("receipt root must be absolute")
    try:
        opened = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MultisourceError("receipt root is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or path != resolved
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        raise MultisourceError("receipt root must be private mode 0700")
    return resolved


def _private_regular(path: Path, maximum: int, *, root: Path) -> Path:
    if not path.is_absolute():
        raise MultisourceError("receipt paths must be absolute")
    try:
        opened = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MultisourceError("receipt is unavailable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size <= 0
        or opened.st_size > maximum
        or path != resolved
        or stat.S_IMODE(opened.st_mode) != 0o600
        or (resolved.parent != root and root not in resolved.parents)
    ):
        raise MultisourceError("receipt is not a bounded regular file")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    document = _read_json(path, MAX_CONFIG_BYTES)
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != "cre_capacity_multisource_v1_config"
        or not isinstance(document.get("sampling"), dict)
        or not isinstance(document.get("profiles"), dict)
        or not isinstance(document.get("counterbalance"), dict)
        or not isinstance(document.get("sources"), list)
    ):
        raise MultisourceError("multisource-v1 configuration is invalid")
    sampling = document["sampling"]
    if (
        set(sampling)
        != {
            "calibration_per_source",
            "core_per_source",
            "minimum_detail_eligible_for_core",
            "minimum_sources_for_experiment",
            "stratification_seed",
        }
        or any(
            type(sampling[key]) is not int or sampling[key] < 1
            for key in (
                "calibration_per_source",
                "core_per_source",
                "minimum_detail_eligible_for_core",
                "minimum_sources_for_experiment",
            )
        )
        or not isinstance(sampling["stratification_seed"], str)
        or not sampling["stratification_seed"]
        or sampling["calibration_per_source"] > sampling["core_per_source"]
        or sampling["minimum_detail_eligible_for_core"] > sampling["core_per_source"]
    ):
        raise MultisourceError("multisource-v1 sampling controls are invalid")
    profiles = document["profiles"]
    if set(profiles) != {"P0", "P1", "P2"}:
        raise MultisourceError("multisource-v1 profiles are invalid")
    expected_profiles = {
        "P0": {"browser_cpus": 2, "global_pages": 4, "source_workers": 1},
        "P1": {"browser_cpus": 6, "global_pages": 10, "source_workers": 1},
        "P2": {
            "browser_cpus": 6,
            "global_pages": 10,
            "source_workers": 2,
            "provider_family_exclusions": ["buildout", "cbre", "colliers", "jll"],
        },
    }
    if profiles != expected_profiles:
        raise MultisourceError("multisource-v1 runtime profiles drifted")
    if document.get("workload_weighting") != {
        "basis": "fresh_enumeration_total",
        "winsorize_percentile": 0.95,
    }:
        raise MultisourceError("multisource-v1 workload weighting drifted")
    sources = document["sources"]
    source_keys = {item.get("key") for item in sources if isinstance(item, dict)}
    if len(sources) != 20 or source_keys != set(EXPECTED_SOURCE_HOSTS):
        raise MultisourceError(
            "multisource-v1 source matrix must contain 20 unique sources"
        )
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source)
            - {
                "key",
                "plane",
                "provider_family",
                "hosts",
                "not_found_classifier",
                "exclusive",
            }
            or not isinstance(source.get("key"), str)
            or source.get("plane") not in PLANES
            or not isinstance(source.get("provider_family"), str)
            or not isinstance(source.get("hosts"), list)
            or not source["hosts"]
            or not all(isinstance(host, str) and host for host in source["hosts"])
            or source.get("not_found_classifier")
            not in {None, "jll_next_data_404_no_property"}
            or ("exclusive" in source and type(source["exclusive"]) is not bool)
        ):
            raise MultisourceError("multisource-v1 source matrix is invalid")
        if tuple(source["hosts"]) != EXPECTED_SOURCE_HOSTS[source["key"]]:
            raise MultisourceError("multisource-v1 source host contract drifted")
    if {
        plane: sum(source["plane"] == plane for source in sources) for plane in PLANES
    } != EXPECTED_PLANE_COUNTS:
        raise MultisourceError("multisource-v1 plane contract drifted")
    return document


def _valid_jll_not_found(raw: bytes, receipt: Mapping[str, Any]) -> bool:
    """The only current v1 attrition classifier: explicit JLL __NEXT_DATA__ 404."""
    if (
        receipt.get("http_status") != 404
        or receipt.get("not_found_classifier") != "jll_next_data_404_no_property"
    ):
        return False
    try:
        payload = json.loads(raw)
        html = payload.get("rawHtml") if isinstance(payload, dict) else None
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html or "",
            re.IGNORECASE | re.DOTALL,
        )
        next_data = json.loads(match.group(1)) if match else None
        page_props = (
            next_data.get("props", {}).get("pageProps", {})
            if isinstance(next_data, dict)
            else {}
        )
        error = page_props.get("error") if isinstance(page_props, dict) else None
        return bool(
            isinstance(page_props, dict)
            and page_props.get("property") is None
            and page_props.get("notFound") is True
            and isinstance(error, dict)
            and error.get("statusCode") == 404
        )
    except (AttributeError, json.JSONDecodeError):
        return False


def _safe_redacted_headers(value: Any) -> dict[str, str]:
    """Reject headers that could carry credentials or unbounded raw content."""
    if not isinstance(value, dict) or set(value) - SAFE_REDACTED_HEADERS:
        raise MultisourceError("receipt headers are not safely redacted")
    safe: dict[str, str] = {}
    for name, header_value in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(header_value, str)
            or len(header_value.encode()) > MAX_REDACTED_HEADER_VALUE_BYTES
        ):
            raise MultisourceError("receipt headers are not safely redacted")
        safe[name] = header_value
    return safe


def _hex_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MultisourceError(f"{label} hash is invalid")
    return value


def _read_private_json(path: Path, maximum: int, *, root: Path) -> Any:
    return _read_json(_private_regular(path, maximum, root=root), maximum)


def _source_config_sha256(source: Mapping[str, Any]) -> str:
    """A row may only claim the source contract admitted by this config."""
    return _sha256(_canonical(dict(source)))


def _public_url(value: Any, source: Mapping[str, Any]) -> str:
    parsed = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if (
        not parsed
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname not in source["hosts"]
        or not parsed.path
    ):
        raise MultisourceError("receipt target is outside the provider host contract")
    return value


def _observed_at(value: Any) -> str:
    # This is deliberately a narrow, UTC-only serial form.  The producer must
    # record the same timestamp in the private enumeration receipt.
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value)
        is None
    ):
        raise MultisourceError("enumeration timestamp is invalid")
    return value


def _enumeration_binding(
    receipt: Mapping[str, Any], source: Mapping[str, Any], *, root: Path
) -> tuple[str, int, str]:
    """Rehash an enumeration receipt instead of trusting a row's assertion."""
    enum_path_value = receipt.get("enumeration_receipt_path")
    if not isinstance(enum_path_value, str):
        raise MultisourceError("enumeration receipt path is invalid")
    enum_path = _private_regular(
        Path(enum_path_value), MAX_RAW_RECEIPT_BYTES, root=root
    )
    enum_hash = _file_sha256(enum_path)
    if enum_hash != _hex_digest(
        receipt.get("enumeration_receipt_sha256"), label="enumeration receipt"
    ):
        raise MultisourceError("enumeration receipt hash drifted")
    document = _read_json(enum_path, MAX_RAW_RECEIPT_BYTES)
    required = {"observed_at", "total", "complete", "truncated", "provider_ids", "body"}
    if not isinstance(document, dict) or set(document) != required:
        raise MultisourceError("enumeration receipt is malformed")
    observed_at = _observed_at(receipt.get("enumeration_observed_at"))
    total = receipt.get("enumeration_total")
    if (
        document["observed_at"] != observed_at
        or type(total) is not int
        or total < 1
        or document["total"] != total
        or document["complete"] is not True
        or document["truncated"] is not False
        or not isinstance(document["provider_ids"], list)
        or not all(
            isinstance(identifier, str) and identifier
            for identifier in document["provider_ids"]
        )
        or receipt.get("provider_id") not in document["provider_ids"]
        or len(document["provider_ids"]) > total
        or not isinstance(document["body"], str)
    ):
        raise MultisourceError("enumeration completeness proof is invalid")
    body_hash = _sha256(document["body"].encode())
    if body_hash != _hex_digest(
        receipt.get("enumeration_body_sha256"), label="enumeration body"
    ):
        raise MultisourceError("enumeration body hash drifted")
    canonical_url = _public_url(receipt.get("canonical_url"), source)
    identity = _enumeration_identity(
        source["key"],
        receipt["provider_id"],
        canonical_url,
        observed_at,
        enum_hash,
        body_hash,
        total,
    )
    if receipt.get("enumeration_identity_sha256") != identity:
        raise MultisourceError("receipt lacks a fresh enumeration receipt binding")
    return identity, total, enum_hash


def _fidelity_evidence(
    receipt: Mapping[str, Any], *, root: Path
) -> tuple[str, str, str]:
    """Bind field and asset fidelity to real normalized artifacts, never booleans."""
    values: dict[str, tuple[int, Any]] = {
        "normalized": (MAX_RAW_RECEIPT_BYTES, None),
        "field_locator": (MAX_RAW_RECEIPT_BYTES, None),
        "asset_evidence": (MAX_RAW_RECEIPT_BYTES, None),
    }
    documents: dict[str, Mapping[str, Any]] = {}
    hashes: dict[str, str] = {}
    for name, (maximum, _) in values.items():
        path_value = receipt.get(f"{name}_path")
        if not isinstance(path_value, str):
            raise MultisourceError(f"{name} path is invalid")
        path = _private_regular(Path(path_value), maximum, root=root)
        actual = _file_sha256(path)
        expected = _hex_digest(receipt.get(f"{name}_sha256"), label=name)
        if actual != expected:
            raise MultisourceError(f"{name} hash drifted")
        document = _read_json(path, maximum)
        if not isinstance(document, Mapping):
            raise MultisourceError(f"{name} evidence is malformed")
        documents[name] = document
        hashes[name] = actual
    provider_id = receipt["provider_id"]
    canonical_url = receipt["canonical_url"]
    normalized = documents["normalized"]
    locators = documents["field_locator"]
    assets = documents["asset_evidence"]
    fields = normalized.get("fields")
    locator_fields = locators.get("fields")
    if (
        normalized.get("provider_id") != provider_id
        or normalized.get("canonical_url") != canonical_url
        or not isinstance(fields, Mapping)
        or not fields
        or locators.get("provider_id") != provider_id
        or not isinstance(locator_fields, Mapping)
        or not locator_fields
        or not set(locator_fields) <= set(fields)
        or assets.get("provider_id") != provider_id
        or not isinstance(assets.get("assets"), list)
    ):
        raise MultisourceError("artifact-bound fidelity evidence is incomplete")
    return hashes["normalized"], hashes["field_locator"], hashes["asset_evidence"]


def _stratum(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(STRATUM_FIELDS):
        raise MultisourceError("stratification fields are incomplete")
    result = {field: value[field] for field in STRATUM_FIELDS}
    if not all(isinstance(item, str) and item.strip() for item in result.values()):
        raise MultisourceError("stratification fields are invalid")
    return result


def _receipt_summary(
    receipt: Mapping[str, Any], source: Mapping[str, Any], *, root: Path
) -> dict[str, Any]:
    required = {
        "source_key",
        "provider_id",
        "enumeration_identity_sha256",
        "enumeration_observed_at",
        "enumeration_receipt_path",
        "enumeration_receipt_sha256",
        "enumeration_body_sha256",
        "enumeration_total",
        "canonical_url",
        "final_url",
        "http_status",
        "content_type",
        "redacted_headers",
        "timing_ms",
        "retry_count",
        "raw_receipt_path",
        "raw_receipt_sha256",
        "normalized_path",
        "normalized_sha256",
        "field_locator_path",
        "field_locator_sha256",
        "asset_evidence_path",
        "asset_evidence_sha256",
        "parser_sha256",
        "config_sha256",
        "source_config_sha256",
        "classification",
        "stratum",
    }
    allowed = required | {"not_found_classifier"}
    if set(receipt) - allowed or not required <= set(receipt):
        raise MultisourceError("receipt has unsupported or missing fields")
    if receipt.get("source_key") != source["key"]:
        raise MultisourceError("receipt is assigned to the wrong source")
    if receipt.get("classification") not in CLASSIFICATIONS:
        raise MultisourceError("receipt classification is invalid")
    if (
        not isinstance(receipt.get("provider_id"), str)
        or not receipt["provider_id"].strip()
    ):
        raise MultisourceError("receipt lacks fresh enumeration provider identity")
    if receipt.get("source_config_sha256") != _source_config_sha256(source):
        raise MultisourceError("receipt source configuration digest is not this cohort")
    identity, population_total, enumeration_receipt_sha256 = _enumeration_binding(
        receipt, source, root=root
    )
    canonical_url = _public_url(receipt["canonical_url"], source)
    _public_url(receipt.get("final_url"), source)
    if (
        type(receipt.get("http_status")) is not int
        or not 100 <= receipt["http_status"] <= 599
    ):
        raise MultisourceError("receipt HTTP status is invalid")
    if not isinstance(receipt.get("content_type"), str) or not receipt["content_type"]:
        raise MultisourceError("receipt transport metadata is invalid")
    _safe_redacted_headers(receipt["redacted_headers"])
    if (
        type(receipt.get("timing_ms")) not in {int, float}
        or receipt["timing_ms"] < 0
        or type(receipt.get("retry_count")) is not int
        or receipt["retry_count"] < 0
    ):
        raise MultisourceError("receipt timing is invalid")
    for key in ("raw_receipt_sha256", "parser_sha256", "config_sha256"):
        _hex_digest(receipt.get(key), label=key)
    raw_path_value = receipt.get("raw_receipt_path")
    if not isinstance(raw_path_value, str):
        raise MultisourceError("raw receipt path is invalid")
    raw_path = _private_regular(Path(raw_path_value), MAX_RAW_RECEIPT_BYTES, root=root)
    raw_hash = _file_sha256(raw_path)
    if raw_hash != receipt["raw_receipt_sha256"]:
        raise MultisourceError("raw receipt hash drifted")
    normalized_sha256, field_locator_sha256, asset_evidence_sha256 = _fidelity_evidence(
        receipt, root=root
    )
    classification = receipt["classification"]
    if classification == "confirmed_current_attrition":
        classifier = source.get("not_found_classifier")
        if classifier != "jll_next_data_404_no_property" or not _valid_jll_not_found(
            raw_path.read_bytes(), receipt
        ):
            raise MultisourceError("provider-specific attrition proof is absent")
    elif classification == "eligible_detail" and receipt["http_status"] != 200:
        raise MultisourceError("eligible detail receipt must be HTTP 200")
    return {
        "provider_id": receipt["provider_id"],
        "enumeration_identity_sha256": identity,
        "enumeration_receipt_sha256": enumeration_receipt_sha256,
        "enumeration_total": population_total,
        "canonical_url": canonical_url,
        "http_status": receipt["http_status"],
        "timing_ms": receipt["timing_ms"],
        "retry_count": receipt["retry_count"],
        "classification": classification,
        "raw_receipt_sha256": raw_hash,
        "normalized_sha256": normalized_sha256,
        "field_locator_sha256": field_locator_sha256,
        "asset_evidence_sha256": asset_evidence_sha256,
        "parser_sha256": receipt["parser_sha256"],
        "config_sha256": receipt["config_sha256"],
        "stratum": _stratum(receipt["stratum"]),
    }


def _ranked_stratified(
    rows: list[dict[str, Any]], *, count: int, seed: str
) -> list[dict[str, Any]]:
    """Deterministic round-robin strata selection, rather than lexical slicing."""
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row["stratum"][field] for field in STRATUM_FIELDS)
        buckets.setdefault(key, []).append(row)
    for key, values in buckets.items():
        values.sort(
            key=lambda row: _sha256(
                _canonical(
                    {
                        "seed": seed,
                        "stratum": key,
                        "provider_id": row["provider_id"],
                        "enumeration_identity_sha256": row[
                            "enumeration_identity_sha256"
                        ],
                    }
                )
            )
        )
    result: list[dict[str, Any]] = []
    indexes = {key: 0 for key in buckets}
    while len(result) < count:
        added = False
        for key in sorted(buckets):
            index = indexes[key]
            if index < len(buckets[key]) and len(result) < count:
                result.append(buckets[key][index])
                indexes[key] += 1
                added = True
        if not added:
            return result
    return result


def _safe_member(row: Mapping[str, Any]) -> dict[str, Any]:
    """The public cohort manifest never carries restricted paths, URLs, or headers."""
    return {
        key: row[key]
        for key in (
            "provider_id",
            "enumeration_identity_sha256",
            "raw_receipt_sha256",
            "normalized_sha256",
            "field_locator_sha256",
            "asset_evidence_sha256",
            "parser_sha256",
            "stratum",
        )
    }


def prevalidate_cohort(
    receipts: Mapping[str, Any], *, config_path: Path = CONFIG
) -> dict[str, Any]:
    """Build a fixed, immutable multisource cohort from rehashed private receipts."""
    config = load_config(config_path)
    if (
        receipts.get("schema_version") != SCHEMA_VERSION
        or receipts.get("kind") != "cre_capacity_multisource_v1_receipts"
        or not isinstance(receipts.get("receipt_root"), str)
        or not isinstance(receipts.get("receipts"), list)
        or set(receipts) != {"schema_version", "kind", "receipt_root", "receipts"}
    ):
        raise MultisourceError("multisource-v1 receipts are invalid")
    root = _private_root(Path(receipts["receipt_root"]))
    by_source = {source["key"]: source for source in config["sources"]}
    summaries: dict[str, list[dict[str, Any]]] = {key: [] for key in by_source}
    config_sha256 = _sha256(_canonical(config))
    seen: set[tuple[str, str]] = set()
    for item in receipts["receipts"]:
        if not isinstance(item, dict) or item.get("source_key") not in by_source:
            raise MultisourceError("receipt source is not in the fixed matrix")
        summary = _receipt_summary(item, by_source[item["source_key"]], root=root)
        if summary["config_sha256"] != config_sha256:
            raise MultisourceError("receipt configuration digest is not this cohort")
        identity = (item["source_key"], summary["provider_id"])
        if identity in seen:
            raise MultisourceError("fresh enumeration identity is duplicated")
        seen.add(identity)
        summaries[item["source_key"]].append(summary)
    sampling = config["sampling"]
    cohort_sources = []
    for key, source in by_source.items():
        rows = summaries[key]
        totals = {row["enumeration_total"] for row in rows}
        enumeration_hashes = {row["enumeration_receipt_sha256"] for row in rows}
        if len(totals) > 1 or len(enumeration_hashes) > 1:
            raise MultisourceError("source rows do not share one fresh enumeration")
        if rows and len(rows) > next(iter(totals)):
            raise MultisourceError("source rows exceed fresh enumeration population")
        eligible = [row for row in rows if row["classification"] == "eligible_detail"]
        qualified = [row for row in eligible if row["retry_count"] == 0]
        attrition = [
            row
            for row in rows
            if row["classification"] == "confirmed_current_attrition"
        ]
        selected_calibration = _ranked_stratified(
            qualified,
            count=sampling["calibration_per_source"],
            seed=sampling["stratification_seed"],
        )
        selected_core = _ranked_stratified(
            qualified,
            count=sampling["core_per_source"],
            seed=sampling["stratification_seed"],
        )
        timing_values = sorted(row["timing_ms"] for row in rows)
        timing_total = sum(timing_values)
        row_count = len(rows)
        parser_failures = sum(row["classification"] == "parser_failure" for row in rows)
        transport_failures = sum(
            row["classification"] == "transport_failure" for row in rows
        )
        # Artifact fidelity is a gate, not a caller-owned boolean.  A malformed
        # normalized/locator/asset artifact rejects the entire admission above,
        # so every admitted row has exactly zero unbound-fidelity failures.
        fidelity_failures = 0
        cohort_sources.append(
            {
                "source_key": key,
                "plane": source["plane"],
                "provider_family": source["provider_family"],
                "exclusive": source.get("exclusive", False),
                "calibration": [_safe_member(row) for row in selected_calibration],
                "core": [_safe_member(row) for row in selected_core]
                if len(qualified) >= sampling["minimum_detail_eligible_for_core"]
                else [],
                "core_state": "ready"
                if len(qualified) >= sampling["minimum_detail_eligible_for_core"]
                else "insufficient_current_detail_eligibility",
                "fresh_enumeration": {
                    "total_population": next(iter(totals)) if totals else 0,
                    "receipt_sha256": next(iter(enumeration_hashes))
                    if enumeration_hashes
                    else None,
                    "complete": bool(rows),
                },
                "outcomes": dict(
                    sorted(Counter(row["classification"] for row in rows).items())
                ),
                "http_outcomes": dict(
                    sorted(Counter(str(row["http_status"]) for row in rows).items())
                ),
                "confirmed_current_attrition": len(attrition),
                "current_active_successes": len(eligible),
                "individually_qualified_rows": len(qualified),
                "parser_failures": parser_failures,
                "transport_failures": transport_failures,
                "fidelity_failures": fidelity_failures,
                "row_rates": {
                    "current_active_successes": round(len(eligible) / row_count, 6)
                    if row_count
                    else None,
                    "confirmed_current_attrition": round(len(attrition) / row_count, 6)
                    if row_count
                    else None,
                    "individually_qualified_rows": round(len(qualified) / row_count, 6)
                    if row_count
                    else None,
                    "parser_failures": round(parser_failures / row_count, 6)
                    if row_count
                    else None,
                    "transport_failures": round(transport_failures / row_count, 6)
                    if row_count
                    else None,
                    "fidelity_failures": round(fidelity_failures / row_count, 6)
                    if row_count
                    else None,
                },
                "challenge_or_throttle": sum(
                    row["classification"] == "challenge_or_throttle" for row in rows
                ),
                "retry_count": sum(row["retry_count"] for row in rows),
                "latency_ms": {
                    "p50": timing_values[len(timing_values) // 2]
                    if timing_values
                    else None,
                    "p95": timing_values[
                        min(len(timing_values) - 1, int(len(timing_values) * 0.95))
                    ]
                    if timing_values
                    else None,
                },
                "eligible_detail_rows_per_minute": round(
                    len(eligible) * 60_000 / timing_total, 3
                )
                if timing_total
                else None,
                "individually_qualified_rows_per_minute": round(
                    len(qualified) * 60_000 / timing_total, 3
                )
                if timing_total
                else None,
                "structured_field_evidence_bound": len(eligible),
                "asset_evidence_bound": len(eligible),
                "raw_receipts_retained": len(rows),
            }
        )
    ready = [item for item in cohort_sources if item["core_state"] == "ready"]
    ready_rates = [
        item["individually_qualified_rows_per_minute"]
        for item in ready
        if item["individually_qualified_rows_per_minute"] is not None
    ]
    populations = sorted(
        item["fresh_enumeration"]["total_population"] for item in ready
    )
    if populations:
        percentile_index = min(
            len(populations) - 1,
            max(
                0,
                int(
                    (len(populations) - 1)
                    * config["workload_weighting"]["winsorize_percentile"]
                ),
            ),
        )
        workload_cap = populations[percentile_index]
    else:
        workload_cap = 0
    workload_weights = {
        item["source_key"]: min(
            item["fresh_enumeration"]["total_population"], workload_cap
        )
        for item in ready
    }
    weighted_rates = [
        (
            item["individually_qualified_rows_per_minute"],
            workload_weights[item["source_key"]],
        )
        for item in ready
        if item["individually_qualified_rows_per_minute"] is not None
    ]
    cohort_members = [
        {
            "source_key": item["source_key"],
            "plane": item["plane"],
            "core": [
                {
                    key: row[key]
                    for key in (
                        "provider_id",
                        "enumeration_identity_sha256",
                        "raw_receipt_sha256",
                        "normalized_sha256",
                        "field_locator_sha256",
                        "asset_evidence_sha256",
                        "parser_sha256",
                    )
                }
                for row in item["core"]
            ],
        }
        for item in cohort_sources
    ]
    cohort_sha256 = _sha256(
        _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "config_sha256": config_sha256,
                "members": cohort_members,
            }
        )
    )
    plane_results = {}
    expected_plane_sizes = {
        plane: sum(source["plane"] == plane for source in config["sources"])
        for plane in PLANES
    }
    for plane in sorted(PLANES):
        plane_ready = [item for item in ready if item["plane"] == plane]
        plane_rates = [
            item["individually_qualified_rows_per_minute"]
            for item in plane_ready
            if item["individually_qualified_rows_per_minute"] is not None
        ]
        plane_weights = [
            (
                item["individually_qualified_rows_per_minute"],
                workload_weights[item["source_key"]],
            )
            for item in plane_ready
            if item["individually_qualified_rows_per_minute"] is not None
        ]
        plane_results[plane] = {
            "sources_in_matrix": expected_plane_sizes[plane],
            "sources_core_ready": len(plane_ready),
            "source_keys": [
                item["source_key"] for item in cohort_sources if item["plane"] == plane
            ],
            "equal_source_individually_qualified_rows_per_minute": round(
                sum(plane_rates) / len(plane_rates), 3
            )
            if plane_rates
            else None,
            "workload_weighted_individually_qualified_rows_per_minute": round(
                sum(rate * weight for rate, weight in plane_weights)
                / sum(weight for _, weight in plane_weights),
                3,
            )
            if plane_weights and sum(weight for _, weight in plane_weights)
            else None,
        }
    complete_matrix = (
        len(ready) == sampling["minimum_sources_for_experiment"]
        and len(cohort_sources) == sampling["minimum_sources_for_experiment"]
        and all(
            plane_results[plane]["sources_core_ready"] == expected_plane_sizes[plane]
            for plane in PLANES
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_capacity_multisource_v1_cohort",
        "config_sha256": config_sha256,
        "cohort_sha256": cohort_sha256,
        "execution": "prevalidation_only_no_adapter_or_runtime_execution",
        "sampling": sampling,
        "sources": cohort_sources,
        "planes": plane_results,
        "aggregate": {
            "sources_in_matrix": len(cohort_sources),
            "sources_core_ready": len(ready),
            "state": "ready_for_review" if complete_matrix else "incomplete_screen",
            "primary_aggregation": "equal_source_weighted",
            "secondary_aggregation": "workload_weighted",
            "equal_source_individually_qualified_rows_per_minute": round(
                sum(ready_rates) / len(ready_rates), 3
            )
            if ready_rates
            else None,
            "workload_weighted_individually_qualified_rows_per_minute": round(
                sum(rate * weight for rate, weight in weighted_rates)
                / sum(weight for _, weight in weighted_rates),
                3,
            )
            if weighted_rates and sum(weight for _, weight in weighted_rates)
            else None,
            "workload_weighting": {
                "basis": config["workload_weighting"]["basis"],
                "winsorized_population_cap": workload_cap or None,
            },
        },
        "safety": {
            "database_writes": 0,
            "cache_writes": 0,
            "status_writes": 0,
            "scheduler_writes": 0,
            "model_or_ocr_changes": 0,
            "unsupported_attrition": "fail_closed",
            "provider_family_stop": "challenge_or_throttle_stops_family_only",
            "primary_measurement_retries": 0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(argv)
    try:
        manifest_root = _private_root(args.receipts.parent.resolve())
        receipts = _read_json(
            _private_regular(
                args.receipts.resolve(), MAX_RECEIPT_BYTES, root=manifest_root
            ),
            MAX_RECEIPT_BYTES,
        )
        if not isinstance(receipts, dict):
            raise MultisourceError("receipt root must be an object")
        print(
            json.dumps(
                prevalidate_cohort(receipts, config_path=args.config),
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except MultisourceError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
