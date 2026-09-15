"""Marcus & Millichap C10 receipt boundary; no network or cache operations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import C10Error

_SOURCE = Path(__file__).parent.parent / "sources" / "marcus-millichap.ts"
_HOSTS = frozenset({"marcusmillichap.com", "www.marcusmillichap.com"})


def _digest() -> str:
    return hashlib.sha256(
        b"capacity-c10-marcus-millichap-v1\0" + _SOURCE.read_bytes()
    ).hexdigest()


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise C10Error("Marcus C10 canonical URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise C10Error("Marcus C10 canonical URL is invalid") from exc
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
        raise C10Error("Marcus C10 URL is outside the public detail host contract")
    return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class MarcusMillichapCapacityC10Adapter:
    key: str = "marcus-millichap"
    fully_verified: bool = False
    implementation_sha256: str = field(default_factory=_digest)

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        ids, urls = evidence.get("deal_ids"), evidence.get("detail_urls")
        if (
            set(evidence)
            != {
                "kind",
                "complete",
                "tile_count",
                "deal_ids",
                "detail_urls",
                "snapshot_sha256",
            }
            or evidence.get("kind") != "marcus_millichap_map_snapshot_v1"
            or evidence.get("complete") is not True
            or type(evidence.get("tile_count")) is not int
            or evidence["tile_count"] < 1
            or not isinstance(ids, list)
            or not isinstance(urls, list)
            or not ids
            or len(ids) != len(urls)
            or len(set(ids)) != len(ids)
            or any(
                not isinstance(item, str) or re.fullmatch(r"[0-9]+", item) is None
                for item in ids
            )
            or len({_url(value) for value in urls}) != len(urls)
            or not isinstance(evidence.get("snapshot_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["snapshot_sha256"]) is None
        ):
            raise C10Error(
                "Marcus C10 enumeration does not prove native DealId coverage"
            )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        deal_id, canonical_url, detail = (
            evidence.get("provider_id"),
            evidence.get("canonical_url"),
            evidence.get("detail"),
        )
        if (
            not isinstance(deal_id, str)
            or re.fullmatch(r"[0-9]+", deal_id) is None
            or _url(canonical_url) != canonical_url.rstrip("/")
            or not isinstance(detail, Mapping)
            or str(detail.get("DealId")) != deal_id
            or _url(detail.get("canonical_url")) != canonical_url.rstrip("/")
            or not isinstance(detail.get("native_documents"), list)
            or not isinstance(detail.get("native_images"), list)
            or not isinstance(detail.get("fidelity"), Mapping)
        ):
            raise C10Error(
                "Marcus C10 member does not bind map identity to detail fidelity"
            )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        # No Marcus-specific current attrition receipt is reviewed for C10.
        return False

    def no_write_request(
        self, evidence: Mapping[str, Any], *, concurrency: int
    ) -> dict[str, Any]:
        self.verify_member(evidence)
        if concurrency != 1:
            raise C10Error("Marcus C10 remains serial pending a dedicated calibration")
        return {
            "source": self.key,
            "url": _url(evidence["canonical_url"]),
            "request_count": 1,
            "retry_attempts": 0,
            "concurrency": 1,
            "cache": "private_replicate_only",
            "cache_env": "MARCUS_DETAIL_CACHE_PATH",
            "writes": {
                "database": 0,
                "canonical_cache": 0,
                "status": 0,
                "scheduler": 0,
                "model_or_ocr": 0,
            },
        }
