"""Offline candidate verifiers for the existing Buildout inventory feeds.

This module intentionally consumes only sealed receipts made by the Buildout
collector.  It does not call the Buildout API, read its page cache, or enqueue
detail enrichment.  Each source's listing host and pacing contract remain
explicit because a Buildout plugin key alone is not a brokerage identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..contracts import C10Error, sha256
from ._evidence import (
    digest_matches,
    exact_mapping,
    false_not_found,
    https_url,
    positive_int,
    require_member_envelope,
    require_normalized_fidelity,
    require_pacing,
    text,
)

_SORT = "created_at asc, id asc"
_RETRY = "one_recovery_pass_after_15000ms"


@dataclass(frozen=True)
class _BuildoutAdapter:
    """Validates one brokerage's strict Buildout pagination receipt."""

    key: str
    hosts: frozenset[str]
    page_concurrency: int
    fully_verified: bool = False
    implementation_sha256: str = ""

    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        exact_mapping(
            evidence,
            {
                "source_key",
                "total",
                "limit",
                "sort",
                "pages",
                "pacing",
                "snapshot_sha256",
            },
            f"{self.key} Buildout enumeration evidence",
        )
        if evidence["source_key"] != self.key:
            raise C10Error(f"{self.key} enumeration is assigned to another source")
        total = positive_int(evidence["total"], f"{self.key} Buildout meta.total")
        limit = positive_int(evidence["limit"], f"{self.key} Buildout meta.limit")
        if evidence["sort"] != _SORT:
            raise C10Error(f"{self.key} Buildout receipt lacks stable inventory sort")
        require_pacing(
            evidence["pacing"],
            concurrency=self.page_concurrency,
            retry=_RETRY,
            label=f"{self.key} pacing",
        )
        pages = evidence["pages"]
        expected_pages = (total + limit - 1) // limit
        if not isinstance(pages, list) or len(pages) != expected_pages:
            raise C10Error(f"{self.key} Buildout pages are incomplete")
        rows: list[Mapping[str, Any]] = []
        for index, page in enumerate(pages):
            exact_mapping(
                page,
                {"page", "total", "limit", "inventory"},
                f"{self.key} Buildout page",
            )
            if (
                page["page"] != index
                or page["total"] != total
                or page["limit"] != limit
            ):
                raise C10Error(f"{self.key} Buildout page metadata drifted")
            inventory = page["inventory"]
            expected_rows = (
                limit if index < expected_pages - 1 else total - limit * index
            )
            if not isinstance(inventory, list) or len(inventory) != expected_rows:
                raise C10Error(f"{self.key} Buildout page is truncated")
            for row in inventory:
                if not isinstance(row, Mapping):
                    raise C10Error(f"{self.key} Buildout inventory row is invalid")
                exact_mapping(
                    row, {"id", "show_link"}, f"{self.key} Buildout inventory row"
                )
                text(row["id"], f"{self.key} Buildout id")
                show_link = https_url(
                    row["show_link"], set(self.hosts), f"{self.key} show_link"
                )
                property_id = parse_qs(urlsplit(show_link).query).get("propertyId", [])
                if len(property_id) != 1 or not property_id[0].strip():
                    raise C10Error(f"{self.key} show_link lacks propertyId")
                rows.append(row)
        ids = [text(row["id"], f"{self.key} Buildout id") for row in rows]
        if len(ids) != total or len(ids) != len(set(ids)):
            raise C10Error(
                f"{self.key} Buildout identities are incomplete or duplicate"
            )
        digest_matches(evidence["snapshot_sha256"], rows, f"{self.key} snapshot_sha256")

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        raw, normalized, provider_id, canonical = require_member_envelope(
            evidence, key=self.key, hosts=set(self.hosts)
        )
        exact_mapping(
            raw,
            {"id", "show_link", "display_name", "description", "photo_url", "pdf_url"},
            f"{self.key} raw",
        )
        if (
            text(raw["id"], f"{self.key} raw id") != provider_id
            or raw["show_link"] != canonical
        ):
            raise C10Error(f"{self.key} member does not bind Buildout id and show_link")
        property_ids = parse_qs(urlsplit(canonical).query).get("propertyId", [])
        if len(property_ids) != 1 or not property_ids[0].strip():
            raise C10Error(f"{self.key} canonical URL lacks Buildout propertyId")
        require_normalized_fidelity(
            normalized,
            expected_fields={
                "title": raw["display_name"],
                "description": raw["description"],
            },
            label=self.key,
        )

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        return false_not_found(evidence)


@dataclass(frozen=True)
class SvnAdapter(_BuildoutAdapter):
    key: str = "svn"
    hosts: frozenset[str] = frozenset({"svn.com"})
    page_concurrency: int = 1
    implementation_sha256: str = sha256("capacity_c10.inventory.buildout.svn.v1")


@dataclass(frozen=True)
class LeeAssociatesAdapter(_BuildoutAdapter):
    key: str = "lee-associates"
    hosts: frozenset[str] = frozenset({"www.lee-associates.com"})
    page_concurrency: int = 3
    implementation_sha256: str = sha256(
        "capacity_c10.inventory.buildout.lee-associates.v1"
    )


@dataclass(frozen=True)
class BullRealtyAdapter(_BuildoutAdapter):
    key: str = "bull-realty"
    hosts: frozenset[str] = frozenset({"www.bullrealty.com"})
    page_concurrency: int = 1
    implementation_sha256: str = sha256(
        "capacity_c10.inventory.buildout.bull-realty.v1"
    )
