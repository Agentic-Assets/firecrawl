"""Offline adversarial contracts for Wave 2 strict-detail adapter batch A."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import cre_capacity_multisource_v1 as multisource
import pytest
from capacity_c10.contracts import C10Error
from capacity_c10.strict_detail_avison_young import AvisonYoungCapacityC10Adapter
from capacity_c10.strict_detail_colliers import ColliersCapacityC10Adapter
from capacity_c10.strict_detail_colliers_main import ColliersMainCapacityC10Adapter
from capacity_c10.strict_detail_jll import JllCapacityC10Adapter
from capacity_c10.strict_detail_jll_investor import JllInvestorCapacityC10Adapter
from capacity_c10.strict_detail_marcus_millichap import (
    MarcusMillichapCapacityC10Adapter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "capacity_c10_strict_detail_batch_a.json"


@pytest.fixture(scope="module")
def evidence() -> Mapping[str, object]:
    return json.loads(FIXTURE.read_text())


def _jll_member() -> dict[str, object]:
    urls = {
        "images": ["https://assets.example.test/image.jpg"],
        "brochures": ["https://assets.example.test/brochure.pdf"],
        "floorPlans": ["https://assets.example.test/floor.pdf"],
        "videos": ["https://assets.example.test/video"],
        "virtualTours": ["https://assets.example.test/tour"],
        "view360URLs": ["https://assets.example.test/360"],
    }
    property_value = {
        "id": "100",
        "pageUrl": "/listings/demo-property",
        "address": "1 Main Street",
        "title": "Demo Property",
        "tenureTypes": ["sale"],
        "propertyTypes": ["office"],
        "images": urls["images"],
        "brochures": urls["brochures"],
        "floorPlans": {"files": urls["floorPlans"]},
        "videos": urls["videos"],
        "virtualTours": urls["virtualTours"],
        "view360URLs": urls["view360URLs"],
    }
    raw = {
        "body": {
            "rawHtml": '<script id="__NEXT_DATA__">'
            + json.dumps({"props": {"pageProps": {"property": property_value}}})
            + "</script>"
        }
    }
    fields = {
        "address": "1 Main Street",
        "name": "Demo Property",
        "transaction_type": "sale",
        "property_type": "office",
    }
    locators = {"fields": {}}
    paths = {
        "address": ("property.address", "1 Main Street"),
        "name": ("property.title", "Demo Property"),
        "transaction_type": ("property.tenureTypes[0]", "sale"),
        "property_type": ("property.propertyTypes[0]", "office"),
    }
    for field, (path, value) in paths.items():
        locators["fields"][field] = {
            "presence": "present",
            "source_path": path,
            "value_sha256": multisource._sha256(multisource._canonical(value)),
        }
    channels = {}
    for channel, path in multisource._JLL_ASSET_CHANNELS.items():
        channels[channel] = {
            "source_path": path,
            "presence": "present",
            "raw_valid_url_set_sha256": multisource._url_set_hash(urls[channel]),
            "accepted_public_url_set_sha256": multisource._url_set_hash(urls[channel]),
            "normalized_mapped_url_set_sha256": multisource._url_set_hash(
                urls[channel]
            ),
            "rejected_invalid_count": 0,
        }
    return {
        "provider_id": "100",
        "canonical_url": "https://property.jll.com/listings/demo-property",
        "raw": raw,
        "normalized": {"fields": fields, "assets": urls},
        "locators": locators,
        "assets": {"channels": channels},
    }


def test_jll_reuses_hardened_native_identity_assets_and_only_proven_404() -> None:
    adapter = JllCapacityC10Adapter()
    aggregate = {
        "kind": "jll_graphql_enumeration_aggregate_v1",
        "complete": True,
        "truncated": False,
        "total": 1,
        "provider_ids": ["100"],
        "page_receipts": [{"path": "private", "sha256": "a" * 64}],
        "resolution_receipts": [{"path": "private", "sha256": "b" * 64}],
    }
    adapter.verify_enumeration(aggregate)
    member = _jll_member()
    adapter.verify_member(member)
    assert adapter.no_write_request(member, concurrency=10)["retry_attempts"] == 0
    not_found_raw = {
        "body": {
            "rawHtml": '<script id="__NEXT_DATA__">'
            + json.dumps(
                {
                    "props": {
                        "pageProps": {
                            "property": None,
                            "notFound": True,
                            "error": {"statusCode": 404},
                        }
                    }
                }
            )
            + "</script>"
        }
    }
    assert adapter.classify_not_found(
        {
            "canonical_url": "https://property.jll.com/listings/missing-property",
            "raw": not_found_raw,
            "receipt": {
                "http_status": 404,
                "not_found_classifier": "jll_next_data_404_no_property",
            },
        }
    )
    assert not adapter.classify_not_found(
        {
            "canonical_url": "https://evil.example.test/not-a-listing",
            "raw": not_found_raw,
            "receipt": {
                "http_status": 404,
                "not_found_classifier": "jll_next_data_404_no_property",
            },
        }
    )
    member["normalized"]["assets"]["images"] = []  # type: ignore[index]
    with pytest.raises(C10Error, match="fidelity"):
        adapter.verify_member(member)


@pytest.mark.parametrize(
    ("fixture_key", "adapter", "expected_concurrency"),
    [
        ("jll_investor", JllInvestorCapacityC10Adapter(), 1),
        ("colliers", ColliersCapacityC10Adapter(), 1),
        ("colliers_main", ColliersMainCapacityC10Adapter(), 1),
        ("marcus", MarcusMillichapCapacityC10Adapter(), 1),
        ("avison", AvisonYoungCapacityC10Adapter(), 1),
    ],
)
def test_batch_a_native_receipts_are_source_bound_serial_and_no_write(
    evidence: Mapping[str, object],
    fixture_key: str,
    adapter: object,
    expected_concurrency: int,
) -> None:
    fixture = evidence[fixture_key]
    assert isinstance(fixture, Mapping)
    adapter.verify_enumeration(fixture["enumeration"])
    adapter.verify_member(fixture["member"])
    descriptor = adapter.no_write_request(
        fixture["member"], concurrency=expected_concurrency
    )
    assert descriptor["request_count"] == 1
    assert descriptor["retry_attempts"] == 0
    assert descriptor["writes"] == {
        "database": 0,
        "canonical_cache": 0,
        "status": 0,
        "scheduler": 0,
        "model_or_ocr": 0,
    }
    assert adapter.fully_verified is False


@pytest.mark.parametrize(
    ("fixture_key", "adapter"),
    [
        ("jll_investor", JllInvestorCapacityC10Adapter()),
        ("colliers", ColliersCapacityC10Adapter()),
        ("colliers_main", ColliersMainCapacityC10Adapter()),
        ("marcus", MarcusMillichapCapacityC10Adapter()),
        ("avison", AvisonYoungCapacityC10Adapter()),
    ],
)
def test_batch_a_rejects_identity_url_and_unproven_attrition(
    evidence: Mapping[str, object], fixture_key: str, adapter: object
) -> None:
    fixture = evidence[fixture_key]
    assert isinstance(fixture, Mapping)
    bad_member = dict(fixture["member"])
    bad_member["canonical_url"] = "https://evil.example.test/not-a-listing"
    with pytest.raises(C10Error):
        adapter.verify_member(bad_member)
    assert (
        adapter.classify_not_found(
            {
                "http_status": 404,
                "detail_state": "not_found",
                "canonical_url": "https://evil.example.test/not-a-listing",
            }
        )
        is False
    )


def test_colliers_main_only_admits_transport_proven_tombstone() -> None:
    adapter = ColliersMainCapacityC10Adapter()
    assert adapter.classify_not_found(
        {
            "http_status": 404,
            "detail_state": "not_found",
            "canonical_url": "https://www.colliers.com/en-us/properties/one",
        }
    )


@pytest.mark.parametrize(
    "adapter",
    [
        JllInvestorCapacityC10Adapter(),
        ColliersCapacityC10Adapter(),
        ColliersMainCapacityC10Adapter(),
        MarcusMillichapCapacityC10Adapter(),
        AvisonYoungCapacityC10Adapter(),
    ],
)
def test_batch_a_malformed_port_is_a_domain_rejection_not_a_raw_url_error(
    adapter: object,
) -> None:
    with pytest.raises(C10Error):
        adapter.verify_member(
            {
                "provider_id": "100",
                "canonical_url": "https://example.test:bad/listing",
                "detail": {},
            }
        )
    assert not adapter.classify_not_found(
        {
            "http_status": 200,
            "detail_state": "not_found",
            "canonical_url": "https://www.colliers.com/en-us/properties/one",
        }
    )


def test_batch_a_modules_do_not_provide_network_or_mutating_executor() -> None:
    adapter_dir = Path(__file__).parent.parent / "capacity_c10"
    modules = sorted(adapter_dir.glob("strict_detail_*.py"))
    assert len(modules) == 6
    forbidden = (
        "requests.",
        "urllib.request",
        "subprocess",
        "write_text",
        "write_bytes",
        "mkdir(",
        "unlink(",
    )
    for module in modules:
        source = module.read_text()
        assert not any(token in source for token in forbidden), module.name
