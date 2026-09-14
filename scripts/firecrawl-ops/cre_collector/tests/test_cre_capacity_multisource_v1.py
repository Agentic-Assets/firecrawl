"""Offline contracts for the deliberately non-generic multisource-v1 cohort."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cre_capacity_multisource_v1 as multisource
import pytest

ADMISSION_NOW = datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc)
OBSERVED_AT = "2026-09-14T12:00:00Z"


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(root: Path, name: str, value: Any) -> tuple[Path, str]:
    path = root / name
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, _hash(raw)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _source(key: str) -> dict[str, Any]:
    return next(
        item for item in multisource.load_config()["sources"] if item["key"] == key
    )


def _jll_raw_html(*, property_value: dict[str, Any] | None) -> str:
    page_props: dict[str, Any] = {"property": property_value}
    if property_value is None:
        page_props.update({"notFound": True, "error": {"statusCode": 404}})
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps({'props': {'pageProps': page_props}}, separators=(',', ':'))}"
        "</script>"
    )


def _raw_body(source_key: str, provider_id: str, classification: str) -> dict[str, Any]:
    if classification == "confirmed_current_attrition":
        return {"rawHtml": _jll_raw_html(property_value=None)}
    if source_key == "jll":
        return {
            "rawHtml": _jll_raw_html(
                property_value={
                    "id": provider_id,
                    "address": "1 Test Street",
                    "title": "Test listing",
                }
            )
        }
    return {"rawHtml": "<html>listing</html>"}


def _locator_fields(fields: dict[str, Any]) -> dict[str, dict[str, str]]:
    source_fields = {"address": "address", "name": "title"}
    return {
        key: {
            "source_path": f"property.{source_fields[key]}",
            "value_sha256": multisource._sha256(multisource._canonical(value)),
        }
        for key, value in fields.items()
    }


def _extractor_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "source_key",
            "provider_id",
            "canonical_url",
            "request_url",
            "final_url",
            "http_status",
            "content_type",
            "observed_at",
            "timing_ms",
            "raw_receipt_sha256",
            "parser_sha256",
            "normalized_sha256",
            "field_locator_sha256",
            "asset_evidence_sha256",
        )
    }


def _refresh_extractor(root: Path, row: dict[str, Any]) -> None:
    path, digest = _write(
        root,
        Path(row["extractor_receipt_path"]).name,
        _extractor_document(row),
    )
    row["extractor_receipt_path"] = str(path)
    row["extractor_receipt_sha256"] = digest


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
    enumeration_request = f"https://{source['hosts'][0]}/enumeration"
    enum_path, enum_hash = _write(
        root,
        f"enumeration-{source_key}.json",
        {
            "observed_at": OBSERVED_AT,
            "total": max(count, 1),
            "complete": True,
            "truncated": False,
            "provider_ids": provider_ids,
            "body": body,
            "request_url": enumeration_request,
            "final_url": enumeration_request,
            "http_status": 200,
            "content_type": "application/json",
            "timing_ms": 10,
        },
    )
    config = multisource.load_config()
    config_hash = multisource._sha256(multisource._canonical(config))
    rows = []
    for number, provider_id in enumerate(provider_ids):
        canonical_url = f"https://{source['hosts'][0]}/listing/{provider_id}"
        http_status = 404 if classification == "confirmed_current_attrition" else 200
        raw_path, raw_hash = _write(
            root,
            f"raw-{source_key}-{number}.json",
            {
                "request_url": canonical_url,
                "final_url": canonical_url,
                "http_status": http_status,
                "content_type": "application/json",
                "observed_at": OBSERVED_AT,
                "timing_ms": 12 + number,
                "body": _raw_body(source_key, provider_id, classification),
            },
        )
        fields = {"address": "1 Test Street", "name": "Test listing"}
        normalized_path, normalized_hash = _write(
            root,
            f"normalized-{source_key}-{number}.json",
            {
                "provider_id": provider_id,
                "canonical_url": canonical_url,
                "fields": fields,
            },
        )
        locator_path, locator_hash = _write(
            root,
            f"locators-{source_key}-{number}.json",
            {"provider_id": provider_id, "fields": _locator_fields(fields)},
        )
        assets_path, assets_hash = _write(
            root,
            f"assets-{source_key}-{number}.json",
            {"provider_id": provider_id, "assets": []},
        )
        row: dict[str, Any] = {
            "source_key": source_key,
            "provider_id": provider_id,
            "enumeration_observed_at": OBSERVED_AT,
            "enumeration_receipt_path": str(enum_path),
            "enumeration_receipt_sha256": enum_hash,
            "enumeration_body_sha256": _hash(body.encode()),
            "enumeration_total": max(count, 1),
            "canonical_url": canonical_url,
            "request_url": canonical_url,
            "final_url": canonical_url,
            "http_status": http_status,
            "content_type": "application/json",
            "redacted_headers": {"content-type": "application/json"},
            "timing_ms": 12 + number,
            "retry_count": 0,
            "raw_receipt_path": str(raw_path),
            "raw_receipt_sha256": raw_hash,
            "observed_at": OBSERVED_AT,
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
        row["extractor_receipt_path"] = str(
            root / f"extractor-{source_key}-{number}.json"
        )
        _refresh_extractor(root, row)
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


def _prevalidate(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return multisource.prevalidate_cohort(_document(root, rows), now_utc=ADMISSION_NOW)


def _private_dir(root: Path, name: str) -> Path:
    child = root / name
    child.mkdir()
    child.chmod(0o700)
    return child


def _rebind_enumeration(root: Path, rows: list[dict[str, Any]], **changes: Any) -> None:
    document = _read(Path(rows[0]["enumeration_receipt_path"]))
    document.update(changes)
    path, digest = _write(
        root, Path(rows[0]["enumeration_receipt_path"]).name, document
    )
    body_hash = _hash(document["body"].encode())
    for row in rows:
        row["enumeration_receipt_path"] = str(path)
        row["enumeration_receipt_sha256"] = digest
        row["enumeration_body_sha256"] = body_hash
        row["enumeration_observed_at"] = document["observed_at"]
        row["enumeration_total"] = document["total"]
        row["enumeration_identity_sha256"] = multisource._enumeration_identity(
            row["source_key"],
            row["provider_id"],
            row["canonical_url"],
            row["enumeration_observed_at"],
            digest,
            body_hash,
            row["enumeration_total"],
        )


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
    assert "api-public.nim.nmrk.com" in sources["newmark"]["hosts"]
    assert "srsre-next-412955565034.us-central1.run.app" in sources["srs"]["hosts"]
    assert config["profiles"]["P2"]["future_executor_provider_family_exclusions"] == [
        "buildout",
        "cbre",
        "colliers",
        "jll",
    ]


def test_jll_only_attrition_requires_private_fresh_provider_specific_proof(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-attrition")
    receipt = _receipt_batch(
        root, source_key="jll", classification="confirmed_current_attrition"
    )
    cohort = _prevalidate(root, receipt)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["confirmed_current_attrition"] == 1
    assert jll["core_state"] == "insufficient_current_detail_eligibility"
    assert cohort["aggregate"]["state"] == "incomplete_screen"

    root = _private_dir(evidence_root, "non-jll-attrition")
    non_jll = _receipt_batch(
        root, source_key="cbre", classification="confirmed_current_attrition"
    )
    with pytest.raises(
        multisource.MultisourceError, match="provider-specific attrition"
    ):
        _prevalidate(root, non_jll)


def test_prevalidation_uses_artifact_bound_fidelity_and_deterministic_strata(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "strata")
    rows = _receipt_batch(root, source_key="jll", count=25)
    cohort = _prevalidate(root, rows)
    reversed_cohort = _prevalidate(root, list(reversed(rows)))
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
        _prevalidate(root, rows)


def test_all_twenty_sources_remain_incomplete_without_source_specific_verifiers(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "all-sources")
    rows = []
    for source in multisource.load_config()["sources"]:
        rows.extend(_receipt_batch(root, source_key=source["key"], count=16))
    cohort = _prevalidate(root, rows)

    assert cohort["aggregate"]["state"] == "incomplete_screen"
    assert cohort["planes"]["strict_detail"]["sources_core_ready"] == 1
    assert cohort["planes"]["authoritative_inventory"]["sources_core_ready"] == 0
    assert cohort["aggregate"]["cross_plane_aggregation"] == (
        "not_computed_distinct_plane_estimands"
    )
    assert cohort["planes"]["strict_detail"]["estimand"] == (
        "individually_qualified_rows_per_minute"
    )


def test_stale_or_partial_enumeration_cannot_claim_complete(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "partial")
    rows = _receipt_batch(root, source_key="jll", count=16)
    _rebind_enumeration(root, rows, total=100)
    with pytest.raises(multisource.MultisourceError, match="completeness"):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "stale")
    rows = _receipt_batch(root, source_key="jll", count=16)
    _rebind_enumeration(root, rows, observed_at="2000-01-01T00:00:00Z")
    with pytest.raises(multisource.MultisourceError, match="freshness window"):
        _prevalidate(root, rows)


def test_core_never_silently_substitutes_a_smaller_qualified_sample(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "underfilled-core")
    rows = _receipt_batch(root, source_key="jll", count=25)
    for row in rows[16:]:
        row["classification"] = "detail_unavailable_by_design"
    cohort = _prevalidate(root, rows)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")

    assert jll["core_state"] == "core_sample_underfilled"
    assert jll["core_target_rows"] == 24
    assert jll["core_selected_rows"] == 16
    assert jll["core"] == []


def test_invented_fidelity_and_challenge_block_source_admission(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "invented")
    rows = _receipt_batch(root, source_key="jll", count=16)
    row = rows[0]
    normalized = _read(Path(row["normalized_path"]))
    normalized["fields"]["address"] = "Invented address"
    _, row["normalized_sha256"] = _write(
        root, Path(row["normalized_path"]).name, normalized
    )
    locators = _read(Path(row["field_locator_path"]))
    locators["fields"] = _locator_fields(normalized["fields"])
    _, row["field_locator_sha256"] = _write(
        root, Path(row["field_locator_path"]).name, locators
    )
    _refresh_extractor(root, row)
    cohort = _prevalidate(root, rows)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["core_state"] == "semantic_fidelity_unverified"
    assert jll["core"] == []

    root = _private_dir(evidence_root, "challenge")
    jll_rows = _receipt_batch(root, source_key="jll", count=16)
    investor_rows = _receipt_batch(
        root, source_key="jll-investor", classification="challenge_or_throttle"
    )
    cohort = _prevalidate(root, jll_rows + investor_rows)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["core_state"] == "challenge_or_throttle_in_family"
    assert jll["core"] == []


def test_unsafe_url_and_calibration_strata_tampering_are_detected(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "credential-url")
    rows = _receipt_batch(root, source_key="jll", count=16)
    rows[0]["request_url"] = "https://user:pass@property.jll.com/listing/jll-00"
    with pytest.raises(multisource.MultisourceError, match="provider host contract"):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "calibration-hash")
    rows = _receipt_batch(root, source_key="jll", count=25)
    original = _prevalidate(root, rows)
    rows[0]["stratum"]["page_weight_band"] = "changed"
    changed = _prevalidate(root, rows)
    assert original["cohort_sha256"] != changed["cohort_sha256"]


def test_malicious_or_incomplete_evidence_is_rejected_and_output_is_sanitized(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "sanitized")
    rows = _receipt_batch(root, source_key="jll", count=16)
    cohort = _prevalidate(root, rows)
    rendered = json.dumps(cohort)
    assert str(root) not in rendered
    assert "https://" not in rendered
    assert "content-type" not in rendered

    rows[0]["enumeration_body_sha256"] = "0" * 64
    with pytest.raises(multisource.MultisourceError, match="enumeration body hash"):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "unprivate")
    rows = _receipt_batch(root, source_key="jll")
    rows[0]["raw_receipt_path"] = "/tmp/not-private.json"
    with pytest.raises(multisource.MultisourceError, match="receipt is"):
        _prevalidate(root, rows)

    root.chmod(0o755)
    with pytest.raises(multisource.MultisourceError, match="private mode 0700"):
        _prevalidate(root, rows)
