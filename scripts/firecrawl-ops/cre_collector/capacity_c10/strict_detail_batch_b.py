"""Offline native-receipt validators for C10 strict-detail Batch B.

These are deliberately not HTTP clients.  They encode the source-specific
proof that a later, reviewed execution adapter must produce, but stay
non-admitting until that complete live proof exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .contracts import C10Error, require_no_write, require_sha256, sha256


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise C10Error(f"{label} must be an object")
    return value


def _items(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise C10Error(f"{label} must be a non-empty array")
    return [_mapping(item, label) for item in value]


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise C10Error(f"{label} must be an integer at least {minimum}")
    return value


def _url(value: Any, hosts: frozenset[str], label: str) -> str:
    if not isinstance(value, str):
        raise C10Error(f"{label} must be an HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or not parsed.path
        or parsed.fragment
    ):
        raise C10Error(f"{label} is not a canonical source URL")
    return value


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise C10Error(f"{label} must be a bounded non-empty source identity")
    return value


@dataclass(frozen=True)
class ReceiptFetchCapability:
    """A no-write request-card declaration, not an HTTP implementation."""

    source_key: str
    maximum_concurrency: int
    minimum_start_interval_ms: int
    maximum_retries: int

    def verify(self, value: Mapping[str, Any]) -> None:
        expected = {
            "kind": "cre_capacity_c10_no_write_fetch_v1",
            "source_key": self.source_key,
            "maximum_concurrency": self.maximum_concurrency,
            "minimum_start_interval_ms": self.minimum_start_interval_ms,
            "maximum_retries": self.maximum_retries,
            "database_writes": 0,
            "cache_writes": 0,
            "status_writes": 0,
            "scheduler_writes": 0,
            "model_or_ocr_changes": 0,
        }
        if dict(value) != expected:
            raise C10Error(f"{self.source_key} fetch capability is not exact no-write")


@dataclass(frozen=True)
class BatchBAdapter:
    """Base for named, source-native receipt contracts; never executable yet."""

    key: str
    hosts: frozenset[str]
    enumeration_kind: str
    member_method: str
    capability: ReceiptFetchCapability
    fully_verified: bool = False

    @property
    def implementation_sha256(self) -> str:
        return sha256(
            {
                "kind": "cre_capacity_c10_batch_b_adapter_v1",
                "key": self.key,
                "enumeration_kind": self.enumeration_kind,
                "member_method": self.member_method,
                "hosts": sorted(self.hosts),
                "capability": self.capability.__dict__,
                "fully_verified": self.fully_verified,
            }
        )

    def _header(self, evidence: Mapping[str, Any], kind: str) -> None:
        if evidence.get("kind") != kind or evidence.get("source_key") != self.key:
            raise C10Error(f"{self.key} receipt kind or source key is invalid")
        self.capability.verify(
            _mapping(evidence.get("fetch_capability"), "fetch capability")
        )
        require_no_write(evidence)
        require_sha256(evidence.get("raw_receipt_sha256"), "raw receipt")

    def _member(self, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        self._header(evidence, f"c10_{self.key}_member_v1")
        member_id = _id(evidence.get("provider_id"), "provider identity")
        _url(evidence.get("canonical_url"), self.hosts, "member canonical URL")
        fidelity = _mapping(evidence.get("fidelity"), "member fidelity")
        if set(fidelity) != {
            "identity",
            "method",
            "required_fields",
            "asset_urls",
        }:
            raise C10Error(f"{self.key} member fidelity fields are invalid")
        if fidelity["identity"] != member_id:
            raise C10Error(
                f"{self.key} member fidelity does not bind provider identity"
            )
        if fidelity["method"] != self.member_method:
            raise C10Error(f"{self.key} member fidelity does not use its native method")
        if (
            not isinstance(fidelity["required_fields"], list)
            or not fidelity["required_fields"]
        ):
            raise C10Error(f"{self.key} member lacks native required-field proof")
        assets = fidelity["asset_urls"]
        if not isinstance(assets, list) or len(assets) != len(set(assets)):
            raise C10Error(f"{self.key} member asset proof is invalid")
        if any(
            not isinstance(asset, str) or not asset.startswith("https://")
            for asset in assets
        ):
            raise C10Error(f"{self.key} member asset proof is unsafe")
        return evidence

    def verify_member(self, evidence: Mapping[str, Any]) -> None:
        self._member(evidence)

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        """Only Matthews currently has a reviewed provider-specific rule."""
        return False


@dataclass(frozen=True)
class SavillsAdapter(BatchBAdapter):
    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        self._header(evidence, "c10_savills_nexturl_total_v1")
        pages = _items(evidence.get("pages"), "Savills pages")
        total = _integer(
            evidence.get("reported_total"), "Savills reported total", minimum=1
        )
        seen: set[str] = set()
        for index, page in enumerate(pages):
            _url(page.get("url"), self.hosts, "Savills page URL")
            next_url = page.get("next_url")
            if index == len(pages) - 1:
                if next_url is not None:
                    raise C10Error("Savills terminal NextUrl must be null")
            else:
                _url(next_url, self.hosts, "Savills NextUrl")
            for row in _items(page.get("rows"), "Savills page rows"):
                member_id = _id(
                    row.get("external_property_id"), "Savills ExternalPropertyID"
                )
                _url(row.get("canonical_url"), self.hosts, "Savills canonical URL")
                if member_id in seen:
                    raise C10Error("Savills ExternalPropertyID is duplicated")
                seen.add(member_id)
        if len(seen) != total:
            raise C10Error("Savills NextUrl pages do not reconcile to reported total")


@dataclass(frozen=True)
class NaiGlobalAdapter(BatchBAdapter):
    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        self._header(evidence, "c10_nai_global_public_posts_v1")
        require_sha256(evidence.get("source_ids_sha256"), "NAI source IDs")
        batches = _items(evidence.get("batches"), "NAI source batches")
        seen: set[str] = set()
        for batch in batches:
            ids = batch.get("source_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or any(type(item) is not int for item in ids)
            ):
                raise C10Error("NAI source batch identities are invalid")
            pages = _items(batch.get("pages"), "NAI pages")
            for offset, page in enumerate(pages):
                if _integer(page.get("offset"), "NAI page offset") != offset * 100:
                    raise C10Error("NAI pagination offset is non-contiguous")
                rows = page.get("rows")
                if not isinstance(rows, list) or any(
                    not isinstance(row, Mapping) for row in rows
                ):
                    raise C10Error("NAI page rows are invalid")
                if len(rows) > 100:
                    raise C10Error("NAI page exceeds native page size")
                if offset < len(pages) - 1 and len(rows) != 100:
                    raise C10Error("NAI short page appeared before batch completion")
                for row in rows:
                    member_id = _id(
                        row.get("public_post_id"), "NAI public post identity"
                    )
                    _url(row.get("canonical_url"), self.hosts, "NAI canonical URL")
                    if member_id in seen:
                        raise C10Error("NAI public post identity is duplicated")
                    seen.add(member_id)
            if len(pages[-1]["rows"]) >= 100:
                raise C10Error("NAI batch lacks a native short-page completion proof")


@dataclass(frozen=True)
class TranswesternAdapter(BatchBAdapter):
    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        self._header(evidence, "c10_transwestern_ajax_buckets_v1")
        buckets = _items(evidence.get("buckets"), "Transwestern buckets")
        seen: set[str] = set()
        for bucket in buckets:
            if bucket.get("name") not in {"Sale", "Lease", "Sublease", "Sale or Lease"}:
                raise C10Error("Transwestern bucket is not native")
            rows = _items(bucket.get("rows"), "Transwestern bucket rows")
            if _integer(
                bucket.get("reported_count"), "Transwestern bucket count"
            ) != len(rows):
                raise C10Error("Transwestern bucket count is not complete")
            for row in rows:
                member_id = _id(row.get("page_url"), "Transwestern PageUrl")
                _url(row.get("canonical_url"), self.hosts, "Transwestern canonical URL")
                if member_id in seen:
                    raise C10Error("Transwestern PageUrl is duplicated across buckets")
                seen.add(member_id)


@dataclass(frozen=True)
class MatthewsAdapter(BatchBAdapter):
    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        self._header(evidence, "c10_matthews_sitemap_v1")
        urls = evidence.get("property_urls")
        if not isinstance(urls, list) or not urls:
            raise C10Error("Matthews sitemap property URLs are missing")
        if len(urls) != len(set(urls)):
            raise C10Error("Matthews sitemap has duplicate property URLs")
        for value in urls:
            url = _url(value, self.hosts, "Matthews property URL")
            if "/properties/" not in url:
                raise C10Error("Matthews sitemap URL is not a property URL")

    def classify_not_found(self, evidence: Mapping[str, Any]) -> bool:
        required = {
            "source_key": "matthews",
            "status": 200,
            "canonical_identity_matches": True,
            "tenure_matches": True,
            "has_property_detail_dom": False,
            "next_redirect_to_listings": True,
            "page_not_found_heading": True,
        }
        return dict(evidence) == required


@dataclass(frozen=True)
class FoundryCommercialAdapter(BatchBAdapter):
    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        self._header(evidence, "c10_foundry_property_sitemap_v1")
        sitemaps = _items(
            evidence.get("property_sitemaps"), "Foundry property sitemaps"
        )
        seen: set[str] = set()
        for sitemap in sitemaps:
            _url(sitemap.get("url"), self.hosts, "Foundry property sitemap URL")
            for row in _items(sitemap.get("rows"), "Foundry sitemap rows"):
                member_id = _id(row.get("provider_id"), "Foundry provider identity")
                _url(row.get("canonical_url"), self.hosts, "Foundry canonical URL")
                if row.get("explicit_status") not in {
                    "for sale",
                    "for lease",
                    "sublease",
                    "for sale or lease",
                    "for lease or sale",
                    "sale and lease",
                    "available",
                    "coming soon",
                    "proposed",
                    "under contract",
                }:
                    raise C10Error("Foundry row lacks an admitted active native status")
                if member_id in seen:
                    raise C10Error("Foundry provider identity is duplicated")
                seen.add(member_id)


@dataclass(frozen=True)
class DaumCommercialAdapter(BatchBAdapter):
    def verify_enumeration(self, evidence: Mapping[str, Any]) -> None:
        self._header(evidence, "c10_daum_wordpress_snapshot_v1")
        total = _integer(
            evidence.get("reported_total"), "Daum reported total", minimum=1
        )
        pages = _items(evidence.get("pages"), "Daum pages")
        seen: set[str] = set()
        for expected, page in enumerate(pages, start=1):
            if _integer(page.get("page"), "Daum page", minimum=1) != expected:
                raise C10Error("Daum WordPress pages are non-contiguous")
            for row in _items(page.get("rows"), "Daum page rows"):
                member_id = _id(row.get("post_id"), "Daum post identity")
                _url(row.get("canonical_url"), self.hosts, "Daum canonical URL")
                if member_id in seen:
                    raise C10Error("Daum post identity is duplicated")
                seen.add(member_id)
        if len(seen) != total:
            raise C10Error("Daum snapshot does not reconcile to reported total")


def strict_detail_batch_b_adapters() -> dict[str, BatchBAdapter]:
    """Explicit incomplete adapters; callers must not treat this as admission."""
    definitions = (
        (
            SavillsAdapter,
            "savills",
            {"search.savills.com"},
            "nexturl_total",
            "savills_next_data",
            2,
            1000,
            1,
        ),
        (
            NaiGlobalAdapter,
            "nai-global",
            {"ab.infabode.com", "infabode.com"},
            "public_posts",
            "infabode_public_posts",
            2,
            0,
            0,
        ),
        (
            TranswesternAdapter,
            "transwestern",
            {"transwestern.com", "www.transwestern.com"},
            "ajax_buckets",
            "transwestern_detail_page",
            1,
            0,
            0,
        ),
        (
            MatthewsAdapter,
            "matthews",
            {"matthews.com", "www.matthews.com"},
            "sitemap",
            "matthews_canonical_detail",
            2,
            1800,
            0,
        ),
        (
            FoundryCommercialAdapter,
            "foundry-commercial",
            {"foundrycommercial.com", "www.foundrycommercial.com"},
            "property_sitemap",
            "foundry_wordpress_detail",
            2,
            0,
            0,
        ),
        (
            DaumCommercialAdapter,
            "daum-commercial",
            {"daumcommercial.com", "www.daumcommercial.com"},
            "wordpress_snapshot",
            "daum_wordpress_detail",
            1,
            3000,
            0,
        ),
    )
    return {
        key: adapter(
            key=key,
            hosts=frozenset(hosts),
            enumeration_kind=kind,
            member_method=member_method,
            capability=ReceiptFetchCapability(key, concurrency, pace, retries),
        )
        for adapter, key, hosts, kind, member_method, concurrency, pace, retries in definitions
    }
