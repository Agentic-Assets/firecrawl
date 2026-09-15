"""JLL-specific C10 receipt checks; deliberately not registered for execution."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cre_capacity_multisource_v1 as multisource

from .contracts import C10Error

_SOURCE = Path(__file__).parent.parent / "sources" / "jll.ts"
_POLICY = {"key": "jll", "hosts": ("property.jll.com",)}


def _digest() -> str:
    return hashlib.sha256(b"capacity-c10-jll-v1\0" + _SOURCE.read_bytes()).hexdigest()


@dataclass(frozen=True)
class JllCapacityC10Adapter:
    """Bind JLL's existing GraphQL and ``__NEXT_DATA__`` proof, without fetching."""

    key: str = "jll"
    fully_verified: bool = False
    implementation_sha256: str = field(default_factory=_digest)

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        required = {
            "kind",
            "complete",
            "truncated",
            "total",
            "provider_ids",
            "page_receipts",
            "resolution_receipts",
        }
        if (
            set(evidence) != required
            or evidence.get("kind") != "jll_graphql_enumeration_aggregate_v1"
            or evidence.get("complete") is not True
            or evidence.get("truncated") is not False
            or type(evidence.get("total")) is not int
            or evidence["total"] < 1
            or not isinstance(evidence.get("provider_ids"), list)
            or len(evidence["provider_ids"]) != evidence["total"]
            or len(set(evidence["provider_ids"])) != len(evidence["provider_ids"])
            or any(
                not isinstance(value, str) or not value
                for value in evidence["provider_ids"]
            )
            or not isinstance(evidence.get("page_receipts"), list)
            or not evidence["page_receipts"]
            or not isinstance(evidence.get("resolution_receipts"), list)
            or not evidence["resolution_receipts"]
        ):
            raise C10Error(
                "JLL C10 enumeration receipt is not a complete native aggregate"
            )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        provider_id = evidence.get("provider_id")
        canonical_url = evidence.get("canonical_url")
        raw = evidence.get("raw")
        normalized = evidence.get("normalized")
        locators = evidence.get("locators")
        assets = evidence.get("assets")
        if (
            not isinstance(provider_id, str)
            or re.fullmatch(r"[0-9]+", provider_id) is None
        ):
            raise C10Error("JLL C10 member provider identity is invalid")
        try:
            normalized_url = multisource._canonical_jll_listing_url(
                canonical_url, _POLICY
            )
            raw_id, raw_url = multisource._jll_raw_detail_identity(
                raw,
                provider_id=provider_id,
                canonical_url=normalized_url,
                source=_POLICY,
            )
        except (AttributeError, TypeError, multisource.MultisourceError) as exc:
            raise C10Error(
                "JLL C10 member does not bind canonical native identity"
            ) from exc
        if (
            raw_id != provider_id
            or raw_url != normalized_url
            or not all(
                isinstance(value, Mapping) for value in (normalized, locators, assets)
            )
            or not multisource._verified_jll_locator_fidelity(
                normalized, locators, assets, raw
            )
        ):
            raise C10Error("JLL C10 member lacks hardened semantic or asset fidelity")

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        raw, receipt = evidence.get("raw"), evidence.get("receipt")
        try:
            multisource._canonical_jll_listing_url(
                evidence.get("canonical_url"), _POLICY
            )
        except (AttributeError, TypeError, multisource.MultisourceError):
            return False
        return (
            isinstance(raw, Mapping)
            and isinstance(receipt, Mapping)
            and (multisource._valid_jll_not_found(raw, receipt))
        )

    def no_write_request(
        self, evidence: Mapping[str, Any], *, concurrency: int
    ) -> dict[str, Any]:
        self.verify_member(evidence)
        if concurrency not in {4, 10}:
            raise C10Error("JLL C10 concurrency must be the governed P0 or P1 value")
        return {
            "source": self.key,
            "url": multisource._canonical_jll_listing_url(
                evidence["canonical_url"], _POLICY
            ),
            "request_count": 1,
            "retry_attempts": 0,
            "concurrency": concurrency,
            "cache": "private_replicate_only",
            "writes": {
                "database": 0,
                "canonical_cache": 0,
                "status": 0,
                "scheduler": 0,
                "model_or_ocr": 0,
            },
        }
