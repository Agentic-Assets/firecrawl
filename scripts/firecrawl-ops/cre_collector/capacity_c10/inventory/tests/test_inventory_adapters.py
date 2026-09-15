"""No-network adversarial tests for Wave 2 inventory verifier candidates."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from capacity_c10.contracts import C10Error, sha256
from capacity_c10.inventory import (
    BullRealtyAdapter,
    CbreAdapter,
    CbreDealflowAdapter,
    CushmanWakefieldAdapter,
    LeeAssociatesAdapter,
    NewmarkAdapter,
    SrsAdapter,
    SvnAdapter,
)
from capacity_c10.inventory.cushman_wakefield import canonical_identity

ADAPTERS = {
    "cbre": CbreAdapter(),
    "cbre-dealflow": CbreDealflowAdapter(),
    "cushman-wakefield": CushmanWakefieldAdapter(),
    "newmark": NewmarkAdapter(),
    "srs": SrsAdapter(),
    "svn": SvnAdapter(),
    "lee-associates": LeeAssociatesAdapter(),
    "bull-realty": BullRealtyAdapter(),
}
FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "member_cases.json").read_text()
)


def member_evidence(source: str) -> dict[str, Any]:
    case = copy.deepcopy(FIXTURES[source])
    if source == "cushman-wakefield":
        case["provider_id"] = canonical_identity(case["canonical_url"])
    return {
        "source_key": source,
        "observed_at": "2026-09-15T12:00:00Z",
        "provider_id": case["provider_id"],
        "canonical_url": case["canonical_url"],
        "raw": case["raw"],
        "raw_sha256": sha256(case["raw"]),
        "normalized": case["normalized"],
        "normalized_sha256": sha256(case["normalized"]),
    }


@pytest.mark.parametrize("source", ADAPTERS)
def test_member_fixture_binds_existing_source_shape(source: str) -> None:
    ADAPTERS[source].verify_member(member_evidence(source))


@pytest.mark.parametrize("source", ADAPTERS)
def test_tampered_member_raw_digest_fails_closed(source: str) -> None:
    evidence = member_evidence(source)
    evidence["raw"] = {"tampered": True}
    with pytest.raises(C10Error, match="raw_sha256"):
        ADAPTERS[source].verify_member(evidence)


@pytest.mark.parametrize("source", ADAPTERS)
def test_unreviewed_404_never_becomes_a_tombstone(source: str) -> None:
    assert ADAPTERS[source].classify_not_found({"http_status": 404}) is False


@pytest.mark.parametrize("source", ADAPTERS)
def test_member_wrong_canonical_host_fails_closed(source: str) -> None:
    evidence = member_evidence(source)
    evidence["canonical_url"] = "https://attacker.example/listing"
    with pytest.raises(C10Error):
        ADAPTERS[source].verify_member(evidence)


@pytest.mark.parametrize("source", ["cbre", "newmark", "srs"])
def test_non_https_asset_fidelity_fails_closed(source: str) -> None:
    evidence = member_evidence(source)
    evidence["normalized"]["assets"]["images"] = ["http://assets.example/image.jpg"]
    evidence["normalized_sha256"] = sha256(evidence["normalized"])
    with pytest.raises(C10Error, match="asset URL"):
        ADAPTERS[source].verify_member(evidence)


def _pages(total: int, page_size: int, key: str, row) -> list[dict[str, Any]]:
    return [
        {
            "page": page,
            "total": total,
            key: [row(index) for index in range(start, min(start + page_size, total))],
        }
        for page, start in enumerate(range(0, total, page_size))
    ]


def test_cbre_enumeration_rejects_truncation_and_duplicate_identity() -> None:
    total = 500
    documents = [
        {
            "primary_key": f"CBRE-{index}",
            "canonical_url": f"https://www.cbre.com/details/CBRE-{index}/example",
        }
        for index in range(total)
    ]
    evidence = {
        "source_key": "cbre",
        "total": total,
        "pages": [{"page": 1, "total": total, "documents": documents}],
        "pacing": {"concurrency": 2, "retry": "firecrawl_three_attempts_linear_2500ms"},
        "snapshot_sha256": sha256(documents),
    }
    CbreAdapter().verify_enumeration(evidence)

    truncated = copy.deepcopy(evidence)
    truncated["pages"][0]["documents"].pop()
    with pytest.raises(C10Error, match="truncated"):
        CbreAdapter().verify_enumeration(truncated)

    duplicate = copy.deepcopy(evidence)
    duplicate["pages"][0]["documents"][-1] = duplicate["pages"][0]["documents"][0]
    duplicate["snapshot_sha256"] = sha256(duplicate["pages"][0]["documents"])
    with pytest.raises(C10Error, match="duplicate"):
        CbreAdapter().verify_enumeration(duplicate)


@pytest.mark.parametrize(
    ("adapter", "source", "page_size", "row_key", "first_page", "row"),
    [
        (
            CushmanWakefieldAdapter(),
            "cushman-wakefield",
            100,
            "rows",
            1,
            lambda index: {
                "api_id": f"CW-{index}",
                "canonical_url": f"https://www.cushmanwakefield.com/properties/{index}",
            },
        ),
        (
            NewmarkAdapter(),
            "newmark",
            100,
            "data",
            0,
            lambda index: {
                "id": f"NMRK-{index}",
                "canonical_url": f"https://nim.nmrk.com/properties/{index}",
            },
        ),
        (
            SrsAdapter(),
            "srs",
            12,
            "properties",
            0,
            lambda index: {
                "srs_listing_id": f"SRS-{index}",
                "canonical_url": f"https://www.srsre.com/properties/{index}",
            },
        ),
    ],
)
def test_native_pagination_receipts_bind_all_rows(
    adapter, source: str, page_size: int, row_key: str, first_page: int, row
) -> None:
    total = page_size + 1
    pages = _pages(total, page_size, row_key, row)
    for index, page in enumerate(pages):
        page["page"] = first_page + index
    retry = {
        "cushman-wakefield": "api_serial_pages_firecrawl_detail_three_attempts",
        "newmark": "nim_six_attempts_exponential_retry_after",
        "srs": "direct_post_three_attempts_linear_2000ms",
    }[source]
    concurrency: int | str = "collector_concurrency" if source == "srs" else 1
    evidence = {
        "source_key": source,
        "total": total,
        "pages": pages,
        "pacing": {"concurrency": concurrency, "retry": retry},
        "snapshot_sha256": sha256([entry for page in pages for entry in page[row_key]]),
    }
    adapter.verify_enumeration(evidence)


def test_buildout_rejects_page_truncation_and_duplicate_ids() -> None:
    rows = [
        {"id": "SVN-1", "show_link": "https://svn.com/properties/?propertyId=one-sale"},
        {
            "id": "SVN-2",
            "show_link": "https://svn.com/properties/?propertyId=two-lease",
        },
        {
            "id": "SVN-3",
            "show_link": "https://svn.com/properties/?propertyId=three-sale",
        },
    ]
    evidence = {
        "source_key": "svn",
        "total": 3,
        "limit": 2,
        "sort": "created_at asc, id asc",
        "pages": [
            {"page": 0, "total": 3, "limit": 2, "inventory": rows[:2]},
            {"page": 1, "total": 3, "limit": 2, "inventory": rows[2:]},
        ],
        "pacing": {"concurrency": 1, "retry": "one_recovery_pass_after_15000ms"},
        "snapshot_sha256": sha256(rows),
    }
    SvnAdapter().verify_enumeration(evidence)

    truncated = copy.deepcopy(evidence)
    truncated["pages"].pop()
    with pytest.raises(C10Error, match="incomplete"):
        SvnAdapter().verify_enumeration(truncated)

    duplicate = copy.deepcopy(evidence)
    duplicate["pages"][1]["inventory"][0] = duplicate["pages"][0]["inventory"][0]
    duplicate["snapshot_sha256"] = sha256(
        [entry for page in duplicate["pages"] for entry in page["inventory"]]
    )
    with pytest.raises(C10Error, match="duplicate"):
        SvnAdapter().verify_enumeration(duplicate)


def test_dealflow_rejects_partial_card_receipt() -> None:
    cards = [
        {
            "id": f"PV-{index}",
            "url": f"https://www.cbredealflow.com/landing.aspx?pv=PV-{index}",
            "url_kind": "detail",
            "listing_pv": f"PV-{index}",
        }
        for index in range(200)
    ]
    evidence = {
        "source_key": "cbre-dealflow",
        "total": 200,
        "pages": [{"start": 1, "total": 200, "cards": cards}],
        "pacing": {
            "concurrency": 1,
            "retry": "inventory_two_attempts_retry_after_cap_30000ms_deadline_250000ms",
            "detail_concurrency": "min(collector_concurrency,2)",
        },
        "parse_omissions": 0,
        "duplicate_identities": 0,
        "exhausted": True,
        "snapshot_sha256": sha256(cards),
    }
    CbreDealflowAdapter().verify_enumeration(evidence)
    evidence["parse_omissions"] = 1
    with pytest.raises(C10Error, match="not complete"):
        CbreDealflowAdapter().verify_enumeration(evidence)


@pytest.mark.parametrize("adapter", ADAPTERS.values())
def test_candidates_can_never_be_admitted_before_receipt_producer_review(
    adapter,
) -> None:
    assert adapter.fully_verified is False
