"""Offline verifier for the Salesforce-backed SRS Cloud Run inventory feed."""

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
    positive_int,
    require_exact_pages,
    require_member_envelope,
    require_normalized_fidelity,
    require_pacing,
    text,
)

_HOSTS = {"srsre-next-412955565034.us-central1.run.app", "srsre.com", "www.srsre.com"}
_PAGE_SIZE = 12
_RETRY = "direct_post_three_attempts_linear_2000ms"


@dataclass(frozen=True)
class SrsAdapter:
    key: str = "srs"
    fully_verified: bool = False
    implementation_sha256: str = sha256("capacity_c10.inventory.srs.v1")

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {"source_key", "total", "pages", "pacing", "snapshot_sha256"},
            "srs enumeration evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error("srs enumeration is assigned to another source")
        total = positive_int(evidence["total"], "srs total")
        require_pacing(
            evidence["pacing"],
            concurrency="collector_concurrency",
            retry=_RETRY,
            label="srs pacing",
        )

        def identity(row: Mapping[str, Any]) -> str:
            exact_mapping(row, {"srs_listing_id", "canonical_url"}, "srs inventory row")
            identifier = text(row["srs_listing_id"], "srs apto_data.SRS_Listings_ID__c")
            https_url(row["canonical_url"], _HOSTS, "srs canonical URL")
            return identifier

        rows = require_exact_pages(
            evidence["pages"],
            total=total,
            page_size=_PAGE_SIZE,
            row_key="properties",
            identity=identity,
            first_page=0,
            label="srs",
        )
        digest_matches(evidence["snapshot_sha256"], list(rows), "srs snapshot_sha256")

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        raw, normalized, provider_id, _ = require_member_envelope(
            evidence, key=self.key, hosts=_HOSTS
        )
        exact_mapping(
            raw,
            {"apto_data", "Name", "Description__c", "images", "documents"},
            "srs raw",
        )
        apto = raw["apto_data"]
        if (
            not isinstance(apto, Mapping)
            or text(apto.get("SRS_Listings_ID__c"), "srs raw listing ID") != provider_id
        ):
            raise C10Error("srs member identity does not bind SRS listing ID")
        require_normalized_fidelity(
            normalized,
            expected_fields={
                "title": raw["Name"],
                "description": raw["Description__c"],
            },
            label="srs",
        )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        return false_not_found(evidence)
