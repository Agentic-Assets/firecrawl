"""Offline verifier for Cushman & Wakefield's public inventory API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256 as raw_sha256
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

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

_HOSTS = {"www.cushmanwakefield.com", "onecap.cushmanwakefield.com"}
_PAGE_SIZE = 100
_RETRY = "api_serial_pages_firecrawl_detail_three_attempts"


def canonical_identity(url: str) -> str:
    """Match ingest's URL-keyed Cushman identity, not the mutable API row ID."""
    parsed = urlsplit(https_url(url, _HOSTS, "cushman canonical URL"))
    host = parsed.hostname
    if host is None:  # pragma: no cover - https_url already rejects this
        raise C10Error("cushman canonical URL has no host")
    path = parsed.path.rstrip("/") or "/"
    if host == "onecap.cushmanwakefield.com":
        record_ids = [
            value
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if name == "recordId"
        ]
        if len(record_ids) != 1 or not record_ids[0].strip():
            raise C10Error("cushman OneCap canonical URL requires one recordId")
        query = "recordId=" + quote(record_ids[0].strip(), safe="-_.")
    else:
        query = ""
    identity_url = urlunsplit(("https", host, path, query, ""))
    return "url:v1:" + raw_sha256(identity_url.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class CushmanWakefieldAdapter:
    key: str = "cushman-wakefield"
    fully_verified: bool = False
    implementation_sha256: str = sha256("capacity_c10.inventory.cushman-wakefield.v1")

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {"source_key", "total", "pages", "pacing", "snapshot_sha256"},
            "cushman enumeration evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error("cushman enumeration is assigned to another source")
        total = positive_int(evidence["total"], "cushman total")
        require_pacing(
            evidence["pacing"], concurrency=1, retry=_RETRY, label="cushman pacing"
        )

        def identity(row: Mapping[str, Any]) -> str:
            exact_mapping(row, {"api_id", "canonical_url"}, "cushman inventory row")
            text(row["api_id"], "cushman API row.id")
            canonical = https_url(row["canonical_url"], _HOSTS, "cushman canonical URL")
            return canonical_identity(canonical)

        rows = require_exact_pages(
            evidence["pages"],
            total=total,
            page_size=_PAGE_SIZE,
            row_key="rows",
            identity=identity,
            label="cushman",
        )
        digest_matches(
            evidence["snapshot_sha256"], list(rows), "cushman snapshot_sha256"
        )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        raw, normalized, provider_id, canonical = require_member_envelope(
            evidence, key=self.key, hosts=_HOSTS
        )
        exact_mapping(
            raw, {"id", "title", "description", "images", "documents"}, "cushman raw"
        )
        if provider_id != canonical_identity(canonical):
            raise C10Error("cushman member does not use canonical URL identity")
        require_normalized_fidelity(
            normalized,
            expected_fields={"title": raw["title"], "description": raw["description"]},
            label="cushman",
        )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        return false_not_found(evidence)
