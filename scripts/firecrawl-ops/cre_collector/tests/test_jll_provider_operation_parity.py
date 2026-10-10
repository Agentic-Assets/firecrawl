"""Hermetic parity between JLL's published operation and sealed Python lanes."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import cre_capacity_multisource_v1 as multisource
import pytest
from capacity_c10 import contracts, jll_admission
from capacity_c10.admission_controller import _JllAdmissionController
from capacity_c10.host_registry import C10SealedCardRegistry

COLLECTOR_ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def typescript_enumeration_card() -> dict[str, Any]:
    # Exercise the real source-owned producer and transport canonicalization.
    # Imports are side-effect-free, with no browser, provider, or database calls.
    result = subprocess.run(
        [
            "node",
            "--import",
            "tsx",
            "--input-type=module",
            "--eval",
            """
import { jllEnumerationCard } from './capacity_c10/receipts/strict_detail/jll.ts';
import { allowlistedCards } from './capacity_c10/receipts/transport.ts';
const card = jllEnumerationCard({ transaction: 'sale', propertyType: 'office', page: 1 });
process.stdout.write(JSON.stringify([...allowlistedCards('jll', [card]).values()][0]));
""",
        ],
        cwd=COLLECTOR_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def test_jll_sealed_lanes_match_the_published_operation(
    typescript_enumeration_card: dict[str, Any],
) -> None:
    provider = json.loads(
        (
            COLLECTOR_ROOT / "tests/fixtures/jll-search-results-operation.json"
        ).read_text()
    )
    request = json.loads(typescript_enumeration_card["body"])
    assert request["operationName"] == provider["operationName"]
    assert (
        re.sub(r"\s+", " ", request["query"]).strip()
        == re.sub(r"\s+", " ", provider["query"]).strip()
    )
    registry_card = C10SealedCardRegistry._enumeration_card([])
    # Exact body bytes, not just equivalent GraphQL, bind both controller lanes.
    assert registry_card["body"] == typescript_enumeration_card["body"]
    assert registry_card["bodySha256"] == jll_admission.JLL_ENUMERATION_BODY_SHA256
    assert (
        hashlib.sha256(request["query"].encode()).hexdigest()
        == multisource._JLL_SEARCH_RESULTS_QUERY_SHA256
    )
    assert (
        _JllAdmissionController._validate_card(typescript_enumeration_card, [], 0)
        == typescript_enumeration_card
    )


@pytest.mark.parametrize("mutation", ["reduced-query", "lease", "industrial"])
def test_jll_admission_rejects_changed_query_or_filters_even_with_rehashed_body(
    typescript_enumeration_card: dict[str, Any], mutation: str
) -> None:
    card = dict(typescript_enumeration_card)
    request = json.loads(card["body"])
    if mutation == "reduced-query":
        request["query"] = (
            "query SearchResults($market: String!, $language: String!) "
            "{ properties(market: $market, language: $language) { count } }"
        )
    elif mutation == "lease":
        request["variables"]["tenureTypes"] = ["rent"]
    else:
        request["variables"]["propertyTypes"] = ["industrial"]
    card["body"] = contracts.canonical_bytes(request).decode()
    card["bodySha256"] = hashlib.sha256(card["body"].encode()).hexdigest()
    with pytest.raises(contracts.C10Error, match="enumeration card is invalid"):
        _JllAdmissionController._validate_card(card, [], 0)
