"""Colliers SalesTracker C10 proof boundary; execution remains disabled."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import C10Error

_SOURCE = Path(__file__).parent.parent / "sources" / "colliers.ts"
_HOSTS = frozenset({"sales.colliers.com", "my.rcm1.com"})


def _digest() -> str:
    return hashlib.sha256(
        b"capacity-c10-colliers-v1\0" + _SOURCE.read_bytes()
    ).hexdigest()


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise C10Error("Colliers C10 canonical URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise C10Error("Colliers C10 canonical URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or not parsed.path
        or parsed.fragment
        or parsed.username
        or parsed.password
        or port
    ):
        raise C10Error("Colliers C10 URL violates its SalesTracker host contract")
    return urlunsplit(
        ("https", parsed.hostname, parsed.path.rstrip("/"), parsed.query, "")
    )


@dataclass(frozen=True)
class ColliersCapacityC10Adapter:
    key: str = "colliers"
    fully_verified: bool = False
    implementation_sha256: str = field(default_factory=_digest)

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        ids, urls = evidence.get("project_ids"), evidence.get("detail_urls")
        if (
            set(evidence)
            != {
                "kind",
                "engine_key",
                "complete",
                "total",
                "project_ids",
                "detail_urls",
                "snapshot_sha256",
            }
            or evidence.get("kind") != "colliers_salestracker_snapshot_v1"
            or not isinstance(evidence.get("engine_key"), str)
            or not evidence["engine_key"]
            or evidence.get("complete") is not True
            or type(evidence.get("total")) is not int
            or evidence["total"] < 1
            or not isinstance(ids, list)
            or not isinstance(urls, list)
            or len(ids) != evidence["total"]
            or len(ids) != len(urls)
            or len(set(ids)) != len(ids)
            or any(not isinstance(item, str) or not item for item in ids)
            or len({_url(value) for value in urls}) != len(urls)
            or not isinstance(evidence.get("snapshot_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["snapshot_sha256"]) is None
        ):
            raise C10Error(
                "Colliers C10 enumeration does not prove complete ProjectId identity"
            )

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        project_id, canonical_url, detail = (
            evidence.get("provider_id"),
            evidence.get("canonical_url"),
            evidence.get("detail"),
        )
        if (
            not isinstance(project_id, str)
            or not project_id
            or _url(canonical_url) != canonical_url.rstrip("/")
            or not isinstance(detail, Mapping)
            or detail.get("ProjectId") != project_id
            or _url(detail.get("detail_url")) != canonical_url.rstrip("/")
            or not isinstance(detail.get("native_documents"), list)
            or not isinstance(detail.get("native_images"), list)
            or not isinstance(detail.get("fidelity"), Mapping)
        ):
            raise C10Error("Colliers C10 member does not bind its SLP detail identity")

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        # SalesTracker does not have a reviewed C10 attrition receipt yet.
        return False

    def no_write_request(
        self, evidence: Mapping[str, Any], *, concurrency: int
    ) -> dict[str, Any]:
        self.verify_member(evidence)
        if concurrency != 1:
            raise C10Error(
                "Colliers C10 remains serial pending provider-specific calibration"
            )
        return {
            "source": self.key,
            "url": _url(evidence["canonical_url"]),
            "request_count": 1,
            "retry_attempts": 0,
            "concurrency": 1,
            "start_interval_ms": 0,
            "cache": "private_replicate_only",
            "writes": {
                "database": 0,
                "canonical_cache": 0,
                "status": 0,
                "scheduler": 0,
                "model_or_ocr": 0,
            },
        }
