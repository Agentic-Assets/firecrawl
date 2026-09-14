"""Offline contracts for the deliberately non-generic multisource-v1 cohort."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cre_capacity_multisource_v1 as multisource
import pytest


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(root: Path, name: str, value: Any) -> tuple[Path, str]:
    path = root / name
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, _hash(raw)


def _source(key: str) -> dict[str, Any]:
    return next(
        item for item in multisource.load_config()["sources"] if item["key"] == key
    )


def _receipt_batch(
    root: Path,
    *,
    source_key: str,
    count: int = 1,
    classification: str = "eligible_detail",
) -> list[dict[str, Any]]:
    source = _source(source_key)
    provider_ids = [f"{source_key}-{number:02d}" for number in range(count)]
    body = json.dumps({"source": source_key, "provider_ids": provider_ids})
    enum_path, enum_hash = _write(
        root,
        f"enumeration-{source_key}.json",
        {
            "observed_at": "2026-09-14T12:00:00Z",
            "total": max(count, 1),
            "complete": True,
            "truncated": False,
            "provider_ids": provider_ids,
            "body": body,
        },
    )
    config = multisource.load_config()
    config_hash = multisource._sha256(multisource._canonical(config))
    rows = []
    for number, provider_id in enumerate(provider_ids):
        canonical_url = f"https://{source['hosts'][0]}/listing/{provider_id}"
        raw: dict[str, Any] = {"rawHtml": "<html>listing</html>"}
        if classification == "confirmed_current_attrition":
            raw = {
                "rawHtml": (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    '{"props":{"pageProps":{"notFound":true,"error":{"statusCode":404}}}}'
                    "</script>"
                )
            }
        raw_path, raw_hash = _write(root, f"raw-{source_key}-{number}.json", raw)
        normalized_path, normalized_hash = _write(
            root,
            f"normalized-{source_key}-{number}.json",
            {
                "provider_id": provider_id,
                "canonical_url": canonical_url,
                "fields": {"address": "1 Test Street", "name": "Test listing"},
            },
        )
        locator_path, locator_hash = _write(
            root,
            f"locators-{source_key}-{number}.json",
            {"provider_id": provider_id, "fields": {"address": "$.address"}},
        )
        assets_path, assets_hash = _write(
            root,
            f"assets-{source_key}-{number}.json",
            {"provider_id": provider_id, "assets": []},
        )
        row: dict[str, Any] = {
            "source_key": source_key,
            "provider_id": provider_id,
            "enumeration_observed_at": "2026-09-14T12:00:00Z",
            "enumeration_receipt_path": str(enum_path),
            "enumeration_receipt_sha256": enum_hash,
            "enumeration_body_sha256": _hash(body.encode()),
            "enumeration_total": max(count, 1),
            "canonical_url": canonical_url,
            "final_url": canonical_url,
            "http_status": 404
            if classification == "confirmed_current_attrition"
            else 200,
            "content_type": "application/json",
            "redacted_headers": {"content-type": "application/json"},
            "timing_ms": 12 + number,
            "retry_count": 0,
            "raw_receipt_path": str(raw_path),
            "raw_receipt_sha256": raw_hash,
            "normalized_path": str(normalized_path),
            "normalized_sha256": normalized_hash,
            "field_locator_path": str(locator_path),
            "field_locator_sha256": locator_hash,
            "asset_evidence_path": str(assets_path),
            "asset_evidence_sha256": assets_hash,
            "parser_sha256": "b" * 64,
            "config_sha256": config_hash,
            "source_config_sha256": multisource._source_config_sha256(source),
            "classification": classification,
            "stratum": {
                "transaction_class": "sale" if number % 2 else "lease",
                "property_type": "office" if number % 3 else "industrial",
                "page_weight_band": "small" if number % 4 else "large",
            },
        }
        row["enumeration_identity_sha256"] = multisource._enumeration_identity(
            source_key,
            provider_id,
            canonical_url,
            row["enumeration_observed_at"],
            enum_hash,
            row["enumeration_body_sha256"],
            row["enumeration_total"],
        )
        if classification == "confirmed_current_attrition":
            row["not_found_classifier"] = "jll_next_data_404_no_property"
        rows.append(row)
    return rows


def _document(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "cre_capacity_multisource_v1_receipts",
        "receipt_root": str(root),
        "receipts": rows,
    }


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    root = tmp_path / "restricted"
    root.mkdir()
    root.chmod(0o700)
    return root


def test_fixed_matrix_profiles_planes_and_real_adapter_hosts_are_explicit() -> None:
    config = multisource.load_config()
    sources = {source["key"]: source for source in config["sources"]}

    assert len(sources) == 20
    assert {key: tuple(source["hosts"]) for key, source in sources.items()} == (
        multisource.EXPECTED_SOURCE_HOSTS
    )
    assert sources["jll"]["plane"] == "strict_detail"
    assert sources["cbre"]["plane"] == "authoritative_inventory"
    assert sources["colliers-main"]["exclusive"] is True
    assert sources["jll-investor"]["hosts"] == ["invest.jll.com"]
    assert sources["cbre-dealflow"]["hosts"] == ["www.cbredealflow.com"]
    assert sources["colliers"]["hosts"] == ["sales.colliers.com", "my.rcm1.com"]
    assert config["profiles"]["P2"]["provider_family_exclusions"] == [
        "buildout",
        "cbre",
        "colliers",
        "jll",
    ]


def test_jll_only_attrition_requires_private_fresh_provider_specific_proof(
    evidence_root: Path,
) -> None:
    receipt = _receipt_batch(
        evidence_root, source_key="jll", classification="confirmed_current_attrition"
    )
    cohort = multisource.prevalidate_cohort(_document(evidence_root, receipt))
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["confirmed_current_attrition"] == 1
    assert jll["core_state"] == "insufficient_current_detail_eligibility"
    assert cohort["aggregate"]["state"] == "incomplete_screen"

    non_jll = _receipt_batch(
        evidence_root, source_key="cbre", classification="confirmed_current_attrition"
    )
    with pytest.raises(
        multisource.MultisourceError, match="provider-specific attrition"
    ):
        multisource.prevalidate_cohort(_document(evidence_root, non_jll))


def test_prevalidation_uses_artifact_bound_fidelity_and_deterministic_strata(
    evidence_root: Path,
) -> None:
    rows = _receipt_batch(evidence_root, source_key="jll", count=25)
    cohort = multisource.prevalidate_cohort(_document(evidence_root, rows))
    reversed_cohort = multisource.prevalidate_cohort(
        _document(evidence_root, list(reversed(rows)))
    )
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    reversed_jll = next(
        source for source in reversed_cohort["sources"] if source["source_key"] == "jll"
    )
    assert jll["current_active_successes"] == 25
    assert jll["individually_qualified_rows"] == 25
    assert len(jll["calibration"]) == 8
    assert len(jll["core"]) == 24
    assert jll["core"] == reversed_jll["core"]
    assert jll["fresh_enumeration"]["total_population"] == 25
    assert jll["row_rates"] == {
        "current_active_successes": 1.0,
        "confirmed_current_attrition": 0.0,
        "individually_qualified_rows": 1.0,
        "parser_failures": 0.0,
        "transport_failures": 0.0,
        "fidelity_failures": 0.0,
    }

    rows[0]["structured_fidelity"] = {"complete": True}
    with pytest.raises(multisource.MultisourceError, match="unsupported"):
        multisource.prevalidate_cohort(_document(evidence_root, rows))


def test_all_twenty_sources_and_both_planes_are_required_for_ready(
    evidence_root: Path,
) -> None:
    rows = []
    for source in multisource.load_config()["sources"]:
        rows.extend(_receipt_batch(evidence_root, source_key=source["key"], count=16))
    cohort = multisource.prevalidate_cohort(_document(evidence_root, rows))

    assert cohort["aggregate"]["state"] == "ready_for_review"
    assert cohort["planes"]["strict_detail"]["sources_core_ready"] == 12
    assert cohort["planes"]["authoritative_inventory"]["sources_core_ready"] == 8
    assert (
        cohort["aggregate"]["workload_weighting"]["basis"] == "fresh_enumeration_total"
    )


def test_malicious_or_incomplete_evidence_is_rejected_and_output_is_sanitized(
    evidence_root: Path,
) -> None:
    rows = _receipt_batch(evidence_root, source_key="jll", count=16)
    cohort = multisource.prevalidate_cohort(_document(evidence_root, rows))
    rendered = json.dumps(cohort)
    assert str(evidence_root) not in rendered
    assert "https://" not in rendered
    assert "content-type" not in rendered

    rows[0]["enumeration_body_sha256"] = "0" * 64
    with pytest.raises(multisource.MultisourceError, match="enumeration body hash"):
        multisource.prevalidate_cohort(_document(evidence_root, rows))

    rows = _receipt_batch(evidence_root, source_key="jll")
    rows[0]["raw_receipt_path"] = "/tmp/not-private.json"
    with pytest.raises(multisource.MultisourceError, match="receipt is"):
        multisource.prevalidate_cohort(_document(evidence_root, rows))

    evidence_root.chmod(0o755)
    with pytest.raises(multisource.MultisourceError, match="private mode 0700"):
        multisource.prevalidate_cohort(_document(evidence_root, rows))
