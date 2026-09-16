"""Golden-vector parity for the GraphQL `errors` contract shared across the
Python host and its two membership-verification surfaces.

Contract (implemented uncommitted, see AGENTS.md task contract for this
branch): a GraphQL envelope's `errors` member means "no errors" iff it is
ABSENT or an EMPTY ARRAY. Any other value (non-empty array, null, object,
string, number, boolean) is a failure.

This module drives that contract at three points:

1. ``capacity_c10.host_orchestration._graphql_errors_absent`` directly.
2. ``capacity_c10.jll_admission.select_jll_admission_members`` (the JLL
   selection rule), which must accept/reject in lockstep.
3. ``capacity_c10.host_orchestration._C10HostTransport._verify_evidence``,
   both for the admission-lane enumeration card (``admission_enumeration=True``,
   ``expectedMemberRoutes`` is ``None``) and for a strict P0/P1 enumeration
   card carrying its sixteen sealed ``expectedMemberRoutes``.

The vectors live in ``tests/fixtures/c10_graphql_errors_vectors.json`` and are
shared with the TypeScript sidecar (``c10_browser_listener.test.ts``) and the
TypeScript JLL selection rule
(``tests/ts/capacity_c10/jll_admission_selection_parity.test.ts``).
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest
from capacity_c10_test_support import sealed_jll_plan
from test_capacity_c10_host_orchestration import _signed_evidence

from capacity_c10 import contracts, jll_admission
from capacity_c10.host_orchestration import _C10HostTransport, _graphql_errors_absent
from capacity_c10.host_registry import C10SealedCardRegistry
from capacity_c10.host_session import C10EphemeralKeys, _OpenSsl

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "c10_graphql_errors_vectors.json"

# Sixteen canonical JLL enumeration candidates. Routes intentionally match the
# sealed cohort's ``member-1``..``member-16`` slugs (see
# ``capacity_c10_test_support.sealed_jll_plan``) so the same base envelope can
# also satisfy a strict P0/P1 enumeration card's sealed membership check.
_CANONICAL_ITEMS: list[dict[str, str]] = [
    {"id": f"900{index + 1}", "pageUrl": f"/listings/member-{index + 1}"}
    for index in range(16)
]


def _load_vectors() -> list[dict[str, Any]]:
    return json.loads(_FIXTURE_PATH.read_text())


def _envelope(vector: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "data": {
            "properties": {
                "count": len(_CANONICAL_ITEMS),
                "items": copy.deepcopy(_CANONICAL_ITEMS),
            }
        }
    }
    errors = vector["errors"]
    if errors["present"]:
        payload["errors"] = copy.deepcopy(errors["value"])
    return payload


def test_fixture_is_non_empty_and_covers_every_disposition() -> None:
    vectors = _load_vectors()
    assert vectors
    names = [vector["name"] for vector in vectors]
    assert len(names) == len(set(names))
    assert any(vector["accepted"] for vector in vectors)
    assert any(not vector["accepted"] for vector in vectors)


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda vector: vector["name"])
def test_graphql_errors_absent_matches_vector(vector: dict[str, Any]) -> None:
    payload = _envelope(vector)
    assert _graphql_errors_absent(payload) is vector["accepted"]


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda vector: vector["name"])
def test_select_jll_admission_members_matches_vector(vector: dict[str, Any]) -> None:
    payload = _envelope(vector)
    if vector["accepted"]:
        result = jll_admission.select_jll_admission_members(payload)
        assert len(result["members"]) == 16
        assert result["candidate_count"] == 16
    else:
        with pytest.raises(contracts.C10Error, match="enumeration envelope is invalid"):
            jll_admission.select_jll_admission_members(payload)


def _keys() -> tuple[C10EphemeralKeys, str]:
    deadline = time.monotonic() + 30
    _coordinator_private, _coordinator_public = _OpenSsl.pair(deadline)
    sidecar_private, sidecar_public = _OpenSsl.pair(deadline)
    keys = C10EphemeralKeys("unused", "unused", sidecar_private, sidecar_public, "key")
    return keys, sidecar_private


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda vector: vector["name"])
def test_verify_evidence_admission_enumeration_matches_vector(
    vector: dict[str, Any],
) -> None:
    """Admission-lane enumeration card: `expectedMemberRoutes` is None, the
    sidecar recomputes membership from this very body, and only the GraphQL
    `errors` disposition (plus a well-formed items list) gates acceptance.
    """
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    keys, sidecar_private = _keys()
    deadline = time.monotonic() + 30
    card = dict(registry.resolve("jll-enumeration"))
    card["expectedMemberRoutes"] = None

    evidence, issued = _signed_evidence(
        card, _envelope(vector), sidecar_private, deadline
    )

    if vector["accepted"]:
        _C10HostTransport._verify_evidence(
            object.__new__(_C10HostTransport),
            evidence,
            issued,
            keys,
            deadline,
            admission_enumeration=True,
        )
    else:
        with pytest.raises(contracts.C10Error, match="contains no accepted cohort"):
            _C10HostTransport._verify_evidence(
                object.__new__(_C10HostTransport),
                evidence,
                issued,
                keys,
                deadline,
                admission_enumeration=True,
            )


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda vector: vector["name"])
def test_verify_evidence_strict_p0_p1_enumeration_matches_vector(
    vector: dict[str, Any],
) -> None:
    """Strict (non-admission) P0/P1 enumeration card carrying its sixteen
    sealed `expectedMemberRoutes`. The base envelope's sixteen canonical
    items match those sealed routes exactly, so acceptance turns solely on
    the GraphQL `errors` disposition.
    """
    plan, cohort = sealed_jll_plan()
    registry = C10SealedCardRegistry(plan, cohort)
    keys, sidecar_private = _keys()
    deadline = time.monotonic() + 30
    card = registry.resolve("jll-enumeration")
    assert isinstance(card.get("expectedMemberRoutes"), list)
    assert len(card["expectedMemberRoutes"]) == 16

    evidence, issued = _signed_evidence(
        card, _envelope(vector), sidecar_private, deadline
    )

    if vector["accepted"]:
        _C10HostTransport._verify_evidence(
            object.__new__(_C10HostTransport), evidence, issued, keys, deadline
        )
    else:
        with pytest.raises(contracts.C10Error, match="contains no accepted cohort"):
            _C10HostTransport._verify_evidence(
                object.__new__(_C10HostTransport), evidence, issued, keys, deadline
            )
