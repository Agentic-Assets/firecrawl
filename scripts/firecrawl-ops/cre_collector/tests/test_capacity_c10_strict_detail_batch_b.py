from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import pytest

from capacity_c10 import adapters, contracts
from capacity_c10.strict_detail_batch_b import strict_detail_batch_b_adapters


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


NO_WRITE = {
    "database_writes": 0,
    "cache_writes": 0,
    "status_writes": 0,
    "scheduler_writes": 0,
    "model_or_ocr_changes": 0,
}


def _header(adapter: Any, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_key": adapter.key,
        "fetch_capability": {
            "kind": "cre_capacity_c10_no_write_fetch_v1",
            "source_key": adapter.key,
            "maximum_concurrency": adapter.capability.maximum_concurrency,
            "minimum_start_interval_ms": adapter.capability.minimum_start_interval_ms,
            "maximum_retries": adapter.capability.maximum_retries,
            **NO_WRITE,
        },
        "raw_receipt_sha256": _sha(adapter.key),
        "no_write": dict(NO_WRITE),
    }


def _enumeration(key: str, adapter: Any) -> dict[str, Any]:
    if key == "savills":
        return {
            **_header(adapter, "c10_savills_nexturl_total_v1"),
            "reported_total": 1,
            "pages": [
                {
                    "url": "https://search.savills.com/p/1",
                    "next_url": None,
                    "rows": [
                        {
                            "external_property_id": "s1",
                            "canonical_url": "https://search.savills.com/property/s1",
                        }
                    ],
                }
            ],
        }
    if key == "nai-global":
        return {
            **_header(adapter, "c10_nai_global_public_posts_v1"),
            "source_ids_sha256": _sha("source-ids"),
            "batches": [
                {
                    "source_ids": [1],
                    "pages": [
                        {
                            "offset": 0,
                            "rows": [
                                {
                                    "public_post_id": "n1",
                                    "canonical_url": "https://infabode.com/services/listings/n1",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    if key == "transwestern":
        return {
            **_header(adapter, "c10_transwestern_ajax_buckets_v1"),
            "buckets": [
                {
                    "name": "Sale",
                    "reported_count": 1,
                    "rows": [
                        {
                            "page_url": "tower",
                            "canonical_url": "https://transwestern.com/property/tower",
                        }
                    ],
                }
            ],
        }
    if key == "matthews":
        return {
            **_header(adapter, "c10_matthews_sitemap_v1"),
            "property_urls": ["https://www.matthews.com/properties/tower"],
        }
    if key == "foundry-commercial":
        return {
            **_header(adapter, "c10_foundry_property_sitemap_v1"),
            "property_sitemaps": [
                {
                    "url": "https://www.foundrycommercial.com/property-sitemap1.xml",
                    "rows": [
                        {
                            "provider_id": "f1",
                            "canonical_url": "https://www.foundrycommercial.com/properties/tower",
                            "explicit_status": "for sale",
                        }
                    ],
                }
            ],
        }
    if key == "daum-commercial":
        return {
            **_header(adapter, "c10_daum_wordpress_snapshot_v1"),
            "reported_total": 1,
            "pages": [
                {
                    "page": 1,
                    "rows": [
                        {
                            "post_id": "1",
                            "canonical_url": "https://daumcommercial.com/property/tower/",
                        }
                    ],
                }
            ],
        }
    raise AssertionError(key)


def _member(adapter: Any) -> dict[str, Any]:
    host = sorted(adapter.hosts)[0]
    return {
        **_header(adapter, f"c10_{adapter.key}_member_v1"),
        "provider_id": "native-1",
        "canonical_url": f"https://{host}/property/native-1",
        "fidelity": {
            "identity": "native-1",
            "method": adapter.member_method,
            "required_fields": ["title", "transaction"],
            "asset_urls": ["https://assets.example.test/brochure.pdf"],
        },
    }


def test_batch_b_registry_is_explicit_and_cannot_admit() -> None:
    registry = adapters.batch_b_registry()
    expected = {
        "savills",
        "nai-global",
        "transwestern",
        "matthews",
        "foundry-commercial",
        "daum-commercial",
    }
    assert expected <= set(registry)
    assert all(registry[key].fully_verified is False for key in expected)
    with pytest.raises(contracts.C10Error, match="not fully verified"):
        adapters.verified_registry(
            {"sources": [{"key": key} for key in registry]}, registry
        )


@pytest.mark.parametrize("key", sorted(strict_detail_batch_b_adapters()))
def test_batch_b_native_enumeration_and_member_fixtures_are_no_write(key: str) -> None:
    adapter = strict_detail_batch_b_adapters()[key]
    adapter.verify_enumeration(_enumeration(key, adapter))
    adapter.verify_member(_member(adapter))
    assert adapter.fully_verified is False
    assert len(adapter.implementation_sha256) == 64


@pytest.mark.parametrize("key", sorted(strict_detail_batch_b_adapters()))
def test_batch_b_rejects_generic_or_writing_member_evidence(key: str) -> None:
    adapter = strict_detail_batch_b_adapters()[key]
    evidence = _member(adapter)
    evidence["no_write"]["cache_writes"] = 1
    with pytest.raises(contracts.C10Error, match="no-write"):
        adapter.verify_member(evidence)
    evidence = _member(adapter)
    evidence["canonical_url"] = "https://unrelated.example.test/property/1"
    with pytest.raises(contracts.C10Error, match="canonical"):
        adapter.verify_member(evidence)
    evidence = _member(adapter)
    evidence["fidelity"]["method"] = "generic_http"
    with pytest.raises(contracts.C10Error, match="native method"):
        adapter.verify_member(evidence)


def test_savills_rejects_nonterminal_nexturl_and_total_mismatch() -> None:
    adapter = strict_detail_batch_b_adapters()["savills"]
    evidence = _enumeration("savills", adapter)
    evidence["pages"][0]["next_url"] = "https://search.savills.com/p/2"
    with pytest.raises(contracts.C10Error, match="terminal"):
        adapter.verify_enumeration(evidence)


def test_nai_requires_native_short_page_and_contiguous_offsets() -> None:
    adapter = strict_detail_batch_b_adapters()["nai-global"]
    evidence = _enumeration("nai-global", adapter)
    evidence["batches"][0]["pages"][0]["rows"] = [
        {
            "public_post_id": str(index),
            "canonical_url": f"https://infabode.com/services/listings/{index}",
        }
        for index in range(100)
    ]
    evidence["batches"][0]["pages"].append(
        {
            "offset": 200,
            "rows": [
                {
                    "public_post_id": "n2",
                    "canonical_url": "https://infabode.com/services/listings/n2",
                }
            ],
        }
    )
    with pytest.raises(contracts.C10Error, match="non-contiguous"):
        adapter.verify_enumeration(evidence)


def test_nai_accepts_an_empty_terminal_short_page_but_not_an_early_short_page() -> None:
    adapter = strict_detail_batch_b_adapters()["nai-global"]
    evidence = _enumeration("nai-global", adapter)
    evidence["batches"][0]["pages"][0]["rows"] = [
        {
            "public_post_id": str(index),
            "canonical_url": f"https://infabode.com/services/listings/{index}",
        }
        for index in range(100)
    ]
    evidence["batches"][0]["pages"].append({"offset": 100, "rows": []})
    adapter.verify_enumeration(evidence)
    evidence["batches"][0]["pages"][0]["rows"] = []
    with pytest.raises(contracts.C10Error, match="before batch completion"):
        adapter.verify_enumeration(evidence)


def test_transwestern_requires_exact_bucket_count() -> None:
    adapter = strict_detail_batch_b_adapters()["transwestern"]
    evidence = _enumeration("transwestern", adapter)
    evidence["buckets"][0]["reported_count"] = 2
    with pytest.raises(contracts.C10Error, match="count"):
        adapter.verify_enumeration(evidence)


def test_matthews_only_accepts_the_reviewed_provider_not_found_shape() -> None:
    adapter = strict_detail_batch_b_adapters()["matthews"]
    exact = {
        "source_key": "matthews",
        "status": 200,
        "canonical_identity_matches": True,
        "tenure_matches": True,
        "has_property_detail_dom": False,
        "next_redirect_to_listings": True,
        "page_not_found_heading": True,
    }
    assert adapter.classify_not_found(exact) is True
    assert adapter.classify_not_found({**exact, "status": 404}) is False
    assert (
        adapter.classify_not_found({**exact, "has_property_detail_dom": True}) is False
    )


def test_foundry_rejects_unknown_or_terminal_status() -> None:
    adapter = strict_detail_batch_b_adapters()["foundry-commercial"]
    evidence = _enumeration("foundry-commercial", adapter)
    evidence["property_sitemaps"][0]["rows"][0]["explicit_status"] = "sold"
    with pytest.raises(contracts.C10Error, match="active native status"):
        adapter.verify_enumeration(evidence)


def test_daum_rejects_missing_page_or_total_reconciliation() -> None:
    adapter = strict_detail_batch_b_adapters()["daum-commercial"]
    evidence = deepcopy(_enumeration("daum-commercial", adapter))
    evidence["reported_total"] = 2
    with pytest.raises(contracts.C10Error, match="reconcile"):
        adapter.verify_enumeration(evidence)
