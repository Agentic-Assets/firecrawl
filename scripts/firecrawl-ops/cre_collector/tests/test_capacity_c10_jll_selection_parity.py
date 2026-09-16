"""Golden-vector parity for the JLL admission selection rule
``jll-canonical-url-lexicographic-v1``, implemented twice and required to be
byte-identical: Python ``jll_admission.select_jll_admission_members`` and
TypeScript ``selectJllAdmissionMembers`` (capacity_c10/receipts/strict_detail/
jll_admission.ts). Both sides read the same fixture,
tests/fixtures/c10_jll_selection_vectors.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capacity_c10 import contracts, jll_admission

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "c10_jll_selection_vectors.json"

CATEGORY_PHRASES = {
    "envelope": "envelope is invalid",
    "noncanonical": "noncanonical candidate",
    "duplicate": "duplicate candidates",
    "insufficient": "insufficient canonical candidates",
}


def _load_vectors() -> list[dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text())


VECTORS = _load_vectors()
VECTOR_IDS = [str(vector["name"]) for vector in VECTORS]


@pytest.mark.parametrize("vector", VECTORS, ids=VECTOR_IDS)
def test_python_selection_matches_fixture(vector: dict[str, object]) -> None:
    payload = vector["payload"]
    expect = vector["expect"]

    if "error" in expect:
        category = expect["error"]
        assert category in CATEGORY_PHRASES, f"unknown category {category!r}"
        with pytest.raises(contracts.C10Error, match=CATEGORY_PHRASES[category]):
            jll_admission.select_jll_admission_members(payload)
        return

    result = jll_admission.select_jll_admission_members(payload)
    assert result["rule"] == expect["rule"]
    assert result["candidate_count"] == expect["candidate_count"]
    assert result["members"] == expect["members"]
    assert result["digest"] == expect["digest"]


def test_fixture_has_every_category() -> None:
    categories = {vector["expect"].get("error", "success") for vector in VECTORS}
    assert categories == {
        "success",
        "envelope",
        "noncanonical",
        "duplicate",
        "insufficient",
    }


def test_fixture_vector_names_are_unique() -> None:
    names = [vector["name"] for vector in VECTORS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# _parse_enumeration_body byte-decoding behavior
# ---------------------------------------------------------------------------


def test_parse_enumeration_body_rejects_invalid_utf8() -> None:
    raw = b'{"errors": [], "data": {"properties": {"count": 0, "items": []}}}'
    # Corrupt a byte to an invalid UTF-8 continuation byte inside the JSON body.
    corrupted = raw[:5] + b"\xff" + raw[6:]
    with pytest.raises(contracts.C10Error, match="envelope is invalid"):
        jll_admission._parse_enumeration_body(corrupted)


def test_parse_enumeration_body_rejects_nan_literal() -> None:
    raw = b'{"errors": [], "data": {"properties": {"count": NaN, "items": []}}}'
    with pytest.raises(contracts.C10Error, match="envelope is invalid"):
        jll_admission._parse_enumeration_body(raw)


def test_parse_enumeration_body_accepts_utf8_bom_prefixed_body() -> None:
    raw = (
        b"\xef\xbb\xbf"
        + b'{"errors": [], "data": {"properties": {"count": 0, "items": []}}}'
    )
    payload = jll_admission._parse_enumeration_body(raw)
    assert payload == {"errors": [], "data": {"properties": {"count": 0, "items": []}}}


# ---------------------------------------------------------------------------
# Selection digest recomputation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    [vector for vector in VECTORS if "error" not in vector["expect"]],
    ids=[vector["name"] for vector in VECTORS if "error" not in vector["expect"]],
)
def test_selection_digest_matches_recomputation(vector: dict[str, object]) -> None:
    expect = vector["expect"]
    assert expect["digest"] == jll_admission._selection_digest(expect["members"])
