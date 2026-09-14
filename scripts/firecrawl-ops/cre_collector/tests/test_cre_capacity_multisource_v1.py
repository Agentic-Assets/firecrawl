"""Offline contracts for the deliberately non-generic multisource-v1 cohort."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cre_capacity_multisource_v1 as multisource
import pytest


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bind_enumeration_identity(receipt: dict[str, object]) -> None:
    receipt["enumeration_identity_sha256"] = multisource._enumeration_identity(
        receipt["source_key"], receipt["provider_id"], receipt["canonical_url"]
    )


def _receipt(
    tmp_path: Path, *, source_key: str, classification: str = "eligible_detail"
):
    raw = {"rawHtml": "<html>listing</html>"}
    if classification == "confirmed_current_attrition":
        raw = {
            "rawHtml": (
                '<script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"notFound":true,"error":{"statusCode":404}}}}'
                "</script>"
            )
        }
    raw_path = tmp_path / f"{source_key}.json"
    raw_bytes = json.dumps(raw).encode()
    raw_path.write_bytes(raw_bytes)
    raw_path.chmod(0o600)
    host = "property.jll.com" if source_key == "jll" else "www.cbre.com"
    receipt = {
        "source_key": source_key,
        "provider_id": f"{source_key}-id",
        "canonical_url": f"https://{host}/listing/{source_key}",
        "final_url": f"https://{host}/listing/{source_key}",
        "http_status": 404 if classification == "confirmed_current_attrition" else 200,
        "content_type": "application/json",
        "redacted_headers": {"content-type": "application/json"},
        "timing_ms": 12,
        "retry_count": 0,
        "raw_receipt_path": str(raw_path.resolve()),
        "raw_receipt_sha256": _hash(raw_bytes),
        "normalized_sha256": "a" * 64,
        "parser_sha256": "b" * 64,
        "config_sha256": multisource._sha256(
            multisource._canonical(multisource.load_config())
        ),
        "classification": classification,
        "structured_fidelity": {"complete": classification == "eligible_detail"},
        "asset_fidelity": {"complete": classification == "eligible_detail"},
    }
    if classification == "confirmed_current_attrition":
        receipt["not_found_classifier"] = "jll_next_data_404_no_property"
    _bind_enumeration_identity(receipt)
    return receipt


def test_fixed_matrix_profiles_and_planes_are_explicit() -> None:
    config = multisource.load_config()
    sources = {source["key"]: source for source in config["sources"]}

    assert len(sources) == 20
    assert sources["jll"]["plane"] == "strict_detail"
    assert sources["cbre"]["plane"] == "authoritative_inventory"
    assert sources["colliers-main"]["exclusive"] is True
    assert config["profiles"]["P0"] == {
        "browser_cpus": 2,
        "global_pages": 4,
        "source_workers": 1,
    }
    assert config["profiles"]["P2"]["source_workers"] == 2


def test_jll_only_attrition_requires_provider_specific_raw_receipt_proof(
    tmp_path: Path,
) -> None:
    receipt = _receipt(
        tmp_path, source_key="jll", classification="confirmed_current_attrition"
    )
    cohort = multisource.prevalidate_cohort(
        {
            "schema_version": 1,
            "kind": "cre_capacity_multisource_v1_receipts",
            "receipts": [receipt],
        }
    )
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["confirmed_current_attrition"] == 1
    assert jll["core_state"] == "insufficient_current_detail_eligibility"
    assert cohort["aggregate"]["state"] == "insufficient_sources"

    non_jll = _receipt(
        tmp_path, source_key="cbre", classification="confirmed_current_attrition"
    )
    non_jll["http_status"] = 404
    non_jll["not_found_classifier"] = "jll_next_data_404_no_property"
    with pytest.raises(
        multisource.MultisourceError, match="provider-specific attrition"
    ):
        multisource.prevalidate_cohort(
            {
                "schema_version": 1,
                "kind": "cre_capacity_multisource_v1_receipts",
                "receipts": [non_jll],
            }
        )


def test_core_uses_only_no_retry_complete_fidelity_rows_and_keeps_planes_separate(
    tmp_path: Path,
) -> None:
    rows = []
    for index in range(16):
        row = _receipt(tmp_path, source_key="jll")
        row["provider_id"] = f"jll-{index:02d}"
        _bind_enumeration_identity(row)
        rows.append(row)
    incomplete = _receipt(tmp_path, source_key="jll")
    incomplete["provider_id"] = "jll-incomplete"
    _bind_enumeration_identity(incomplete)
    incomplete["asset_fidelity"] = {"complete": False}
    retried = _receipt(tmp_path, source_key="jll")
    retried["provider_id"] = "jll-retried"
    _bind_enumeration_identity(retried)
    retried["retry_count"] = 1
    cohort = multisource.prevalidate_cohort(
        {
            "schema_version": 1,
            "kind": "cre_capacity_multisource_v1_receipts",
            "receipts": [*rows, incomplete, retried],
        }
    )

    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["current_active_successes"] == 18
    assert jll["individually_qualified_rows"] == 16
    assert jll["fidelity_failures"] == 1
    assert jll["retry_count"] == 1
    assert len(jll["calibration"]) == 8
    assert len(jll["core"]) == 16
    assert cohort["planes"]["strict_detail"]["sources_in_matrix"] == 12
    assert cohort["planes"]["authoritative_inventory"]["sources_in_matrix"] == 8
    assert len(cohort["cohort_sha256"]) == 64


def test_prevalidation_rejects_an_enumeration_hash_not_bound_to_its_target(
    tmp_path: Path,
) -> None:
    receipt = _receipt(tmp_path, source_key="jll")
    receipt["canonical_url"] = "https://property.jll.com/listing/swapped-target"
    with pytest.raises(multisource.MultisourceError, match="fresh enumeration"):
        multisource.prevalidate_cohort(
            {
                "schema_version": 1,
                "kind": "cre_capacity_multisource_v1_receipts",
                "receipts": [receipt],
            }
        )


def test_prevalidation_rejects_unbound_config_and_unredacted_headers(
    tmp_path: Path,
) -> None:
    receipt = _receipt(tmp_path, source_key="jll")
    receipt["config_sha256"] = "0" * 64
    with pytest.raises(multisource.MultisourceError, match="configuration digest"):
        multisource.prevalidate_cohort(
            {
                "schema_version": 1,
                "kind": "cre_capacity_multisource_v1_receipts",
                "receipts": [receipt],
            }
        )

    receipt = _receipt(tmp_path, source_key="jll")
    receipt["redacted_headers"] = {"authorization": "Bearer should-not-survive"}
    with pytest.raises(multisource.MultisourceError, match="safely redacted"):
        multisource.prevalidate_cohort(
            {
                "schema_version": 1,
                "kind": "cre_capacity_multisource_v1_receipts",
                "receipts": [receipt],
            }
        )
