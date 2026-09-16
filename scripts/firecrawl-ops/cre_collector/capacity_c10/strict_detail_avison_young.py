"""Avison Young C10 receipt boundary; direct-detail retries stay disabled."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import C10Error

_SOURCE = Path(__file__).parent.parent / "sources" / "avison-young.ts"
_HOSTS = frozenset({"www.avisonyoung.us", "pse-api.sharplaunch.com"})


def _digest() -> str:
    return hashlib.sha256(
        b"capacity-c10-avison-young-v1\0" + _SOURCE.read_bytes()
    ).hexdigest()


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise C10Error("Avison Young C10 canonical URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise C10Error("Avison Young C10 canonical URL is invalid") from exc
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
        raise C10Error(
            "Avison Young C10 URL is outside the reviewed public detail contract"
        )
    return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class AvisonYoungCapacityC10Adapter:
    key: str = "avison-young"
    fully_verified: bool = False
    implementation_sha256: str = field(default_factory=_digest)

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        ids, urls = evidence.get("sharplaunch_ids"), evidence.get("detail_urls")
        if (
            set(evidence)
            != {
                "kind",
                "complete",
                "feed_sha256",
                "sharplaunch_ids",
                "detail_urls",
                "snapshot_sha256",
            }
            or evidence.get("kind") != "avison_young_sharplaunch_snapshot_v1"
            or evidence.get("complete") is not True
            or not isinstance(ids, list)
            or not isinstance(urls, list)
            or not ids
            or len(ids) != len(urls)
            or len(set(ids)) != len(ids)
            or any(not isinstance(item, str) or not item for item in ids)
            or len({_url(value) for value in urls}) != len(urls)
            or any(
                not isinstance(evidence.get(key), str)
                or re.fullmatch(r"[0-9a-f]{64}", evidence[key]) is None
                for key in ("feed_sha256", "snapshot_sha256")
            )
        ):
            raise C10Error(
                "Avison Young C10 enumeration does not prove SharpLaunch identity"
            )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        provider_id, canonical_url, detail = (
            evidence.get("provider_id"),
            evidence.get("canonical_url"),
            evidence.get("detail"),
        )
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or _url(canonical_url) != canonical_url.rstrip("/")
            or not isinstance(detail, Mapping)
            or str(detail.get("sharp_launch_id")) != provider_id
            or _url(detail.get("canonical_url")) != canonical_url.rstrip("/")
            or not isinstance(detail.get("native_documents"), list)
            or not isinstance(detail.get("native_images"), list)
            or not isinstance(detail.get("fidelity"), Mapping)
        ):
            raise C10Error("Avison Young C10 member lacks bound direct-detail fidelity")

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        # Cloudflare/error pages are explicitly not attrition evidence.
        return False

    def no_write_request(
        self, evidence: Mapping[str, Any], *, concurrency: int
    ) -> dict[str, Any]:
        self.verify_member(evidence)
        if concurrency != 1:
            raise C10Error(
                "Avison Young C10 remains serial pending a dedicated calibration"
            )
        return {
            "source": self.key,
            "url": _url(evidence["canonical_url"]),
            "request_count": 1,
            "retry_attempts": 0,
            "concurrency": 1,
            "direct_detail_retry_attempts": 0,
            "cache": "private_replicate_only",
            "writes": {
                "database": 0,
                "canonical_cache": 0,
                "status": 0,
                "scheduler": 0,
                "model_or_ocr": 0,
            },
        }
