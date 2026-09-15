"""Offline candidate verifier for CBRE Deal Flow public ListingEngine cards.

The current collector can preserve public cards that lack a canonical property
page.  They are valid additive inventory evidence but cannot establish a
tombstone; this adapter therefore rejects any attempt to treat an HTTP status
or a detail-unavailable marker as source-native absence evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import C10Error, sha256
from ._evidence import (
    digest_matches,
    exact_mapping,
    false_not_found,
    https_url,
    observed_at,
    positive_int,
    require_asset_urls,
    text,
)

_HOSTS = {"www.cbredealflow.com"}
_PAGE_SIZE = 200
_RETRY = "inventory_two_attempts_retry_after_cap_30000ms_deadline_250000ms"


@dataclass(frozen=True)
class CbreDealflowAdapter:
    """Validate a complete sealed ListingEngine card population when supplied.

    No producer currently emits this receipt, and the full collector still has
    an intentional provisional-card path.  ``fully_verified`` stays false
    until both conditions are reviewed and the Wave 1 registry integration is
    explicitly approved.
    """

    key: str = "cbre-dealflow"
    fully_verified: bool = False
    implementation_sha256: str = sha256("capacity_c10.inventory.cbre-dealflow.v1")

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {
                "source_key",
                "total",
                "pages",
                "pacing",
                "parse_omissions",
                "duplicate_identities",
                "exhausted",
                "snapshot_sha256",
            },
            "cbre-dealflow enumeration evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error("cbre-dealflow enumeration is assigned to another source")
        total = positive_int(evidence["total"], "cbre-dealflow total")
        require_pacing = evidence["pacing"]
        exact_mapping(
            require_pacing,
            {"concurrency", "retry", "detail_concurrency"},
            "cbre-dealflow pacing",
        )
        if (
            require_pacing["concurrency"] != 1
            or require_pacing["retry"] != _RETRY
            or require_pacing["detail_concurrency"] != "min(collector_concurrency,2)"
        ):
            raise C10Error(
                "cbre-dealflow pacing does not match reviewed source controls"
            )
        if (
            evidence["parse_omissions"] != 0
            or evidence["duplicate_identities"] != 0
            or evidence["exhausted"] is not True
        ):
            raise C10Error("cbre-dealflow card enumeration is not complete")
        pages = evidence["pages"]
        expected_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        if not isinstance(pages, list) or len(pages) != expected_pages:
            raise C10Error("cbre-dealflow pages are incomplete")
        rows: list[Mapping[str, Any]] = []
        for index, page in enumerate(pages):
            exact_mapping(page, {"start", "total", "cards"}, "cbre-dealflow page")
            if page["start"] != 1 + index * _PAGE_SIZE or page["total"] != total:
                raise C10Error("cbre-dealflow page metadata drifted")
            cards = page["cards"]
            expected_rows = (
                _PAGE_SIZE if index < expected_pages - 1 else total - _PAGE_SIZE * index
            )
            if not isinstance(cards, list) or len(cards) != expected_rows:
                raise C10Error("cbre-dealflow page is truncated")
            for card in cards:
                if not isinstance(card, Mapping):
                    raise C10Error("cbre-dealflow card is invalid")
                exact_mapping(
                    card, {"id", "url", "url_kind", "listing_pv"}, "cbre-dealflow card"
                )
                identity = text(card["id"], "cbre-dealflow card id")
                if card["url_kind"] == "detail":
                    url = https_url(card["url"], _HOSTS, "cbre-dealflow detail URL")
                    if (
                        text(card["listing_pv"], "cbre-dealflow listing pv") != identity
                        or "pv=" not in url
                    ):
                        raise C10Error("cbre-dealflow detail card identity drifted")
                elif card["url_kind"] not in {"brochure", "agreement", "unlinked"}:
                    raise C10Error("cbre-dealflow card has an unknown URL kind")
                rows.append(card)
        identities = [text(row["id"], "cbre-dealflow card id") for row in rows]
        if len(identities) != total or len(identities) != len(set(identities)):
            raise C10Error("cbre-dealflow identities are incomplete or duplicate")
        digest_matches(
            evidence["snapshot_sha256"], rows, "cbre-dealflow snapshot_sha256"
        )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {
                "source_key",
                "observed_at",
                "provider_id",
                "canonical_url",
                "raw",
                "raw_sha256",
                "normalized",
                "normalized_sha256",
            },
            "cbre-dealflow member evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error(
                "cbre-dealflow member evidence is assigned to another source"
            )
        observed_at(evidence["observed_at"])
        provider_id = text(evidence["provider_id"], "cbre-dealflow provider_id")
        canonical = https_url(
            evidence["canonical_url"], _HOSTS, "cbre-dealflow canonical_url"
        )
        raw = evidence["raw"]
        normalized = evidence["normalized"]
        if not isinstance(raw, Mapping) or not isinstance(normalized, Mapping):
            raise C10Error("cbre-dealflow member payload is invalid")
        digest_matches(evidence["raw_sha256"], raw, "cbre-dealflow raw_sha256")
        digest_matches(
            evidence["normalized_sha256"], normalized, "cbre-dealflow normalized_sha256"
        )
        exact_mapping(
            raw,
            {
                "id",
                "url",
                "url_kind",
                "listing_pv",
                "name",
                "description",
                "photos",
                "brochures",
            },
            "cbre-dealflow raw",
        )
        if raw["url_kind"] != "detail" or raw["url"] != canonical:
            raise C10Error(
                "cbre-dealflow canonical URL must be a public detail landing page"
            )
        if (
            text(raw["id"], "cbre-dealflow raw id") != provider_id
            or text(raw["listing_pv"], "cbre-dealflow raw pv") != provider_id
        ):
            raise C10Error(
                "cbre-dealflow member does not bind ListingEngine project identity"
            )
        exact_mapping(normalized, {"fields", "assets"}, "cbre-dealflow normalized")
        if normalized["fields"] != {
            "title": raw["name"],
            "description": raw["description"],
        }:
            raise C10Error(
                "cbre-dealflow normalized fields do not bind its public card"
            )
        exact_mapping(
            normalized["assets"], {"images", "documents"}, "cbre-dealflow assets"
        )
        require_asset_urls(normalized["assets"]["images"], "cbre-dealflow images")
        require_asset_urls(normalized["assets"]["documents"], "cbre-dealflow documents")

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        return false_not_found(evidence)
