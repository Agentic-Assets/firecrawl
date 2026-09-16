"""Offline verifier for CBRE's public paginated listings API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..contracts import C10Error, sha256
from ._evidence import (
    digest_matches,
    exact_mapping,
    false_not_found,
    positive_int,
    require_exact_pages,
    require_member_envelope,
    require_normalized_fidelity,
    require_pacing,
    text,
)

_HOSTS = {"cbre.com", "www.cbre.com"}
_PAGE_SIZE = 500
_RETRY = "firecrawl_three_attempts_linear_2500ms"


@dataclass(frozen=True)
class CbreAdapter:
    """Validates the existing two-pass CBRE membership-converged snapshot."""

    key: str = "cbre"
    fully_verified: bool = False
    implementation_sha256: str = sha256("capacity_c10.inventory.cbre.v1")

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {"source_key", "total", "pages", "pacing", "snapshot_sha256"},
            "cbre enumeration evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error("cbre enumeration is assigned to another source")
        total = positive_int(evidence["total"], "cbre total")
        require_pacing(
            evidence["pacing"], concurrency=2, retry=_RETRY, label="cbre pacing"
        )

        def identity(row: Mapping[str, Any]) -> str:
            exact_mapping(row, {"primary_key", "canonical_url"}, "cbre inventory row")
            primary_key = text(row["primary_key"], "cbre Common.PrimaryKey")
            canonical = text(row["canonical_url"], "cbre canonical_url")
            parsed = urlsplit(canonical)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in _HOSTS
                or f"/details/{primary_key}/" not in parsed.path
            ):
                raise C10Error("cbre canonical URL does not bind Common.PrimaryKey")
            return primary_key

        rows = require_exact_pages(
            evidence["pages"],
            total=total,
            page_size=_PAGE_SIZE,
            row_key="documents",
            identity=identity,
            label="cbre",
        )
        digest_matches(evidence["snapshot_sha256"], list(rows), "cbre snapshot_sha256")

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        raw, normalized, provider_id, canonical = require_member_envelope(
            evidence, key=self.key, hosts=_HOSTS
        )
        exact_mapping(
            raw,
            {
                "Common.PrimaryKey",
                "Common.Name",
                "Common.LongDescription",
                "images",
                "documents",
            },
            "cbre raw",
        )
        if (
            text(raw["Common.PrimaryKey"], "cbre raw primary key") != provider_id
            or f"/details/{provider_id}/" not in urlsplit(canonical).path
        ):
            raise C10Error("cbre member identity does not bind raw Common.PrimaryKey")
        require_normalized_fidelity(
            normalized,
            expected_fields={
                "title": raw["Common.Name"],
                "description": raw["Common.LongDescription"],
            },
            label="cbre",
        )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        return false_not_found(evidence)
