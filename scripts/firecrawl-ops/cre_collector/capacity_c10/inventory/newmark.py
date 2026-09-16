"""Offline verifier for Newmark NIM's complete ascending search feed."""

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

_HOSTS = {"api-public.nim.nmrk.com", "nim.nmrk.com", "nmrk.com", "www.nmrk.com"}
_PAGE_SIZE = 100
_RETRY = "nim_six_attempts_exponential_retry_after"


@dataclass(frozen=True)
class NewmarkAdapter:
    key: str = "newmark"
    fully_verified: bool = False
    implementation_sha256: str = sha256("capacity_c10.inventory.newmark.v1")

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {"source_key", "total", "pages", "pacing", "snapshot_sha256"},
            "newmark enumeration evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error("newmark enumeration is assigned to another source")
        total = positive_int(evidence["total"], "newmark total")
        require_pacing(
            evidence["pacing"], concurrency=1, retry=_RETRY, label="newmark pacing"
        )

        def identity(row: Mapping[str, Any]) -> str:
            exact_mapping(row, {"id", "canonical_url"}, "newmark NIM row")
            identifier = text(row["id"], "newmark NIM id")
            https_url(row["canonical_url"], _HOSTS, "newmark canonical URL")
            return identifier

        rows = require_exact_pages(
            evidence["pages"],
            total=total,
            page_size=_PAGE_SIZE,
            row_key="data",
            identity=identity,
            first_page=0,
            label="newmark",
        )
        digest_matches(
            evidence["snapshot_sha256"], list(rows), "newmark snapshot_sha256"
        )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        raw, normalized, provider_id, _ = require_member_envelope(
            evidence, key=self.key, hosts=_HOSTS
        )
        exact_mapping(
            raw, {"id", "title", "description", "images", "documents"}, "newmark raw"
        )
        if text(raw["id"], "newmark raw id") != provider_id:
            raise C10Error("newmark member identity does not bind NIM id")
        require_normalized_fidelity(
            normalized,
            expected_fields={"title": raw["title"], "description": raw["description"]},
            label="newmark",
        )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        return false_not_found(evidence)
