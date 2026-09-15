"""JLL Investor-specific C10 receipt checks; not an executable adapter yet."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import C10Error

_SOURCE = Path(__file__).parent.parent / "sources" / "jll-investor.ts"


def _digest() -> str:
    return hashlib.sha256(
        b"capacity-c10-jll-investor-v1\0" + _SOURCE.read_bytes()
    ).hexdigest()


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise C10Error("JLL Investor C10 canonical URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise C10Error("JLL Investor C10 canonical URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "invest.jll.com"
        or not parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port
    ):
        raise C10Error(
            "JLL Investor C10 canonical URL is outside the public detail contract"
        )
    return urlunsplit(("https", "invest.jll.com", parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class JllInvestorCapacityC10Adapter:
    key: str = "jll-investor"
    fully_verified: bool = False
    implementation_sha256: str = field(default_factory=_digest)

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        ids, urls = evidence.get("provider_ids"), evidence.get("canonical_urls")
        if (
            set(evidence)
            != {"kind", "complete", "provider_ids", "canonical_urls", "snapshot_sha256"}
            or evidence.get("kind") != "jll_investor_search_snapshot_v1"
            or evidence.get("complete") is not True
            or not isinstance(ids, list)
            or not isinstance(urls, list)
            or not ids
            or len(ids) != len(urls)
            or len(set(ids)) != len(ids)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[A-Za-z0-9]{15,18}", item) is None
                for item in ids
            )
            or len({_url(value) for value in urls}) != len(urls)
            or not isinstance(evidence.get("snapshot_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["snapshot_sha256"]) is None
        ):
            raise C10Error("JLL Investor C10 enumeration receipt is incomplete")

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        provider_id, canonical_url, detail = (
            evidence.get("provider_id"),
            evidence.get("canonical_url"),
            evidence.get("detail"),
        )
        if (
            not isinstance(provider_id, str)
            or re.fullmatch(r"[A-Za-z0-9]{15,18}", provider_id) is None
            or _url(canonical_url) != canonical_url.rstrip("/")
            or not isinstance(detail, Mapping)
            or detail.get("salesforce_id") != provider_id
            or _url(detail.get("canonical_url")) != canonical_url.rstrip("/")
            or not isinstance(detail.get("native_documents"), list)
            or not isinstance(detail.get("native_images"), list)
            or not isinstance(detail.get("fidelity"), Mapping)
        ):
            raise C10Error(
                "JLL Investor C10 member does not bind native detail fidelity"
            )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        page = evidence.get("page_props")
        try:
            _url(evidence.get("canonical_url"))
        except C10Error:
            return False
        return bool(
            isinstance(page, Mapping)
            and evidence.get("public_page_http_status") == 404
            and page.get("notFound") is True
            and isinstance(page.get("error"), Mapping)
            and page["error"].get("statusCode") == 404
            and page.get("listing") is None
        )

    def no_write_request(
        self, evidence: Mapping[str, Any], *, concurrency: int
    ) -> dict[str, Any]:
        self.verify_member(evidence)
        if concurrency != 1:
            raise C10Error(
                "JLL Investor C10 remains serial pending a dedicated calibration"
            )
        return {
            "source": self.key,
            "url": _url(evidence["canonical_url"]),
            "request_count": 1,
            "retry_attempts": 0,
            "concurrency": 1,
            "cache": "private_replicate_only",
            "writes": {
                "database": 0,
                "canonical_cache": 0,
                "status": 0,
                "scheduler": 0,
                "model_or_ocr": 0,
            },
        }
