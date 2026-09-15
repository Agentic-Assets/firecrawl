"""Colliers Main C10 proof boundary with its exclusive paced detail contract."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import C10Error

_SOURCE = Path(__file__).parent.parent / "sources" / "colliers-main.ts"
_HOSTS = frozenset({"colliers.com", "www.colliers.com"})


def _digest() -> str:
    return hashlib.sha256(
        b"capacity-c10-colliers-main-v1\0" + _SOURCE.read_bytes()
    ).hexdigest()


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise C10Error("Colliers Main C10 canonical URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise C10Error("Colliers Main C10 canonical URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or not parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port
    ):
        raise C10Error("Colliers Main C10 URL is outside the sitemap detail contract")
    return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class ColliersMainCapacityC10Adapter:
    key: str = "colliers-main"
    fully_verified: bool = False
    implementation_sha256: str = field(default_factory=_digest)

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        ids, urls = evidence.get("listing_ids"), evidence.get("sitemap_urls")
        if (
            set(evidence)
            != {
                "kind",
                "complete",
                "truncated",
                "lastmod_bound",
                "listing_ids",
                "sitemap_urls",
                "snapshot_sha256",
            }
            or evidence.get("kind") != "colliers_main_sitemap_snapshot_v1"
            or evidence.get("complete") is not True
            or evidence.get("truncated") is not False
            or not isinstance(evidence.get("lastmod_bound"), str)
            or not isinstance(ids, list)
            or not isinstance(urls, list)
            or not ids
            or len(ids) != len(urls)
            or len(set(ids)) != len(ids)
            or any(
                not isinstance(item, str) or re.fullmatch(r"usa[0-9]+", item) is None
                for item in ids
            )
            or len({_url(value) for value in urls}) != len(urls)
            or not isinstance(evidence.get("snapshot_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["snapshot_sha256"]) is None
        ):
            raise C10Error(
                "Colliers Main C10 enumeration is not a complete sitemap proof"
            )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        listing_id, canonical_url, detail = (
            evidence.get("provider_id"),
            evidence.get("canonical_url"),
            evidence.get("detail"),
        )
        if (
            not isinstance(listing_id, str)
            or re.fullmatch(r"usa[0-9]+", listing_id) is None
            or _url(canonical_url) != canonical_url.rstrip("/")
            or not isinstance(detail, Mapping)
            or detail.get("listing_id") != listing_id
            or _url(detail.get("canonical_url")) != canonical_url.rstrip("/")
            or not isinstance(detail.get("json_ld"), Mapping)
            or not isinstance(detail.get("native_documents"), list)
            or not isinstance(detail.get("native_images"), list)
            or not isinstance(detail.get("fidelity"), Mapping)
        ):
            raise C10Error(
                "Colliers Main C10 member lacks canonical JSON-LD detail evidence"
            )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        try:
            canonical = _url(evidence.get("canonical_url"))
        except C10Error:
            return False
        return (
            evidence.get("http_status") in {404, 410}
            and evidence.get("detail_state") == "not_found"
            and bool(canonical)
        )

    def no_write_request(
        self, evidence: Mapping[str, Any], *, concurrency: int
    ) -> dict[str, Any]:
        self.verify_member(evidence)
        if concurrency != 1:
            raise C10Error("Colliers Main C10 is exclusive and serial")
        return {
            "source": self.key,
            "url": _url(evidence["canonical_url"]),
            "request_count": 1,
            "retry_attempts": 0,
            "concurrency": 1,
            "start_interval_ms": 3000,
            "cache": "private_replicate_only",
            "writes": {
                "database": 0,
                "canonical_cache": 0,
                "status": 0,
                "scheduler": 0,
                "model_or_ocr": 0,
            },
        }
