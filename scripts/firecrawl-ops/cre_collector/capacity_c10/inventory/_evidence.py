"""Small no-I/O primitives shared by source-specific inventory verifiers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..contracts import C10Error, require_sha256, sha256


def exact_mapping(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise C10Error(f"{label} has an invalid key set")
    return value


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise C10Error(f"{label} must be a nonempty string")
    return value.strip()


def observed_at(value: Any, label: str = "observed_at") -> str:
    raw = text(value, label)
    if not raw.endswith("Z"):
        raise C10Error(f"{label} must be UTC Z time")
    try:
        parsed = datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise C10Error(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise C10Error(f"{label} must be UTC")
    return raw


def https_url(value: Any, hosts: set[str], label: str) -> str:
    raw = text(value, label)
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise C10Error(f"{label} is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        raise C10Error(f"{label} is outside the source host contract")
    return raw


def digest_matches(value: Any, document: Any, label: str) -> str:
    digest = require_sha256(value, label)
    if digest != sha256(document):
        raise C10Error(f"{label} does not bind its evidence")
    return digest


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise C10Error(f"{label} must be a positive integer")
    return value


def nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise C10Error(f"{label} must be a nonnegative integer")
    return value


def unique_strings(values: Any, label: str) -> list[str]:
    if not isinstance(values, list):
        raise C10Error(f"{label} must be a list")
    normalized = [text(value, label) for value in values]
    if len(normalized) != len(set(normalized)):
        raise C10Error(f"{label} contains duplicate identities")
    return normalized


def require_asset_urls(value: Any, label: str) -> list[str]:
    """Require public HTTPS assets without inventing a provider CDN allowlist.

    The source host is authoritative for a listing's canonical URL.  Existing
    source parsers preserve provider-hosted CDN documents and image URLs, so an
    adapter must bind them to raw evidence but must not reject a valid asset
    solely because it is not served from the listing host.
    """
    if not isinstance(value, list):
        raise C10Error(f"{label} must be a list")
    urls: list[str] = []
    for item in value:
        raw = text(item, label)
        try:
            parsed = urlsplit(raw)
        except ValueError as exc:
            raise C10Error(f"{label} is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            raise C10Error(f"{label} must contain a public HTTPS asset URL")
        urls.append(raw)
    if len(urls) != len(set(urls)):
        raise C10Error(f"{label} contains duplicate URLs")
    return urls


def require_pacing(
    value: Any, *, concurrency: int | str, retry: str, label: str
) -> None:
    exact_mapping(value, {"concurrency", "retry"}, label)
    if value["concurrency"] != concurrency or value["retry"] != retry:
        raise C10Error(f"{label} does not match the reviewed source controls")


def require_exact_pages(
    pages: Any,
    *,
    total: int,
    page_size: int,
    row_key: str,
    identity: Callable[[Mapping[str, Any]], str],
    first_page: int = 1,
    label: str,
) -> list[Mapping[str, Any]]:
    if not isinstance(pages, list) or not pages:
        raise C10Error(f"{label} requires pages")
    expected_count = (total + page_size - 1) // page_size
    if len(pages) != expected_count:
        raise C10Error(f"{label} page count is incomplete")
    rows: list[Mapping[str, Any]] = []
    for index, page in enumerate(pages):
        exact_mapping(page, {"page", "total", row_key}, f"{label} page")
        if page["page"] != first_page + index or page["total"] != total:
            raise C10Error(f"{label} page metadata drifted")
        page_rows = page[row_key]
        if not isinstance(page_rows, list):
            raise C10Error(f"{label} page rows are invalid")
        expected_rows = (
            page_size if index < expected_count - 1 else total - page_size * index
        )
        if len(page_rows) != expected_rows:
            raise C10Error(f"{label} page is truncated")
        if not all(isinstance(row, Mapping) for row in page_rows):
            raise C10Error(f"{label} row is invalid")
        rows.extend(page_rows)
    identities = [identity(row) for row in rows]
    if len(identities) != total or len(identities) != len(set(identities)):
        raise C10Error(f"{label} population identities are incomplete or duplicate")
    return rows


def require_member_envelope(
    evidence: Mapping[str, Any],
    *,
    key: str,
    hosts: set[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any], str, str]:
    exact_mapping(
        evidence,
        {
            "source_key",
            "observed_at",
            "provider_id",
            "canonical_url",
            "raw",
            "raw_sha256",
            "normalized",
            "normalized_sha256",
        },
        f"{key} member evidence",
    )
    if evidence["source_key"] != key:
        raise C10Error(f"{key} member evidence is assigned to another source")
    observed_at(evidence["observed_at"])
    provider_id = text(evidence["provider_id"], f"{key} provider_id")
    canonical_url = https_url(evidence["canonical_url"], hosts, f"{key} canonical_url")
    raw = evidence["raw"]
    normalized = evidence["normalized"]
    if not isinstance(raw, Mapping) or not isinstance(normalized, Mapping):
        raise C10Error(f"{key} member payload is invalid")
    digest_matches(evidence["raw_sha256"], raw, f"{key} raw_sha256")
    digest_matches(
        evidence["normalized_sha256"], normalized, f"{key} normalized_sha256"
    )
    return raw, normalized, provider_id, canonical_url


def require_normalized_fidelity(
    normalized: Mapping[str, Any],
    *,
    expected_fields: Mapping[str, Any],
    label: str,
) -> None:
    exact_mapping(normalized, {"fields", "assets"}, f"{label} normalized")
    fields = normalized["fields"]
    assets = normalized["assets"]
    if not isinstance(fields, Mapping) or dict(fields) != dict(expected_fields):
        raise C10Error(f"{label} normalized fields do not match source evidence")
    exact_mapping(assets, {"images", "documents"}, f"{label} assets")
    require_asset_urls(assets["images"], f"{label} image assets")
    require_asset_urls(assets["documents"], f"{label} document assets")


def false_not_found(_: Mapping[str, Any]) -> bool:
    """Inventory sources in this batch have no reviewed attrition classifier."""
    return False
