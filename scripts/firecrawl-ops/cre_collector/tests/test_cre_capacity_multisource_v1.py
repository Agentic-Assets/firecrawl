"""Offline contracts for the deliberately non-generic multisource-v1 cohort."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import cre_capacity_multisource_v1 as multisource

ADMISSION_NOW = datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc)
OBSERVED_AT = "2026-09-14T12:00:00Z"


def _jll_search_query() -> str:
    source = (Path(__file__).parents[1] / "sources" / "jll.ts").read_text()
    match = re.search(
        r"export const JLL_SEARCH_RESULTS_QUERY = `(.*?)`;", source, re.DOTALL
    )
    assert match is not None
    return match.group(1)


JLL_SEARCH_QUERY = _jll_search_query()


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


def _raw_body(
    source_key: str,
    provider_id: str,
    classification: str,
    *,
    canonical_url: str,
    transaction_type: str,
    property_type: str,
) -> dict[str, Any]:
    if classification == "confirmed_current_attrition":
        return {"rawHtml": _jll_raw_html(property_value=None)}
    if source_key == "jll":
        return {
            "rawHtml": _jll_raw_html(
                property_value={
                    "id": provider_id,
                    "pageUrl": canonical_url,
                    "address": "1 Test Street",
                    "title": "Test listing",
                    "tenureTypes": [transaction_type],
                    "propertyTypes": [property_type],
                }
            )
        }
    return {"rawHtml": "<html>listing</html>"}


def _locator_fields(fields: dict[str, Any]) -> dict[str, dict[str, str]]:
    source_fields = {
        "address": "address",
        "name": "title",
        "transaction_type": "tenureTypes[0]",
        "property_type": "propertyTypes[0]",
    }
    return {
        key: (
            {
                "presence": "present",
                "source_path": f"property.{source_fields[key]}",
                "value_sha256": multisource._sha256(
                    multisource._canonical(fields[key])
                ),
            }
            if key in fields
            else {"presence": "absent"}
        )
        for key in multisource._JLL_SEMANTIC_FIELDS
    }


def _jll_asset_evidence(
    property_value: dict[str, Any] | None = None,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    property_value = property_value or {}
    normalized: dict[str, list[str]] = {}
    channels: dict[str, Any] = {}
    for channel, source_path in multisource._JLL_ASSET_CHANNELS.items():
        raw_present = channel in property_value
        candidates = (
            multisource._jll_asset_values(channel, property_value.get(channel))
            if raw_present
            else []
        )
        valid = [
            url for item in candidates if (url := multisource._public_asset_url(item))
        ]
        normalized[channel] = valid
        channels[channel] = {
            "source_path": source_path,
            "presence": "present" if valid else "empty" if raw_present else "absent",
            "raw_valid_url_set_sha256": multisource._url_set_hash(valid),
            "accepted_public_url_set_sha256": multisource._url_set_hash(valid),
            "normalized_mapped_url_set_sha256": multisource._url_set_hash(valid),
            "rejected_invalid_count": len(candidates) - len(valid),
        }
    return normalized, {"channels": channels}


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


def _rewrite_raw_receipt(
    root: Path, row: dict[str, Any], document: dict[str, Any]
) -> None:
    path, digest = _write(root, Path(row["raw_receipt_path"]).name, document)
    row["raw_receipt_path"] = str(path)
    row["raw_receipt_sha256"] = digest


def _jll_enumeration(
    root: Path,
    provider_ids: list[str],
    *,
    filters: list[tuple[str, str, list[str]]] | None = None,
) -> tuple[Path, str, str, int]:
    """Create the sealed aggregate plus every native JLL GraphQL page receipt."""
    if filters is None:
        filters = [
            (property_type, "sale", provider_ids)
            for property_type in sorted(multisource._JLL_ENUMERATION_PROPERTY_TYPES)
        ]
    manifests = []
    resolution_manifests = []
    graphql_url = "https://property.jll.com/api/graphql"
    entries = {
        provider_id: (
            f"search-{provider_id}",
            f"https://property.jll.com/listings/search-{provider_id}",
        )
        for provider_id in provider_ids
    }
    for filter_number, (property_type, tenure_type, filter_ids) in enumerate(filters):
        for page_number, skip in enumerate(range(0, len(filter_ids), 50) or (0,)):
            page_ids = filter_ids[skip : skip + 50]
            body = json.dumps(
                {
                    "data": {
                        "properties": {
                            "count": len(filter_ids),
                            "items": [
                                {
                                    "id": entries[provider_id][0],
                                    "pageUrl": entries[provider_id][1],
                                }
                                for provider_id in page_ids
                            ],
                        }
                    }
                },
                separators=(",", ":"),
            )
            page_path, page_hash = _write(
                root,
                f"jll-graphql-{filter_number}-{page_number}.json",
                {
                    "kind": "jll_graphql_page_receipt_v1",
                    "request_url": graphql_url,
                    "final_url": graphql_url,
                    "http_status": 200,
                    "content_type": "application/json; charset=utf-8",
                    "observed_at": OBSERVED_AT,
                    "timing_ms": 10,
                    "operation_name": "SearchResults",
                    "variables": {
                        "market": "us",
                        "language": "en",
                        "propertyTypes": [property_type],
                        "tenureTypes": [tenure_type],
                        "skip": skip,
                        "take": 50,
                        "orderBy": {
                            "field": "dateModified",
                            "direction": "desc",
                            "imagePriority": True,
                        },
                    },
                    "request_body": json.dumps(
                        {
                            "query": JLL_SEARCH_QUERY,
                            "variables": {
                                "market": "us",
                                "language": "en",
                                "propertyTypes": [property_type],
                                "tenureTypes": [tenure_type],
                                "skip": skip,
                                "take": 50,
                                "orderBy": {
                                    "field": "dateModified",
                                    "direction": "desc",
                                    "imagePriority": True,
                                },
                            },
                            "operationName": "SearchResults",
                        },
                        separators=(",", ":"),
                    ),
                    "query_sha256": _hash(JLL_SEARCH_QUERY.encode()),
                    "body": body,
                },
            )
            manifests.append({"path": str(page_path), "sha256": page_hash})
    for number, provider_id in enumerate(provider_ids):
        search_id, canonical_url = entries[provider_id]
        detail_path, detail_hash = _write(
            root,
            f"jll-detail-{number}.json",
            {
                "kind": "jll_detail_page_receipt_v1",
                "request_url": canonical_url,
                "final_url": canonical_url,
                "http_status": 200,
                "content_type": "text/html; charset=utf-8",
                "observed_at": OBSERVED_AT,
                "timing_ms": 10,
                "body": _jll_raw_html(
                    property_value={"id": provider_id, "pageUrl": canonical_url}
                ),
            },
        )
        resolution_path, resolution_hash = _write(
            root,
            f"jll-resolution-{number}.json",
            {
                "kind": "jll_detail_resolution_receipt_v1",
                "search_id": search_id,
                "canonical_url": canonical_url,
                "detail_receipt_path": str(detail_path),
                "detail_receipt_sha256": detail_hash,
            },
        )
        resolution_manifests.append(
            {"path": str(resolution_path), "sha256": resolution_hash}
        )
    aggregate = {
        "kind": "jll_graphql_enumeration_aggregate_v1",
        "observed_at": OBSERVED_AT,
        "total": len(provider_ids),
        "complete": True,
        "truncated": False,
        "provider_ids": provider_ids,
        "page_receipts": manifests,
        "resolution_receipts": resolution_manifests,
    }
    path, digest = _write(root, "enumeration-jll.json", aggregate)
    return path, digest, _hash(multisource._canonical(manifests)), len(provider_ids)


def _receipt_batch(
    root: Path,
    *,
    source_key: str,
    count: int = 1,
    classification: str = "eligible_detail",
    jll_filters: list[tuple[str, str, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    source = _source(source_key)
    provider_ids = (
        [str(1_000_000 + number) for number in range(count)]
        if source_key == "jll"
        else [f"{source_key}-{number:02d}" for number in range(count)]
    )
    if source_key == "jll":
        enum_path, enum_hash, body_hash, enumeration_total = _jll_enumeration(
            root, provider_ids, filters=jll_filters
        )
    else:
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
        body_hash = _hash(body.encode())
        enumeration_total = max(count, 1)
    config = multisource.load_config()
    config_hash = multisource._sha256(multisource._canonical(config))
    rows = []
    for number, provider_id in enumerate(provider_ids):
        canonical_url = (
            f"https://{source['hosts'][0]}/listings/search-{provider_id}"
            if source_key == "jll"
            else f"https://{source['hosts'][0]}/properties/?propertyId={provider_id}-sale"
            if source["provider_family"] == "buildout"
            else f"https://{source['hosts'][0]}/listings/{provider_id}"
        )
        transaction_type = "sale" if number % 2 else "rent"
        property_type = "office" if number % 3 else "industrial"
        http_status = (
            404
            if classification == "confirmed_current_attrition"
            else 429
            if classification == "challenge_or_throttle"
            else 200
        )
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
                "body": _raw_body(
                    source_key,
                    provider_id,
                    classification,
                    canonical_url=canonical_url,
                    transaction_type=transaction_type,
                    property_type=property_type,
                ),
            },
        )
        fields = {
            "address": "1 Test Street",
            "name": "Test listing",
            "transaction_type": transaction_type,
            "property_type": property_type,
        }
        normalized_assets, asset_evidence = _jll_asset_evidence()
        normalized_path, normalized_hash = _write(
            root,
            f"normalized-{source_key}-{number}.json",
            {
                "provider_id": provider_id,
                "canonical_url": canonical_url,
                "fields": fields,
                "assets": normalized_assets,
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
            {"provider_id": provider_id, **asset_evidence},
        )
        row: dict[str, Any] = {
            "source_key": source_key,
            "provider_id": provider_id,
            "enumeration_observed_at": OBSERVED_AT,
            "enumeration_receipt_path": str(enum_path),
            "enumeration_receipt_sha256": enum_hash,
            "enumeration_body_sha256": body_hash,
            "enumeration_total": enumeration_total,
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


def _bind_enumeration_document(
    root: Path, rows: list[dict[str, Any]], document: dict[str, Any]
) -> None:
    path, digest = _write(
        root, Path(rows[0]["enumeration_receipt_path"]).name, document
    )
    body_hash = (
        _hash(multisource._canonical(document["page_receipts"]))
        if document.get("kind") == "jll_graphql_enumeration_aggregate_v1"
        else _hash(document["body"].encode())
    )
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


def _rebind_enumeration(root: Path, rows: list[dict[str, Any]], **changes: Any) -> None:
    document = _read(Path(rows[0]["enumeration_receipt_path"]))
    document.update(changes)
    _bind_enumeration_document(root, rows, document)


def _rewrite_jll_page(
    root: Path, rows: list[dict[str, Any]], index: int, document: dict[str, Any]
) -> None:
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    page_manifest = aggregate["page_receipts"][index]
    path, digest = _write(root, Path(page_manifest["path"]).name, document)
    aggregate["page_receipts"][index] = {"path": str(path), "sha256": digest}
    _bind_enumeration_document(root, rows, aggregate)


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
    assert {member["stratum"]["transaction_class"] for member in jll["core"]} == {
        "lease",
        "sale",
    }
    assert {member["stratum"]["property_type"] for member in jll["core"]} == {
        "industrial",
        "office",
    }
    assert {member["stratum"]["page_weight_band"] for member in jll["core"]} == {
        "small"
    }
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


def _jll_fidelity_documents(property_value: dict[str, Any]):
    fields = {
        "address": property_value["address"],
        "name": property_value["title"],
        "transaction_type": property_value["tenureTypes"][0],
        "property_type": property_value["propertyTypes"][0],
    }
    normalized_assets, asset_evidence = _jll_asset_evidence(property_value)
    return (
        {"fields": fields, "assets": normalized_assets},
        {"fields": _locator_fields(fields)},
        asset_evidence,
        {"body": {"rawHtml": _jll_raw_html(property_value=property_value)}},
    )


def test_jll_semantic_contract_rejects_omitted_or_name_only_core_mapping() -> None:
    property_value = {
        "id": "strict",
        "pageUrl": "https://property.jll.com/listings/strict",
        "address": "1 Test Street",
        "title": "Strict listing",
        "tenureTypes": ["sale"],
        "propertyTypes": ["office"],
    }
    normalized, locators, assets, raw = _jll_fidelity_documents(property_value)
    assert multisource._verified_jll_locator_fidelity(normalized, locators, assets, raw)

    normalized = {
        **normalized,
        "fields": {
            key: value
            for key, value in normalized["fields"].items()
            if key != "address"
        },
    }
    assert not multisource._verified_jll_locator_fidelity(
        normalized, locators, assets, raw
    )


def test_jll_name_only_twenty_four_row_fixture_is_screening_only(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-name-only")
    rows = _receipt_batch(root, source_key="jll", count=25)
    for row in rows:
        raw = _read(Path(row["raw_receipt_path"]))
        property_value = multisource._jll_next_property(raw)
        assert property_value is not None
        name_only = dict(property_value)
        name_only.pop("address")
        raw["body"] = {"rawHtml": _jll_raw_html(property_value=name_only)}
        _rewrite_raw_receipt(root, row, raw)
        fields = {
            key: value
            for key, value in _read(Path(row["normalized_path"]))["fields"].items()
            if key != "address"
        }
        normalized_assets, asset_evidence = _jll_asset_evidence(name_only)
        normalized_path, normalized_hash = _write(
            root,
            Path(row["normalized_path"]).name,
            {
                "provider_id": row["provider_id"],
                "canonical_url": row["canonical_url"],
                "fields": fields,
                "assets": normalized_assets,
            },
        )
        locator_path, locator_hash = _write(
            root,
            Path(row["field_locator_path"]).name,
            {"provider_id": row["provider_id"], "fields": _locator_fields(fields)},
        )
        assets_path, assets_hash = _write(
            root,
            Path(row["asset_evidence_path"]).name,
            {"provider_id": row["provider_id"], **asset_evidence},
        )
        row.update(
            {
                "normalized_path": str(normalized_path),
                "normalized_sha256": normalized_hash,
                "field_locator_path": str(locator_path),
                "field_locator_sha256": locator_hash,
                "asset_evidence_path": str(assets_path),
                "asset_evidence_sha256": assets_hash,
            }
        )
        _refresh_extractor(root, row)

    cohort = _prevalidate(root, rows)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["core_state"] == "semantic_fidelity_unverified"
    assert jll["core"] == []

    name_only = dict(property_value)
    name_only.pop("address")
    fields = {
        key: value
        for key, value in _jll_fidelity_documents(property_value)[0]["fields"].items()
        if key != "address"
    }
    normalized_assets, asset_evidence = _jll_asset_evidence(name_only)
    assert not multisource._verified_jll_locator_fidelity(
        {"fields": fields, "assets": normalized_assets},
        {"fields": _locator_fields(fields)},
        asset_evidence,
        {"body": {"rawHtml": _jll_raw_html(property_value=name_only)}},
    )


def test_jll_asset_contract_binds_every_native_channel_and_rejects_mismatch() -> None:
    property_value = {
        "id": "assets",
        "pageUrl": "https://property.jll.com/listings/assets",
        "address": "1 Test Street",
        "title": "Asset listing",
        "tenureTypes": ["sale"],
        "propertyTypes": ["office"],
        "images": ["https://cdn.example/image.jpg", "not-a-url"],
        "brochures": ["https://cdn.example/brochure.pdf"],
        "floorPlans": {
            "images": [{"image": "https://cdn.example/floor.jpg"}],
            "files": [{"download": "https://cdn.example/floor.pdf"}],
        },
        "videos": [{"url": "https://video.example/watch"}],
        "virtualTours": "https://tour.example/virtual",
        "view360URLs": ["https://tour.example/360"],
    }
    normalized, locators, assets, raw = _jll_fidelity_documents(property_value)
    assert multisource._verified_jll_locator_fidelity(normalized, locators, assets, raw)
    assert assets["channels"]["images"]["rejected_invalid_count"] == 1

    normalized["assets"]["videos"] = ["https://wrong.example/video"]
    assert not multisource._verified_jll_locator_fidelity(
        normalized, locators, assets, raw
    )


def test_jll_asset_contract_accepts_only_bounded_documented_asset_shapes() -> None:
    assert multisource._jll_asset_values(
        "images",
        ["https://cdn.example/image.jpg", {"image": "https://cdn.example/preview.jpg"}],
    ) == ["https://cdn.example/image.jpg", "https://cdn.example/preview.jpg"]
    assert multisource._jll_asset_values(
        "brochures", [{"file": "https://cdn.example/brochure.pdf"}]
    ) == ["https://cdn.example/brochure.pdf"]
    assert multisource._jll_asset_values(
        "floorPlans",
        {
            "images": [{"image": "https://cdn.example/floor.jpg"}],
            "files": [{"download": "https://cdn.example/floor.pdf"}],
        },
    ) == ["https://cdn.example/floor.jpg", "https://cdn.example/floor.pdf"]
    assert (
        multisource._jll_asset_values(
            "videos", {"nested": {"url": "https://unsafe.example/unbounded"}}
        )
        == []
    )
    assert (
        multisource._jll_asset_values(
            "images", [["https://unsafe.example/nested-array"]]
        )
        == []
    )


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


def test_jll_native_aggregate_seals_paginated_pages_and_filter_overlap(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-paginated-overlap")
    provider_ids = [str(1_000_000 + number) for number in range(51)]
    rows = _receipt_batch(
        root,
        source_key="jll",
        count=len(provider_ids),
        jll_filters=[
            (
                property_type,
                "sale",
                provider_ids if property_type == "office" else provider_ids[:10],
            )
            for property_type in sorted(multisource._JLL_ENUMERATION_PROPERTY_TYPES)
        ],
    )
    cohort = _prevalidate(root, rows)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))

    assert len(aggregate["page_receipts"]) == 10
    assert cohort["sources"][0]["fresh_enumeration"]["total_population"] == 51
    first_page = _read(Path(aggregate["page_receipts"][0]["path"]))
    first_card = json.loads(first_page["body"])["data"]["properties"]["items"][0]
    assert first_card["id"] != rows[0]["provider_id"]
    assert rows[0]["provider_id"].isdigit()


def test_jll_aggregate_requires_all_adapter_filters_and_pinned_query(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-filter-contract")
    rows = _receipt_batch(root, source_key="jll", count=16)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    aggregate["page_receipts"] = [
        manifest
        for manifest in aggregate["page_receipts"]
        if json.loads(_read(Path(manifest["path"]))["request_body"])["variables"][
            "propertyTypes"
        ][0]
        != "office"
    ]
    _bind_enumeration_document(root, rows, aggregate)
    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "jll-invented-filter")
    rows = _receipt_batch(root, source_key="jll", count=16)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    page = _read(Path(aggregate["page_receipts"][0]["path"]))
    page["variables"]["propertyTypes"] = ["invented"]
    request = json.loads(page["request_body"])
    request["variables"]["propertyTypes"] = ["invented"]
    page["request_body"] = json.dumps(request, separators=(",", ":"))
    _rewrite_jll_page(root, rows, 0, page)
    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "jll-unrelated-query")
    rows = _receipt_batch(root, source_key="jll", count=16)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    page = _read(Path(aggregate["page_receipts"][0]["path"]))
    request = json.loads(page["request_body"])
    request["query"] = "query SearchResults { unrelated { id } }"
    page["request_body"] = json.dumps(request, separators=(",", ":"))
    page["query_sha256"] = _hash(request["query"].encode())
    _rewrite_jll_page(root, rows, 0, page)
    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)


def test_jll_producer_roundtrip_and_unresolved_detail_fail_closed(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-producer")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    produced = multisource.produce_jll_enumeration_artifacts(
        receipt_root=root,
        page_receipt_paths=[Path(item["path"]) for item in original["page_receipts"]],
        detail_receipt_paths=[
            Path(_read(Path(item["path"]))["detail_receipt_path"])
            for item in original["resolution_receipts"]
        ],
        aggregate_path=root / "produced-aggregate.json",
    )
    _bind_enumeration_document(root, rows, _read(Path(produced["path"])))
    cohort = _prevalidate(root, rows)
    assert cohort["sources"][0]["fresh_enumeration"]["total_population"] == 16

    root = _private_dir(evidence_root, "jll-unresolved-detail")
    rows = _receipt_batch(root, source_key="jll", count=16)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    aggregate["resolution_receipts"].pop()
    _bind_enumeration_document(root, rows, aggregate)
    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)


def test_jll_producer_rejects_existing_or_symlinked_output(
    evidence_root: Path, tmp_path: Path
) -> None:
    root = _private_dir(evidence_root, "jll-producer-output-boundary")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    existing, _ = _write(root, "already-exists.json", {"existing": True})
    with pytest.raises(multisource.MultisourceError, match="must not overwrite"):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=existing,
        )

    target = tmp_path / "outside.json"
    target.write_text("outside")
    linked = root / "linked-output.json"
    linked.symlink_to(target)
    with pytest.raises(multisource.MultisourceError, match="must not overwrite"):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=linked,
        )


@pytest.mark.parametrize("artifact_kind", ["page", "detail"])
def test_jll_producer_rejects_in_root_symlinked_input(
    evidence_root: Path, artifact_kind: str
) -> None:
    root = _private_dir(evidence_root, f"jll-producer-input-{artifact_kind}")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    if artifact_kind == "page":
        linked = root / "linked-page.json"
        linked.symlink_to(pages[0].name)
        pages[0] = linked
    else:
        linked = root / "linked-detail.json"
        linked.symlink_to(details[0].name)
        details[0] = linked

    with pytest.raises(multisource.MultisourceError):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=root / f"{artifact_kind}-symlink.json",
        )


@pytest.mark.parametrize("artifact_kind", ["page", "detail"])
def test_jll_producer_does_not_read_input_replaced_after_validation(
    evidence_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_kind: str,
) -> None:
    root = _private_dir(evidence_root, f"jll-producer-toctou-{artifact_kind}")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    candidate = pages[0] if artifact_kind == "page" else details[0]
    outside = tmp_path / f"outside-{artifact_kind}.json"
    outside.write_text("outside data must never be read")
    original_open = multisource.os.open
    original_read = multisource.os.read
    replaced = False
    read_after_replacement = False

    def replace_after_lstat(path: Any, flags: int, *args: Any) -> int:
        nonlocal replaced
        if path == candidate and not replaced:
            candidate.unlink()
            candidate.symlink_to(outside)
            replaced = True
        return original_open(path, flags, *args)

    def reject_read_after_replacement(descriptor: int, size: int) -> bytes:
        nonlocal read_after_replacement
        if replaced:
            read_after_replacement = True
            raise AssertionError("producer read the replacement after validation")
        return original_read(descriptor, size)

    monkeypatch.setattr(multisource.os, "open", replace_after_lstat)
    monkeypatch.setattr(multisource.os, "read", reject_read_after_replacement)
    with pytest.raises(multisource.MultisourceError):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=root / f"{artifact_kind}-toctou.json",
        )

    assert replaced
    assert not read_after_replacement


def test_jll_producer_root_descriptor_failure_leaves_no_staging_directory(
    evidence_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_dir(evidence_root, "jll-producer-root-descriptor")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    baseline = {path.name for path in root.iterdir()}
    original_open = multisource.os.open

    def reject_root_descriptor(path: Any, flags: int, *args: Any) -> int:
        if path == root:
            raise OSError("root descriptor denied")
        return original_open(path, flags, *args)

    monkeypatch.setattr(multisource.os, "open", reject_root_descriptor)
    with pytest.raises(multisource.MultisourceError, match="directory is unavailable"):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=root / "root-descriptor.json",
        )

    assert {path.name for path in root.iterdir()} == baseline


def test_jll_producer_preserves_replacement_between_link_and_ownership_capture(
    evidence_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_dir(evidence_root, "jll-producer-publication-race")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    replacement = {"replacement": "must survive publication ownership check"}
    original_link = multisource.os.link
    raced_path: Path | None = None

    def replace_after_link(
        source: Any, destination: Any, *, follow_symlinks: bool = True
    ) -> None:
        nonlocal raced_path
        original_link(source, destination, follow_symlinks=follow_symlinks)
        if raced_path is None:
            raced_path = Path(destination)
            raced_path.unlink()
            _write(root, raced_path.name, replacement)

    monkeypatch.setattr(multisource.os, "link", replace_after_link)
    with pytest.raises(
        multisource.MultisourceError, match="output changed during publication"
    ):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=root / "publication-race.json",
        )

    assert raced_path is not None
    assert _read(raced_path) == replacement
    assert not list(root.glob(".jll-produce-*"))


def test_jll_producer_failure_cleanup_preserves_replaced_output(
    evidence_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_dir(evidence_root, "jll-producer-cleanup-race")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    aggregate_path = root / "raced-aggregate.json"
    replacement = {"replacement": "must survive producer cleanup"}
    verifier = multisource._verified_jll_enumeration_population
    calls = 0

    def fail_after_publishing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            aggregate_path.unlink()
            _write(root, aggregate_path.name, replacement)
            return False
        return verifier(*args, **kwargs)

    monkeypatch.setattr(
        multisource, "_verified_jll_enumeration_population", fail_after_publishing
    )
    with pytest.raises(
        multisource.MultisourceError,
        match=(
            "retained published outputs: "
            + re.escape(str(aggregate_path))
            + " sha256="
            + _hash(
                json.dumps(replacement, sort_keys=True, separators=(",", ":")).encode()
            )
        ),
    ):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=aggregate_path,
        )

    assert _read(aggregate_path) == replacement
    published_resolutions = list(root.glob("raced-aggregate.resolution-*.json"))
    assert len(published_resolutions) == 16
    assert all(path.is_file() for path in published_resolutions)
    assert not list(root.glob(".jll-produce-*"))


def test_jll_producer_prevalidation_and_staging_leave_no_partial_artifacts(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-producer-cleanup")
    rows = _receipt_batch(root, source_key="jll", count=16)
    original = _read(Path(rows[0]["enumeration_receipt_path"]))
    pages = [Path(item["path"]) for item in original["page_receipts"]]
    details = [
        Path(_read(Path(item["path"]))["detail_receipt_path"])
        for item in original["resolution_receipts"]
    ]
    baseline = {path.name for path in root.iterdir()}
    incomplete_pages = [
        path
        for path in pages
        if json.loads(_read(path)["request_body"])["variables"]["propertyTypes"][0]
        != "office"
    ]
    with pytest.raises(multisource.MultisourceError, match="scope is incomplete"):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=incomplete_pages,
            detail_receipt_paths=details,
            aggregate_path=root / "incomplete.json",
        )
    assert {path.name for path in root.iterdir()} == baseline

    broken = _read(details[0])
    broken["content_type"] = "application/json"
    _write(root, details[0].name, broken)
    with pytest.raises(multisource.MultisourceError, match="output is not consumable"):
        multisource.produce_jll_enumeration_artifacts(
            receipt_root=root,
            page_receipt_paths=pages,
            detail_receipt_paths=details,
            aggregate_path=root / "broken.json",
        )
    assert {path.name for path in root.iterdir()} == baseline


def test_jll_wrapper_cannot_claim_population_absent_from_native_pages(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-wrapper-invention")
    rows = _receipt_batch(root, source_key="jll", count=16)
    provider_ids = [row["provider_id"] for row in rows] + [
        f"jll-wrapper-{number:03d}" for number in range(501 - len(rows))
    ]
    _rebind_enumeration(root, rows, total=len(provider_ids), provider_ids=provider_ids)

    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)


def test_jll_aggregate_rejects_old_synthetic_single_response(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-old-synthetic")
    rows = _receipt_batch(root, source_key="jll", count=16)
    provider_ids = [row["provider_id"] for row in rows]
    _bind_enumeration_document(
        root,
        rows,
        {
            "observed_at": OBSERVED_AT,
            "total": len(provider_ids),
            "complete": True,
            "truncated": False,
            "provider_ids": provider_ids,
            "body": json.dumps({"data": {"properties": {"count": 16, "items": []}}}),
            "request_url": "https://property.jll.com/enumeration",
            "final_url": "https://property.jll.com/enumeration",
            "http_status": 200,
            "content_type": "application/json",
            "timing_ms": 10,
        },
    )

    with pytest.raises(multisource.MultisourceError, match="receipt is malformed"):
        _prevalidate(root, rows)


@pytest.mark.parametrize("bad_skip", [0, 100])
def test_jll_aggregate_rejects_duplicate_or_gapped_page_sequence(
    evidence_root: Path, bad_skip: int
) -> None:
    root = _private_dir(evidence_root, f"jll-page-sequence-{bad_skip}")
    rows = _receipt_batch(root, source_key="jll", count=51)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    page = _read(Path(aggregate["page_receipts"][1]["path"]))
    page["variables"]["skip"] = bad_skip
    request = json.loads(page["request_body"])
    request["variables"]["skip"] = bad_skip
    page["request_body"] = json.dumps(request, separators=(",", ":"))
    _rewrite_jll_page(root, rows, 1, page)

    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)


def test_jll_aggregate_rejects_missing_page_and_inconsistent_filter_count(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-missing-page")
    rows = _receipt_batch(root, source_key="jll", count=51)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    aggregate["page_receipts"].pop()
    _bind_enumeration_document(root, rows, aggregate)
    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "jll-bad-filter-count")
    rows = _receipt_batch(root, source_key="jll", count=51)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    page = _read(Path(aggregate["page_receipts"][1]["path"]))
    payload = json.loads(page["body"])
    payload["data"]["properties"]["count"] = 52
    page["body"] = json.dumps(payload, separators=(",", ":"))
    _rewrite_jll_page(root, rows, 1, page)
    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)


@pytest.mark.parametrize("mutation", ["variables", "body"])
def test_jll_aggregate_rejects_altered_native_page(
    evidence_root: Path, mutation: str
) -> None:
    root = _private_dir(evidence_root, f"jll-altered-{mutation}")
    rows = _receipt_batch(root, source_key="jll", count=51)
    aggregate = _read(Path(rows[0]["enumeration_receipt_path"]))
    page = _read(Path(aggregate["page_receipts"][1]["path"]))
    if mutation == "variables":
        page["variables"]["take"] = 49
    else:
        payload = json.loads(page["body"])
        payload["data"]["properties"]["items"] = [{"id": rows[0]["provider_id"]}]
        page["body"] = json.dumps(payload, separators=(",", ":"))
    _rewrite_jll_page(root, rows, 1, page)

    with pytest.raises(
        multisource.MultisourceError, match="JLL enumeration completeness"
    ):
        _prevalidate(root, rows)


def test_unverified_source_population_is_screening_only_not_workload_weighted(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "screening-only-population")
    rows = _receipt_batch(root, source_key="cbre", count=16)
    cohort = _prevalidate(root, rows)
    cbre = next(
        source for source in cohort["sources"] if source["source_key"] == "cbre"
    )

    assert cbre["core_state"] == "enumeration_population_unverified"
    assert cbre["core_target_rows"] == 0
    assert cbre["core_selected_rows"] == 0
    assert cbre["fresh_enumeration"] == {
        "total_population": None,
        "population_state": "unverified",
        "receipt_sha256": cbre["fresh_enumeration"]["receipt_sha256"],
        "complete": False,
    }
    assert {row["stratum"]["page_weight_band"] for row in cbre["calibration"]} == {
        "unverified"
    }
    plane = cohort["planes"]["authoritative_inventory"]
    assert plane["workload_weighted_individually_qualified_rows_per_minute"] is None
    assert plane["workload_weighting"]["population_state"] == "unverified"


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
    assert _read(Path(investor_rows[0]["raw_receipt_path"]))["http_status"] == 429
    cohort = _prevalidate(root, jll_rows + investor_rows)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["core_state"] == "challenge_or_throttle_in_family"
    assert jll["core"] == []

    root = _private_dir(evidence_root, "challenge-wrapper-mismatch")
    inconsistent = _receipt_batch(
        root, source_key="jll-investor", classification="challenge_or_throttle"
    )
    inconsistent[0]["classification"] = "eligible_detail"
    with pytest.raises(
        multisource.MultisourceError, match="classification conflicts with raw evidence"
    ):
        _prevalidate(root, inconsistent)


def test_bound_challenge_marker_requires_the_challenge_classification(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "challenge-marker")
    rows = _receipt_batch(root, source_key="jll-investor")
    raw = _read(Path(rows[0]["raw_receipt_path"]))
    raw["body"] = {"rawHtml": "<title>Verify you are human</title>"}
    _rewrite_raw_receipt(root, rows[0], raw)
    _refresh_extractor(root, rows[0])
    with pytest.raises(
        multisource.MultisourceError, match="classification conflicts with raw evidence"
    ):
        _prevalidate(root, rows)

    rows[0]["classification"] = "challenge_or_throttle"
    assert _prevalidate(root, rows)["aggregate"]["state"] == "incomplete_screen"


def test_unsafe_url_and_caller_asserted_strata_are_rejected(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "credential-url")
    rows = _receipt_batch(root, source_key="jll", count=16)
    rows[0]["request_url"] = "https://user:pass@property.jll.com/listing/jll-00"
    with pytest.raises(multisource.MultisourceError, match="provider host contract"):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "caller-stratum")
    rows = _receipt_batch(root, source_key="jll", count=25)
    rows[0]["stratum"] = {
        "transaction_class": "arbitrary",
        "property_type": "arbitrary",
        "page_weight_band": "arbitrary",
    }
    with pytest.raises(multisource.MultisourceError, match="unsupported"):
        _prevalidate(root, rows)


@pytest.mark.parametrize(
    ("source_key", "url"),
    [
        ("svn", "https://svn.com/properties/?propertyId=0-pray-boulevard-sale"),
        (
            "lee-associates",
            "https://www.lee-associates.com/properties/?propertyId=882616-sale&address=9001-Alico-Trade-Center-Rd&officeId=2403",
        ),
    ],
)
def test_buildout_property_id_query_contract_admits_native_listing_urls(
    source_key: str, url: str
) -> None:
    assert multisource._canonical_target_url(url, _source(source_key)) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://svn.com/properties/",
        "https://svn.com/properties/?propertyId=",
        "https://svn.com/properties/?propertyId=one&propertyId=two",
        "https://svn.com/properties/?propertyid=one",
        "https://svn.com/properties/?propertyId=one&next=https%3A%2F%2Fbad.example",
        "https://svn.com/properties/?propertyId=one&officeId=abc",
        "https://svn.com/properties/?propertyId=one%ZZ",
        "https://svn.com/properties/?propertyId=one#fragment",
        "http://svn.com/properties/?propertyId=one",
        "https://user:pass@svn.com/properties/?propertyId=one",
        "https://svn.com:8443/properties/?propertyId=one",
        "https://attacker.example/properties/?propertyId=one",
    ],
)
def test_buildout_property_id_query_contract_rejects_unsafe_or_malformed_targets(
    url: str,
) -> None:
    with pytest.raises(multisource.MultisourceError, match="provider host contract"):
        multisource._canonical_target_url(url, _source("svn"))


def test_jll_replayed_detail_identity_is_rejected_before_cohort_selection(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "jll-replay")
    rows = _receipt_batch(root, source_key="jll", count=16)
    replayed_body = _read(Path(rows[0]["raw_receipt_path"]))["body"]
    raw = _read(Path(rows[1]["raw_receipt_path"]))
    raw["body"] = replayed_body
    _rewrite_raw_receipt(root, rows[1], raw)
    _refresh_extractor(root, rows[1])

    with pytest.raises(multisource.MultisourceError, match="raw JLL property id"):
        _prevalidate(root, rows)

    root = _private_dir(evidence_root, "jll-page-target")
    rows = _receipt_batch(root, source_key="jll", count=16)
    raw = _read(Path(rows[0]["raw_receipt_path"]))
    raw["body"] = _raw_body(
        "jll",
        rows[0]["provider_id"],
        "eligible_detail",
        canonical_url="https://property.jll.com/listings/a-different-property",
        transaction_type="rent",
        property_type="industrial",
    )
    _rewrite_raw_receipt(root, rows[0], raw)
    _refresh_extractor(root, rows[0])

    with pytest.raises(multisource.MultisourceError, match="raw JLL property page URL"):
        _prevalidate(root, rows)


def test_nonpositive_detail_timing_cannot_make_a_source_or_plane_ready(
    evidence_root: Path,
) -> None:
    root = _private_dir(evidence_root, "zero-timing")
    rows = _receipt_batch(root, source_key="jll", count=16)
    for row in rows:
        raw = _read(Path(row["raw_receipt_path"]))
        raw["timing_ms"] = 0
        _rewrite_raw_receipt(root, row, raw)
        row["timing_ms"] = 0
        _refresh_extractor(root, row)

    cohort = _prevalidate(root, rows)
    jll = next(source for source in cohort["sources"] if source["source_key"] == "jll")
    assert jll["core_state"] == "invalid_measurement_timing"
    assert jll["core"] == []
    assert jll["individually_qualified_rows_per_minute"] is None
    assert cohort["planes"]["strict_detail"]["sources_core_ready"] == 0


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
