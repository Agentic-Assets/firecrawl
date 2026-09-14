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


class MultisourceError(ValueError):
    """The offline cohort evidence is not safe to admit."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _enumeration_identity(source_key: str, provider_id: str, canonical_url: str) -> str:
    """Bind a prevalidation row to the exact freshly enumerated provider target."""
    return _sha256(
        _canonical(
            {
                "source_key": source_key,
                "provider_id": provider_id,
                "canonical_url": canonical_url,
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


def _private_regular(path: Path, maximum: int) -> Path:
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
        or opened.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
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
        }
        or any(type(value) is not int or value < 1 for value in sampling.values())
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
            "provider_family_exclusions": ["colliers"],
        },
    }
    if profiles != expected_profiles:
        raise MultisourceError("multisource-v1 runtime profiles drifted")
    sources = document["sources"]
    if (
        len(sources) != 20
        or len({item.get("key") for item in sources if isinstance(item, dict)}) != 20
    ):
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
            re.I | re.S,
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


def _complete_fidelity(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("complete") is True


def _receipt_summary(
    receipt: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    required = {
        "source_key",
        "provider_id",
        "enumeration_identity_sha256",
        "canonical_url",
        "final_url",
        "http_status",
        "content_type",
        "redacted_headers",
        "timing_ms",
        "retry_count",
        "raw_receipt_path",
        "raw_receipt_sha256",
        "normalized_sha256",
        "parser_sha256",
        "config_sha256",
        "classification",
        "structured_fidelity",
        "asset_fidelity",
    }
    if set(receipt) - {
        "not_found_classifier",
        "enumeration_identity_sha256",
        *required,
    }:
        raise MultisourceError("receipt has unsupported fields")
    if not required <= set(receipt) or receipt.get("source_key") != source["key"]:
        raise MultisourceError("receipt is incomplete or assigned to the wrong source")
    if receipt.get("classification") not in CLASSIFICATIONS:
        raise MultisourceError("receipt classification is invalid")
    if (
        not isinstance(receipt.get("provider_id"), str)
        or not receipt["provider_id"].strip()
    ):
        raise MultisourceError("receipt lacks fresh enumeration provider identity")
    if (
        not isinstance(receipt.get("enumeration_identity_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["enumeration_identity_sha256"]) is None
        or receipt["enumeration_identity_sha256"]
        != _enumeration_identity(
            source["key"], receipt["provider_id"], receipt["canonical_url"]
        )
    ):
        raise MultisourceError("receipt lacks a fresh enumeration receipt binding")
    for key in ("canonical_url", "final_url"):
        value = receipt.get(key)
        parsed = urllib.parse.urlsplit(value) if isinstance(value, str) else None
        if (
            not parsed
            or parsed.scheme != "https"
            or parsed.hostname not in source["hosts"]
        ):
            raise MultisourceError(
                "receipt target is outside the provider host contract"
            )
    if (
        type(receipt.get("http_status")) is not int
        or not 100 <= receipt["http_status"] <= 599
    ):
        raise MultisourceError("receipt HTTP status is invalid")
    if not isinstance(receipt.get("content_type"), str) or not receipt["content_type"]:
        raise MultisourceError("receipt transport metadata is invalid")
    redacted_headers = _safe_redacted_headers(receipt["redacted_headers"])
    if not _complete_fidelity(receipt.get("structured_fidelity")) and not isinstance(
        receipt.get("structured_fidelity"), Mapping
    ):
        raise MultisourceError("structured fidelity is invalid")
    if not _complete_fidelity(receipt.get("asset_fidelity")) and not isinstance(
        receipt.get("asset_fidelity"), Mapping
    ):
        raise MultisourceError("asset fidelity is invalid")
    if (
        type(receipt.get("timing_ms")) not in {int, float}
        or receipt["timing_ms"] < 0
        or type(receipt.get("retry_count")) is not int
        or receipt["retry_count"] < 0
    ):
        raise MultisourceError("receipt timing is invalid")
    for key in (
        "raw_receipt_sha256",
        "normalized_sha256",
        "parser_sha256",
        "config_sha256",
    ):
        if (
            not isinstance(receipt.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt[key]) is None
        ):
            raise MultisourceError("receipt hash is invalid")
    if not isinstance(receipt.get("raw_receipt_path"), str):
        raise MultisourceError("raw receipt path is invalid")
    raw_path = _private_regular(
        Path(receipt["raw_receipt_path"]), MAX_RAW_RECEIPT_BYTES
    )
    raw_hash = _file_sha256(raw_path)
    if raw_hash != receipt["raw_receipt_sha256"]:
        raise MultisourceError("raw receipt hash drifted")
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
        "enumeration_identity_sha256": receipt["enumeration_identity_sha256"],
        "canonical_url": receipt["canonical_url"],
        "final_url": receipt["final_url"],
        "http_status": receipt["http_status"],
        "content_type": receipt["content_type"],
        "redacted_headers": redacted_headers,
        "timing_ms": receipt["timing_ms"],
        "retry_count": receipt["retry_count"],
        "classification": classification,
        "raw_receipt_path": str(raw_path),
        "raw_receipt_sha256": raw_hash,
        "normalized_sha256": receipt["normalized_sha256"],
        "parser_sha256": receipt["parser_sha256"],
        "config_sha256": receipt["config_sha256"],
        "structured_fidelity": receipt["structured_fidelity"],
        "asset_fidelity": receipt["asset_fidelity"],
    }


def prevalidate_cohort(
    receipts: Mapping[str, Any], *, config_path: Path = CONFIG
) -> dict[str, Any]:
    """Build a fixed, immutable multisource cohort from rehashed private receipts."""
    config = load_config(config_path)
    if (
        receipts.get("schema_version") != SCHEMA_VERSION
        or receipts.get("kind") != "cre_capacity_multisource_v1_receipts"
        or not isinstance(receipts.get("receipts"), list)
    ):
        raise MultisourceError("multisource-v1 receipts are invalid")
    by_source = {source["key"]: source for source in config["sources"]}
    summaries: dict[str, list[dict[str, Any]]] = {key: [] for key in by_source}
    config_sha256 = _sha256(_canonical(config))
    seen: set[tuple[str, str]] = set()
    for item in receipts["receipts"]:
        if not isinstance(item, dict) or item.get("source_key") not in by_source:
            raise MultisourceError("receipt source is not in the fixed matrix")
        summary = _receipt_summary(item, by_source[item["source_key"]])
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
        rows = sorted(summaries[key], key=lambda row: row["provider_id"])
        eligible = [row for row in rows if row["classification"] == "eligible_detail"]
        qualified = [
            row
            for row in eligible
            if row["retry_count"] == 0
            and _complete_fidelity(row["structured_fidelity"])
            and _complete_fidelity(row["asset_fidelity"])
        ]
        attrition = [
            row
            for row in rows
            if row["classification"] == "confirmed_current_attrition"
        ]
        calibration = qualified[: sampling["calibration_per_source"]]
        core = qualified[: sampling["core_per_source"]]
        timing_values = sorted(row["timing_ms"] for row in rows)
        timing_total = sum(timing_values)
        cohort_sources.append(
            {
                "source_key": key,
                "plane": source["plane"],
                "provider_family": source["provider_family"],
                "exclusive": source.get("exclusive", False),
                "calibration": calibration,
                "core": core
                if len(qualified) >= sampling["minimum_detail_eligible_for_core"]
                else [],
                "core_state": "ready"
                if len(qualified) >= sampling["minimum_detail_eligible_for_core"]
                else "insufficient_current_detail_eligibility",
                "outcomes": dict(
                    sorted(Counter(row["classification"] for row in rows).items())
                ),
                "http_outcomes": dict(
                    sorted(Counter(str(row["http_status"]) for row in rows).items())
                ),
                "confirmed_current_attrition": len(attrition),
                "current_active_successes": len(eligible),
                "individually_qualified_rows": len(qualified),
                "parser_failures": sum(
                    row["classification"] == "parser_failure" for row in rows
                ),
                "transport_failures": sum(
                    row["classification"] == "transport_failure" for row in rows
                ),
                "fidelity_failures": sum(
                    row["classification"] == "eligible_detail"
                    and not (
                        _complete_fidelity(row["structured_fidelity"])
                        and _complete_fidelity(row["asset_fidelity"])
                    )
                    for row in rows
                ),
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
                "structured_fidelity_complete": sum(
                    _complete_fidelity(row["structured_fidelity"]) for row in rows
                ),
                "asset_fidelity_complete": sum(
                    _complete_fidelity(row["asset_fidelity"]) for row in rows
                ),
                "raw_receipts_retained": len(rows),
            }
        )
    ready = [item for item in cohort_sources if item["core_state"] == "ready"]
    ready_rates = [
        item["individually_qualified_rows_per_minute"]
        for item in ready
        if item["individually_qualified_rows_per_minute"] is not None
    ]
    weighted_eligible = sum(len(item["core"]) for item in ready)
    weighted_ms = sum(sum(row["timing_ms"] for row in item["core"]) for item in ready)
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
                        "canonical_url",
                        "final_url",
                        "raw_receipt_sha256",
                        "normalized_sha256",
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
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_capacity_multisource_v1_cohort",
        "config_sha256": config_sha256,
        "cohort_sha256": cohort_sha256,
        "execution": "prevalidation_only_no_adapter_or_runtime_execution",
        "sampling": sampling,
        "sources": cohort_sources,
        "planes": {
            plane: {
                "sources_in_matrix": sum(
                    item["plane"] == plane for item in cohort_sources
                ),
                "sources_core_ready": sum(
                    item["plane"] == plane and item["core_state"] == "ready"
                    for item in cohort_sources
                ),
                "source_keys": [
                    item["source_key"]
                    for item in cohort_sources
                    if item["plane"] == plane
                ],
            }
            for plane in sorted(PLANES)
        },
        "aggregate": {
            "sources_in_matrix": len(cohort_sources),
            "sources_core_ready": len(ready),
            "state": "ready_for_review"
            if len(ready) >= sampling["minimum_sources_for_experiment"]
            else "insufficient_sources",
            "primary_aggregation": "equal_source_weighted",
            "secondary_aggregation": "workload_weighted",
            "equal_source_individually_qualified_rows_per_minute": round(
                sum(ready_rates) / len(ready_rates), 3
            )
            if ready_rates
            else None,
            "workload_weighted_individually_qualified_rows_per_minute": round(
                weighted_eligible * 60_000 / weighted_ms, 3
            )
            if weighted_ms
            else None,
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
        receipts = _read_json(
            _private_regular(args.receipts, MAX_RECEIPT_BYTES), MAX_RECEIPT_BYTES
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
