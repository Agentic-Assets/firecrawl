#!/usr/bin/env python3
"""Offline-only multisource-v1 cohort admission for CRE capacity experiments.

This module deliberately does not scrape, invoke an adapter, or start an
experiment.  It binds reviewed provider receipts into an immutable cohort.
Adapters without an explicit provider not-found classifier stay unsupported for
attrition rather than inheriting a generic HTTP-status rule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import urllib.parse
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CONFIG = Path(__file__).with_name("cre_capacity_multisource_v1.json")
MAX_CONFIG_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_RAW_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_REDACTED_HEADER_VALUE_BYTES = 512
SAFE_REDACTED_HEADERS = frozenset(
    {
        "cache-control",
        "cf-cache-status",
        "cf-ray",
        "content-type",
        "date",
        "retry-after",
        "server",
        "x-request-id",
    }
)
CLASSIFICATIONS = frozenset(
    {
        "eligible_detail",
        "confirmed_current_attrition",
        "detail_unavailable_by_design",
        "invalid_target",
        "challenge_or_throttle",
        "transport_failure",
        "provider_5xx",
        "not_listing_content",
        "parser_failure",
        "unclassified",
    }
)
PLANES = frozenset({"strict_detail", "authoritative_inventory"})
STRATUM_FIELDS = ("transaction_class", "property_type", "page_weight_band")
_JLL_TRANSACTION_CLASSES = {
    "sale": "sale",
    "for sale": "sale",
    "investment sale": "sale",
    "lease": "lease",
    "for lease": "lease",
    "rent": "lease",
    "sale/lease": "sale_or_lease",
    "sale or lease": "sale_or_lease",
}
_JLL_PROPERTY_TYPES = {
    "office": "office",
    "industrial": "industrial",
    "retail": "retail",
    "medical": "medical",
    "multifamily": "multifamily",
    "land": "land",
    "hospitality": "hospitality",
    "mixed use": "mixed_use",
    "special purpose": "special_purpose",
}
_JLL_SEMANTIC_FIELDS = (
    "address",
    "name",
    "transaction_type",
    "property_type",
)
_JLL_REQUIRED_SEMANTIC_FIELDS = frozenset({"transaction_type", "property_type"})
_JLL_ASSET_CHANNELS = {
    "images": "property.images",
    "brochures": "property.brochures",
    "floorPlans": "property.floorPlans",
    "videos": "property.videos",
    "virtualTours": "property.virtualTours",
    "view360URLs": "property.view360URLs",
}
_JLL_GRAPHQL_PATH = "/api/graphql"
_JLL_PAGE_TAKE = 50
_JLL_GRAPHQL_ORDER_BY = {
    "field": "dateModified",
    "direction": "desc",
    "imagePriority": True,
}
_JLL_ENUMERATION_PROPERTY_TYPES = frozenset(
    {
        "office",
        "industrial",
        "retail",
        "land",
        "medical",
        "multifamily",
        "lab",
        "coworking",
        "data-center",
    }
)
_JLL_SUPPORTED_TENURES = frozenset({"sale", "rent"})
_JLL_SEARCH_RESULTS_QUERY_SHA256 = (
    "a37aa4cde62fc942439ef618db119157f6158a70a9700acc7cd44a5d6577a9b6"
)
# This is a versioned prevalidation lane, not a generic registry projection.
# Keep the review matrix adjacent to its verifier so a config edit cannot point
# an admitted cohort at a stale proxy/search host by accident.
EXPECTED_SOURCE_HOSTS = {
    "jll": ("property.jll.com",),
    "jll-investor": ("invest.jll.com",),
    "colliers": ("sales.colliers.com", "my.rcm1.com"),
    "colliers-main": ("colliers.com", "www.colliers.com"),
    "marcus-millichap": ("marcusmillichap.com", "www.marcusmillichap.com"),
    "avison-young": ("www.avisonyoung.us", "pse-api.sharplaunch.com"),
    "savills": ("search.savills.com",),
    "nai-global": ("ab.infabode.com", "infabode.com"),
    "transwestern": ("transwestern.com", "www.transwestern.com"),
    "matthews": ("matthews.com", "www.matthews.com"),
    "foundry-commercial": ("foundrycommercial.com", "www.foundrycommercial.com"),
    "daum-commercial": ("daumcommercial.com", "www.daumcommercial.com"),
    "cbre": ("cbre.com", "www.cbre.com"),
    "cbre-dealflow": ("www.cbredealflow.com",),
    "cushman-wakefield": ("cushmanwakefield.com", "www.cushmanwakefield.com"),
    "newmark": (
        "api-public.nim.nmrk.com",
        "nim.nmrk.com",
        "nmrk.com",
        "www.nmrk.com",
    ),
    "svn": ("svn.com",),
    "lee-associates": ("www.lee-associates.com",),
    "srs": (
        "srsre-next-412955565034.us-central1.run.app",
        "srsre.com",
        "www.srsre.com",
    ),
    "bull-realty": ("www.bullrealty.com",),
}
EXPECTED_PLANE_COUNTS = {"strict_detail": 12, "authoritative_inventory": 8}


class MultisourceError(ValueError):
    """The offline cohort evidence is not safe to admit."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _enumeration_identity(
    source_key: str,
    provider_id: str,
    canonical_url: str,
    observed_at: str,
    receipt_sha256: str,
    body_sha256: str,
    total: int,
) -> str:
    """Bind a prevalidation row to the exact freshly enumerated provider target."""
    return _sha256(
        _canonical(
            {
                "source_key": source_key,
                "provider_id": provider_id,
                "canonical_url": canonical_url,
                "observed_at": observed_at,
                "receipt_sha256": receipt_sha256,
                "body_sha256": body_sha256,
                "total": total,
            }
        )
    )


def _read_json(path: Path, maximum: int) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MultisourceError(f"cannot read {path}") from exc
    if not raw or len(raw) > maximum:
        raise MultisourceError(f"invalid size for {path}")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MultisourceError(f"invalid JSON in {path}") from exc


def _private_root(path: Path) -> Path:
    if not path.is_absolute():
        raise MultisourceError("receipt root must be absolute")
    try:
        opened = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MultisourceError("receipt root is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or path != resolved
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        raise MultisourceError("receipt root must be private mode 0700")
    return resolved


def _private_regular_bytes(path: Path, maximum: int, *, root: Path) -> bytes:
    """Read one bounded private file from its verified, no-follow descriptor."""
    if not path.is_absolute():
        raise MultisourceError("receipt paths must be absolute")
    try:
        parent = path.parent
        resolved_parent = parent.resolve(strict=True)
        opened = path.lstat()
    except OSError as exc:
        raise MultisourceError("receipt is unavailable") from exc
    if parent != resolved_parent or (
        resolved_parent != root and root not in resolved_parent.parents
    ):
        raise MultisourceError("receipt is not a bounded regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MultisourceError("receipt is unavailable") from exc
    try:
        opened_descriptor = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(opened_descriptor.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (opened_descriptor.st_dev, opened_descriptor.st_ino)
            or opened.st_nlink != 1
            or opened_descriptor.st_nlink != 1
            or opened.st_size <= 0
            or opened.st_size > maximum
            or stat.S_IMODE(opened.st_mode) != 0o600
            or stat.S_IMODE(opened_descriptor.st_mode) != 0o600
        ):
            raise MultisourceError("receipt is not a bounded regular file")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        closed_descriptor = os.fstat(descriptor)
    except OSError as exc:
        raise MultisourceError("receipt is unavailable") from exc
    finally:
        os.close(descriptor)
    if (
        len(raw) != opened_descriptor.st_size
        or len(raw) > maximum
        or (opened_descriptor.st_dev, opened_descriptor.st_ino)
        != (closed_descriptor.st_dev, closed_descriptor.st_ino)
        or opened_descriptor.st_size != closed_descriptor.st_size
        or opened_descriptor.st_mtime_ns != closed_descriptor.st_mtime_ns
        or opened_descriptor.st_ctime_ns != closed_descriptor.st_ctime_ns
    ):
        raise MultisourceError("receipt is not a bounded regular file")
    return bytes(raw)


def _exclusive_private_write(path: Path, value: bytes) -> None:
    """Write one new 0600 regular file without following or replacing a target."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MultisourceError("producer output cannot be created exclusively") from exc
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        raise MultisourceError("producer output cannot be written") from exc
    finally:
        os.close(descriptor)
    try:
        opened = path.lstat()
    except OSError as exc:
        raise MultisourceError("producer output is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600:
        raise MultisourceError("producer output is not a private regular file")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MultisourceError(
            "producer output directory cannot be synchronized"
        ) from exc


def _entry_inode(directory_fd: int, name: str) -> tuple[int, int]:
    """Read one root entry by descriptor without following a replacement link."""
    try:
        opened = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise MultisourceError("producer output entry is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode):
        raise MultisourceError("producer output entry is not a regular file")
    return opened.st_dev, opened.st_ino


def _path_inode(path: Path) -> tuple[int, int]:
    """Read one staged regular file without accepting a symbolic-link source."""
    try:
        opened = path.lstat()
    except OSError as exc:
        raise MultisourceError("producer staged output is unavailable") from exc
    if not stat.S_ISREG(opened.st_mode):
        raise MultisourceError("producer staged output is not a regular file")
    return opened.st_dev, opened.st_ino


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MultisourceError("producer output directory is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise MultisourceError("producer output directory is unavailable") from exc
    if not stat.S_ISDIR(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o700:
        os.close(descriptor)
        raise MultisourceError("producer output directory is not private")
    return descriptor


def _publish_staged_file(
    staged_path: Path,
    final_path: Path,
    *,
    root_fd: int,
    published: set[str],
) -> None:
    """Publish an owned staged file, refusing a destination changed during link."""
    staged_inode = _path_inode(staged_path)
    try:
        os.link(staged_path, final_path, follow_symlinks=False)
    except OSError as exc:
        raise MultisourceError("producer output cannot be published") from exc
    final_inode = _entry_inode(root_fd, final_path.name)
    if final_inode != staged_inode:
        raise MultisourceError("producer output changed during publication")
    published.add(final_path.name)
    staged_path.unlink()


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    document = _read_json(path, MAX_CONFIG_BYTES)
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != "cre_capacity_multisource_v1_config"
        or not isinstance(document.get("sampling"), dict)
        or not isinstance(document.get("profiles"), dict)
        or not isinstance(document.get("counterbalance"), dict)
        or not isinstance(document.get("sources"), list)
    ):
        raise MultisourceError("multisource-v1 configuration is invalid")
    sampling = document["sampling"]
    if (
        set(sampling)
        != {
            "calibration_per_source",
            "core_per_source",
            "minimum_detail_eligible_for_core",
            "minimum_sources_for_experiment",
            "stratification_seed",
            "maximum_enumeration_age_seconds",
        }
        or any(
            type(sampling[key]) is not int or sampling[key] < 1
            for key in (
                "calibration_per_source",
                "core_per_source",
                "minimum_detail_eligible_for_core",
                "minimum_sources_for_experiment",
                "maximum_enumeration_age_seconds",
            )
        )
        or not isinstance(sampling["stratification_seed"], str)
        or not sampling["stratification_seed"]
        or sampling["calibration_per_source"] > sampling["core_per_source"]
        or sampling["minimum_detail_eligible_for_core"] > sampling["core_per_source"]
    ):
        raise MultisourceError("multisource-v1 sampling controls are invalid")
    profiles = document["profiles"]
    if set(profiles) != {"P0", "P1", "P2"}:
        raise MultisourceError("multisource-v1 profiles are invalid")
    expected_profiles = {
        "P0": {"browser_cpus": 2, "global_pages": 4, "source_workers": 1},
        "P1": {"browser_cpus": 6, "global_pages": 10, "source_workers": 1},
        "P2": {
            "browser_cpus": 6,
            "global_pages": 10,
            "source_workers": 2,
            "future_executor_provider_family_exclusions": [
                "buildout",
                "cbre",
                "colliers",
                "jll",
            ],
        },
    }
    if profiles != expected_profiles:
        raise MultisourceError("multisource-v1 runtime profiles drifted")
    if document.get("workload_weighting") != {
        "basis": "fresh_enumeration_total",
        "winsorize_percentile": 0.95,
    }:
        raise MultisourceError("multisource-v1 workload weighting drifted")
    sources = document["sources"]
    source_keys = {item.get("key") for item in sources if isinstance(item, dict)}
    if len(sources) != 20 or source_keys != set(EXPECTED_SOURCE_HOSTS):
        raise MultisourceError(
            "multisource-v1 source matrix must contain 20 unique sources"
        )
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source)
            - {
                "key",
                "plane",
                "provider_family",
                "hosts",
                "not_found_classifier",
                "exclusive",
                "allow_query",
                "query_contract",
            }
            or not isinstance(source.get("key"), str)
            or source.get("plane") not in PLANES
            or not isinstance(source.get("provider_family"), str)
            or not isinstance(source.get("hosts"), list)
            or not source["hosts"]
            or not all(isinstance(host, str) and host for host in source["hosts"])
            or source.get("not_found_classifier")
            not in {None, "jll_next_data_404_no_property"}
            or ("exclusive" in source and type(source["exclusive"]) is not bool)
            or ("allow_query" in source and type(source["allow_query"]) is not bool)
            or (
                "query_contract" in source
                and source["query_contract"] != "buildout_property_id_v1"
            )
        ):
            raise MultisourceError("multisource-v1 source matrix is invalid")
        if tuple(source["hosts"]) != EXPECTED_SOURCE_HOSTS[source["key"]]:
            raise MultisourceError("multisource-v1 source host contract drifted")
        if source.get("query_contract") == "buildout_property_id_v1" and (
            source["provider_family"] != "buildout"
            or source.get("allow_query") is True
        ):
            raise MultisourceError("multisource-v1 query contract is invalid")
    if {
        plane: sum(source["plane"] == plane for source in sources) for plane in PLANES
    } != EXPECTED_PLANE_COUNTS:
        raise MultisourceError("multisource-v1 plane contract drifted")
    return document


def _valid_jll_not_found(raw: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    """The only current v1 attrition classifier: explicit JLL __NEXT_DATA__ 404."""
    if (
        receipt.get("http_status") != 404
        or receipt.get("not_found_classifier") != "jll_next_data_404_no_property"
    ):
        return False
    try:
        body = raw.get("body")
        html = body.get("rawHtml") if isinstance(body, Mapping) else None
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html or "",
            re.IGNORECASE | re.DOTALL,
        )
        next_data = json.loads(match.group(1)) if match else None
        page_props = (
            next_data.get("props", {}).get("pageProps", {})
            if isinstance(next_data, dict)
            else {}
        )
        error = page_props.get("error") if isinstance(page_props, dict) else None
        return bool(
            isinstance(page_props, dict)
            and page_props.get("property") is None
            and page_props.get("notFound") is True
            and isinstance(error, dict)
            and error.get("statusCode") == 404
        )
    except (AttributeError, json.JSONDecodeError):
        return False


def _safe_redacted_headers(value: Any) -> dict[str, str]:
    """Reject headers that could carry credentials or unbounded raw content."""
    if not isinstance(value, dict) or set(value) - SAFE_REDACTED_HEADERS:
        raise MultisourceError("receipt headers are not safely redacted")
    safe: dict[str, str] = {}
    for name, header_value in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(header_value, str)
            or len(header_value.encode()) > MAX_REDACTED_HEADER_VALUE_BYTES
        ):
            raise MultisourceError("receipt headers are not safely redacted")
        safe[name] = header_value
    return safe


def _hex_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MultisourceError(f"{label} hash is invalid")
    return value


def _read_private_json(path: Path, maximum: int, *, root: Path) -> tuple[Any, str]:
    """Parse and hash exactly the bytes consumed through the verified descriptor."""
    raw = _private_regular_bytes(path, maximum, root=root)
    try:
        return json.loads(raw), _sha256(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MultisourceError(f"invalid JSON in {path}") from exc


def _source_config_sha256(source: Mapping[str, Any]) -> str:
    """A row may only claim the source contract admitted by this config."""
    return _sha256(_canonical(dict(source)))


def _buildout_property_id_query(query: str) -> bool:
    """Accept the exact Buildout listing query shape, not arbitrary parameters.

    Buildout's native ``show_link`` always identifies the row with one
    ``propertyId``.  Lee's recorded adapter fixtures also carry its display-only
    ``address`` and numeric ``officeId`` parameters, so retain those exact
    provider parameters while refusing a generic query-string escape hatch.
    """
    if not query or re.search(r"%(?![0-9A-Fa-f]{2})", query):
        return False
    try:
        pairs = urllib.parse.parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError):
        return False
    permitted = {"propertyId", "address", "officeId"}
    if not pairs or any(key not in permitted or not value for key, value in pairs):
        return False
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or keys.count("propertyId") != 1:
        return False
    values = dict(pairs)
    property_id = values["propertyId"]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]*", property_id) is None:
        return False
    if "officeId" in values and not values["officeId"].isdigit():
        return False
    return not any(
        any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in values.values()
    )


def _public_url(value: Any, source: Mapping[str, Any]) -> str:
    if not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise MultisourceError("receipt target is outside the provider host contract")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MultisourceError(
            "receipt target is outside the provider host contract"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname not in source["hosts"]
        or not parsed.path
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (
            parsed.query
            and source.get("allow_query") is not True
            and not (
                source.get("query_contract") == "buildout_property_id_v1"
                and _buildout_property_id_query(parsed.query)
            )
        )
    ):
        raise MultisourceError("receipt target is outside the provider host contract")
    return value


def _canonical_jll_listing_url(value: Any, source: Mapping[str, Any]) -> str:
    """Normalize a JLL inventory/detail target before identity comparison."""
    if not isinstance(value, str):
        raise MultisourceError("receipt target is outside the provider host contract")
    absolute = urllib.parse.urljoin("https://property.jll.com/", value)
    public_url = _public_url(absolute, source)
    parsed = urllib.parse.urlsplit(public_url)
    path = urllib.parse.quote(
        urllib.parse.unquote(parsed.path), safe="/%:@!$&'()*+,;=-._~"
    )
    if not path.startswith("/listings/"):
        raise MultisourceError("JLL target is not a canonical listing URL")
    normalized_path = path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        ("https", parsed.hostname.lower(), normalized_path, "", "")
    )


def _canonical_target_url(value: Any, source: Mapping[str, Any]) -> str:
    if source["key"] == "jll":
        return _canonical_jll_listing_url(value, source)
    public_url = _public_url(value, source)
    if (
        source.get("query_contract") == "buildout_property_id_v1"
        and not urllib.parse.urlsplit(public_url).query
    ):
        raise MultisourceError("receipt target is outside the provider host contract")
    return public_url


def _finite_nonnegative_timing(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _finite_positive_timing(value: Any) -> bool:
    return _finite_nonnegative_timing(value) and value > 0


def _observed_at(value: Any, *, now_utc: datetime, maximum_age: int) -> str:
    # This is deliberately a narrow, UTC-only serial form.  The producer must
    # record the same timestamp in the private enumeration receipt.
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value)
        is None
    ):
        raise MultisourceError("enumeration timestamp is invalid")
    try:
        observed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise MultisourceError("enumeration timestamp is invalid") from exc
    if observed < now_utc - timedelta(
        seconds=maximum_age
    ) or observed > now_utc + timedelta(seconds=60):
        raise MultisourceError("enumeration timestamp is outside the freshness window")
    return value


def _enumeration_binding(
    receipt: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    root: Path,
    now_utc: datetime,
    maximum_age: int,
) -> tuple[str, int, str, bool, dict[str, str] | None]:
    """Rehash an enumeration receipt instead of trusting a row's assertion."""
    enum_path_value = receipt.get("enumeration_receipt_path")
    if not isinstance(enum_path_value, str):
        raise MultisourceError("enumeration receipt path is invalid")
    document, enum_hash = _read_private_json(
        Path(enum_path_value), MAX_RAW_RECEIPT_BYTES, root=root
    )
    if enum_hash != _hex_digest(
        receipt.get("enumeration_receipt_sha256"), label="enumeration receipt"
    ):
        raise MultisourceError("enumeration receipt hash drifted")
    jll_aggregate = source["key"] == "jll"
    required = (
        {
            "kind",
            "observed_at",
            "total",
            "complete",
            "truncated",
            "provider_ids",
            "page_receipts",
            "resolution_receipts",
        }
        if jll_aggregate
        else {
            "observed_at",
            "total",
            "complete",
            "truncated",
            "provider_ids",
            "body",
            "request_url",
            "final_url",
            "http_status",
            "content_type",
            "timing_ms",
        }
    )
    if not isinstance(document, dict) or set(document) != required:
        raise MultisourceError("enumeration receipt is malformed")
    observed_at = _observed_at(
        receipt.get("enumeration_observed_at"),
        now_utc=now_utc,
        maximum_age=maximum_age,
    )
    total = receipt.get("enumeration_total")
    if (
        document["observed_at"] != observed_at
        or type(total) is not int
        or total < 1
        or document["total"] != total
        or document["complete"] is not True
        or document["truncated"] is not False
        or not isinstance(document["provider_ids"], list)
        or not all(
            isinstance(identifier, str) and identifier
            for identifier in document["provider_ids"]
        )
        or receipt.get("provider_id") not in document["provider_ids"]
        or len(document["provider_ids"]) != total
        or len(set(document["provider_ids"])) != total
        or (
            jll_aggregate
            and (
                document["kind"] != "jll_graphql_enumeration_aggregate_v1"
                or not isinstance(document["page_receipts"], list)
                or not document["page_receipts"]
                or not isinstance(document["resolution_receipts"], list)
                or not document["resolution_receipts"]
            )
        )
        or (
            not jll_aggregate
            and (
                not isinstance(document["body"], str)
                or _public_url(document["request_url"], source)
                != document["request_url"]
                or _public_url(document["final_url"], source) != document["final_url"]
                or type(document["http_status"]) is not int
                or document["http_status"] != 200
                or not isinstance(document["content_type"], str)
                or not document["content_type"]
                or not _finite_positive_timing(document["timing_ms"])
            )
        )
    ):
        raise MultisourceError("enumeration completeness proof is invalid")
    body_hash = (
        _sha256(_canonical(document["page_receipts"]))
        if jll_aggregate
        else _sha256(document["body"].encode())
    )
    if body_hash != _hex_digest(
        receipt.get("enumeration_body_sha256"), label="enumeration body"
    ):
        raise MultisourceError("enumeration body hash drifted")
    canonical_url = _canonical_target_url(receipt.get("canonical_url"), source)
    identity = _enumeration_identity(
        source["key"],
        receipt["provider_id"],
        canonical_url,
        observed_at,
        enum_hash,
        body_hash,
        total,
    )
    if receipt.get("enumeration_identity_sha256") != identity:
        raise MultisourceError("receipt lacks a fresh enumeration receipt binding")
    # Only JLL has a reviewed native enumeration parser in v1. A signed
    # wrapper can prove capture integrity, but it cannot establish population
    # size or workload until the provider response itself agrees exactly.
    population_verified = False
    resolution_targets: dict[str, str] | None = None
    if jll_aggregate:
        resolution_targets = _verified_jll_enumeration_population(
            document,
            provider_ids=document["provider_ids"],
            total=total,
            root=root,
            source=source,
            now_utc=now_utc,
            maximum_age=maximum_age,
        )
        population_verified = isinstance(resolution_targets, dict)
        if not population_verified:
            raise MultisourceError("JLL enumeration completeness proof is invalid")
    return identity, total, enum_hash, population_verified, resolution_targets


def _verified_jll_enumeration_population(
    aggregate: Mapping[str, Any],
    *,
    provider_ids: list[str],
    total: int,
    root: Path,
    source: Mapping[str, Any],
    now_utc: datetime,
    maximum_age: int,
) -> dict[str, str] | bool:
    """Validate every native JLL GraphQL page sealed by the aggregate receipt."""
    page_receipts = aggregate.get("page_receipts")
    if not isinstance(page_receipts, list) or not page_receipts:
        return False
    pages_by_filter: dict[tuple[str, str], list[tuple[int, int, list[str]]]] = {}
    page_timestamps: list[datetime] = []
    search_targets: dict[str, str] = {}
    seen_artifacts: set[tuple[str, str]] = set()
    for manifest in page_receipts:
        if not isinstance(manifest, Mapping) or set(manifest) != {"path", "sha256"}:
            return False
        path_value = manifest.get("path")
        if not isinstance(path_value, str):
            return False
        try:
            page_path = Path(path_value)
            page_receipt, actual_hash = _read_private_json(
                page_path, MAX_RAW_RECEIPT_BYTES, root=root
            )
            page_hash = _hex_digest(manifest.get("sha256"), label="JLL page receipt")
        except MultisourceError:
            return False
        artifact_identity = (str(page_path), page_hash)
        if artifact_identity in seen_artifacts or actual_hash != page_hash:
            return False
        seen_artifacts.add(artifact_identity)
        required = {
            "kind",
            "request_url",
            "final_url",
            "http_status",
            "content_type",
            "observed_at",
            "timing_ms",
            "operation_name",
            "variables",
            "request_body",
            "query_sha256",
            "body",
        }
        if not isinstance(page_receipt, Mapping) or set(page_receipt) != required:
            return False
        if (
            page_receipt["kind"] != "jll_graphql_page_receipt_v1"
            or page_receipt["operation_name"] != "SearchResults"
            or not _valid_jll_graphql_url(page_receipt["request_url"], source)
            or page_receipt["final_url"] != page_receipt["request_url"]
            or type(page_receipt["http_status"]) is not int
            or page_receipt["http_status"] != 200
            or not isinstance(page_receipt["content_type"], str)
            or "application/json" not in page_receipt["content_type"].casefold()
            or not _finite_positive_timing(page_receipt["timing_ms"])
            or not isinstance(page_receipt["request_body"], str)
            or page_receipt["query_sha256"] != _JLL_SEARCH_RESULTS_QUERY_SHA256
            or not isinstance(page_receipt["body"], str)
        ):
            return False
        try:
            _observed_at(
                page_receipt["observed_at"],
                now_utc=now_utc,
                maximum_age=maximum_age,
            )
            request_payload = json.loads(page_receipt["request_body"])
            payload = json.loads(page_receipt["body"])
        except (MultisourceError, TypeError, json.JSONDecodeError):
            return False
        variables = page_receipt["variables"]
        if (
            not isinstance(request_payload, Mapping)
            or set(request_payload) != {"query", "variables", "operationName"}
            or not isinstance(request_payload["query"], str)
            or not request_payload["query"].strip()
            or _sha256(request_payload["query"].encode())
            != page_receipt["query_sha256"]
            or request_payload["operationName"] != page_receipt["operation_name"]
            or request_payload["variables"] != variables
        ):
            return False
        if not isinstance(variables, Mapping) or set(variables) != {
            "market",
            "language",
            "propertyTypes",
            "tenureTypes",
            "skip",
            "take",
            "orderBy",
        }:
            return False
        property_types = variables["propertyTypes"]
        tenure_types = variables["tenureTypes"]
        skip = variables["skip"]
        if (
            variables["market"] != "us"
            or variables["language"] != "en"
            or not isinstance(property_types, list)
            or len(property_types) != 1
            or not isinstance(property_types[0], str)
            or not property_types[0].strip()
            or not isinstance(tenure_types, list)
            or len(tenure_types) != 1
            or tenure_types[0] not in _JLL_SUPPORTED_TENURES
            or type(skip) is not int
            or skip < 0
            or skip % _JLL_PAGE_TAKE != 0
            or variables["take"] != _JLL_PAGE_TAKE
            or variables["orderBy"] != _JLL_GRAPHQL_ORDER_BY
        ):
            return False
        if not isinstance(payload, Mapping) or isinstance(payload, list):
            return False
        if "errors" in payload and (
            not isinstance(payload["errors"], list) or payload["errors"]
        ):
            return False
        data = payload.get("data")
        properties = data.get("properties") if isinstance(data, Mapping) else None
        if not isinstance(properties, Mapping):
            return False
        count = properties.get("count")
        items = properties.get("items")
        if type(count) is not int or count < 0 or not isinstance(items, list):
            return False
        ids: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                return False
            search_id = item.get("id")
            if not isinstance(search_id, str) or not search_id.strip():
                return False
            try:
                target = _canonical_jll_listing_url(item.get("pageUrl"), source)
            except MultisourceError:
                return False
            existing_target = search_targets.get(search_id)
            if existing_target is not None and existing_target != target:
                return False
            if target in search_targets.values() and existing_target != target:
                return False
            search_targets[search_id] = target
            ids.append(search_id)
        pages_by_filter.setdefault((property_types[0], tenure_types[0]), []).append(
            (skip, count, ids)
        )
        page_timestamps.append(
            datetime.fromisoformat(
                page_receipt["observed_at"].removesuffix("Z") + "+00:00"
            )
        )
    if (
        {property_type for property_type, _ in pages_by_filter}
        != _JLL_ENUMERATION_PROPERTY_TYPES
        or len({tenure for _, tenure in pages_by_filter}) != 1
        or not page_timestamps
    ):
        return False
    aggregate_observed = datetime.fromisoformat(
        aggregate["observed_at"].removesuffix("Z") + "+00:00"
    )
    if not min(page_timestamps) <= aggregate_observed <= max(page_timestamps):
        return False
    for pages in pages_by_filter.values():
        counts = {count for _, count, _ in pages}
        if len(counts) != 1:
            return False
        count = next(iter(counts))
        expected_skips = list(range(0, count, _JLL_PAGE_TAKE)) or [0]
        actual_skips = sorted(skip for skip, _, _ in pages)
        if actual_skips != expected_skips:
            return False
        filter_ids: list[str] = []
        for skip, _, ids in pages:
            expected_items = min(_JLL_PAGE_TAKE, max(0, count - skip))
            if len(ids) != expected_items:
                return False
            filter_ids.extend(ids)
        if len(filter_ids) != count or len(set(filter_ids)) != count:
            return False
    return _verified_jll_resolution_targets(
        aggregate,
        search_targets=search_targets,
        provider_ids=provider_ids,
        total=total,
        root=root,
        source=source,
        now_utc=now_utc,
        maximum_age=maximum_age,
    )


def _valid_jll_graphql_url(value: Any, source: Mapping[str, Any]) -> bool:
    """Accept only JLL's origin-bound, non-redirected GraphQL endpoint."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        _public_url(value, source) == value
        and parsed.scheme == "https"
        and parsed.hostname == "property.jll.com"
        and parsed.path == _JLL_GRAPHQL_PATH
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
    )


def _verified_jll_resolution_targets(
    aggregate: Mapping[str, Any],
    *,
    search_targets: Mapping[str, str],
    provider_ids: list[str],
    total: int,
    root: Path,
    source: Mapping[str, Any],
    now_utc: datetime,
    maximum_age: int,
) -> dict[str, str] | bool:
    """Resolve every search-card identity to its sealed detail-page numeric ID."""
    manifests = aggregate.get("resolution_receipts")
    if not isinstance(manifests, list) or len(manifests) != len(search_targets):
        return False
    resolved_search_ids: set[str] = set()
    detail_targets: dict[str, str] = {}
    seen_artifacts: set[tuple[str, str]] = set()
    for manifest in manifests:
        if not isinstance(manifest, Mapping) or set(manifest) != {"path", "sha256"}:
            return False
        path_value = manifest.get("path")
        if not isinstance(path_value, str):
            return False
        try:
            resolution_path = Path(path_value)
            resolution, actual_hash = _read_private_json(
                resolution_path, MAX_RAW_RECEIPT_BYTES, root=root
            )
            resolution_hash = _hex_digest(
                manifest.get("sha256"), label="JLL resolution receipt"
            )
        except MultisourceError:
            return False
        artifact = (str(resolution_path), resolution_hash)
        if artifact in seen_artifacts or actual_hash != resolution_hash:
            return False
        seen_artifacts.add(artifact)
        required = {
            "kind",
            "search_id",
            "canonical_url",
            "detail_receipt_path",
            "detail_receipt_sha256",
        }
        if not isinstance(resolution, Mapping) or set(resolution) != required:
            return False
        search_id = resolution.get("search_id")
        if (
            resolution.get("kind") != "jll_detail_resolution_receipt_v1"
            or not isinstance(search_id, str)
            or search_id in resolved_search_ids
            or search_targets.get(search_id) != resolution.get("canonical_url")
        ):
            return False
        try:
            canonical_url = _canonical_jll_listing_url(
                resolution["canonical_url"], source
            )
            detail_path = Path(resolution["detail_receipt_path"])
            detail, actual_detail_hash = _read_private_json(
                detail_path, MAX_RAW_RECEIPT_BYTES, root=root
            )
            detail_hash = _hex_digest(
                resolution["detail_receipt_sha256"], label="JLL detail receipt"
            )
        except (KeyError, MultisourceError, TypeError):
            return False
        if actual_detail_hash != detail_hash:
            return False
        detail_required = {
            "kind",
            "request_url",
            "final_url",
            "http_status",
            "content_type",
            "observed_at",
            "timing_ms",
            "body",
        }
        if not isinstance(detail, Mapping) or set(detail) != detail_required:
            return False
        if (
            detail.get("kind") != "jll_detail_page_receipt_v1"
            or _canonical_jll_listing_url(detail.get("request_url"), source)
            != canonical_url
            or detail.get("final_url") != detail.get("request_url")
            or detail.get("http_status") != 200
            or not isinstance(detail.get("content_type"), str)
            or "text/html" not in detail["content_type"].casefold()
            or not _finite_positive_timing(detail.get("timing_ms"))
            or not isinstance(detail.get("body"), str)
        ):
            return False
        try:
            _observed_at(
                detail["observed_at"], now_utc=now_utc, maximum_age=maximum_age
            )
        except MultisourceError:
            return False
        property_value = _jll_next_property({"body": {"rawHtml": detail["body"]}})
        detail_id = property_value.get("id") if property_value else None
        detail_url = property_value.get("pageUrl") if property_value else None
        if (
            not isinstance(detail_id, str)
            or re.fullmatch(r"[0-9]+", detail_id) is None
            or detail_id in detail_targets
        ):
            return False
        try:
            if _canonical_jll_listing_url(detail_url, source) != canonical_url:
                return False
        except MultisourceError:
            return False
        resolved_search_ids.add(search_id)
        detail_targets[detail_id] = canonical_url
    return (
        detail_targets
        if resolved_search_ids == set(search_targets)
        and len(detail_targets) == total
        and set(provider_ids) == set(detail_targets)
        and all(re.fullmatch(r"[0-9]+", identifier) for identifier in provider_ids)
        else False
    )


def _extractor_receipt_binding(
    receipt: Mapping[str, Any], *, root: Path
) -> Mapping[str, Any]:
    """Rehash the extractor receipt that binds all detail-side artifacts."""
    path_value = receipt.get("extractor_receipt_path")
    if not isinstance(path_value, str):
        raise MultisourceError("extractor receipt path is invalid")
    document, actual = _read_private_json(
        Path(path_value), MAX_RAW_RECEIPT_BYTES, root=root
    )
    if actual != _hex_digest(
        receipt.get("extractor_receipt_sha256"), label="extractor receipt"
    ):
        raise MultisourceError("extractor receipt hash drifted")
    required = {
        "source_key",
        "provider_id",
        "canonical_url",
        "request_url",
        "final_url",
        "http_status",
        "content_type",
        "observed_at",
        "timing_ms",
        "raw_receipt_sha256",
        "parser_sha256",
        "normalized_sha256",
        "field_locator_sha256",
        "asset_evidence_sha256",
    }
    if not isinstance(document, Mapping) or set(document) != required:
        raise MultisourceError("extractor receipt is malformed")
    for key in required:
        if document[key] != receipt.get(key):
            raise MultisourceError("extractor receipt does not bind row evidence")
    return document


def _raw_receipt_binding(
    receipt: Mapping[str, Any], source: Mapping[str, Any], *, root: Path
) -> tuple[Mapping[str, Any], str, str]:
    """Bind receipt transport claims to bytes captured by the extractor."""
    raw_path_value = receipt.get("raw_receipt_path")
    if not isinstance(raw_path_value, str):
        raise MultisourceError("raw receipt path is invalid")
    document, raw_hash = _read_private_json(
        Path(raw_path_value), MAX_RAW_RECEIPT_BYTES, root=root
    )
    if raw_hash != receipt["raw_receipt_sha256"]:
        raise MultisourceError("raw receipt hash drifted")
    required = {
        "request_url",
        "final_url",
        "http_status",
        "content_type",
        "observed_at",
        "timing_ms",
        "body",
    }
    if not isinstance(document, Mapping) or set(document) != required:
        raise MultisourceError("raw receipt is malformed")
    if (
        _public_url(document["request_url"], source) != document["request_url"]
        or _public_url(document["final_url"], source) != document["final_url"]
        or document["request_url"] != receipt["request_url"]
        or document["final_url"] != receipt["final_url"]
        or document["http_status"] != receipt["http_status"]
        or document["content_type"] != receipt["content_type"]
        or document["observed_at"] != receipt["observed_at"]
        or document["timing_ms"] != receipt["timing_ms"]
    ):
        raise MultisourceError("raw receipt does not bind transport evidence")
    return document, raw_hash, _sha256(_canonical(document["body"]))


_CHALLENGE_MARKERS = (
    "captcha",
    "cf-chl-",
    "cloudflare challenge",
    "challenge-platform",
    "verify you are human",
)
_CHALLENGE_HTTP_STATUSES = frozenset({401, 403, 429})


def _raw_challenge_classification(raw: Mapping[str, Any]) -> str | None:
    """Derive a stop signal only from the bound transport/body evidence."""
    if raw.get("http_status") in _CHALLENGE_HTTP_STATUSES:
        return "challenge_or_throttle"
    body = raw.get("body")
    if isinstance(body, Mapping):
        text = " ".join(value for value in body.values() if isinstance(value, str))
    elif isinstance(body, str):
        text = body
    else:
        text = ""
    if any(marker in text.casefold() for marker in _CHALLENGE_MARKERS):
        return "challenge_or_throttle"
    return None


def _jll_next_property(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the exact public JLL property object used by the v1 verifier."""
    body = raw.get("body")
    html = body.get("rawHtml") if isinstance(body, Mapping) else None
    if not isinstance(html, str):
        return None
    try:
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        next_data = json.loads(match.group(1)) if match else None
        property_value = (
            next_data.get("props", {}).get("pageProps", {}).get("property")
            if isinstance(next_data, Mapping)
            else None
        )
    except (AttributeError, json.JSONDecodeError):
        return None
    return property_value if isinstance(property_value, Mapping) else None


def _jll_property_source_field(
    property_value: Mapping[str, Any], normalized_field: str
) -> tuple[str, Any] | None:
    """Return only an unambiguous JLL detail value and its locator path."""
    direct_fields = {"address": "address", "name": "title"}
    if normalized_field in direct_fields:
        key = direct_fields[normalized_field]
        value = property_value.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        return (f"property.{key}", value)
    collection_fields = {
        "transaction_type": "tenureTypes",
        "property_type": "propertyTypes",
    }
    key = collection_fields.get(normalized_field)
    value = property_value.get(key) if key else None
    if (
        not isinstance(value, list)
        or len(value) != 1
        or not isinstance(value[0], str)
        or not value[0].strip()
    ):
        return None
    return (f"property.{key}[0]", value[0])


def _jll_field_presence(
    property_value: Mapping[str, Any], normalized_field: str
) -> str:
    """Classify a contract field without treating omitted evidence as absence."""
    source = _jll_property_source_field(property_value, normalized_field)
    return "present" if source is not None else "absent"


def _public_asset_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _jll_asset_values(value: Any) -> list[Any]:
    """Flatten JLL's native asset shapes without inventing a cross-channel map."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        flattened: list[Any] = []
        for item in value:
            flattened.extend(_jll_asset_values(item))
        return flattened
    if isinstance(value, Mapping):
        flattened = []
        for key in ("url", "image", "download", "file", "images", "files"):
            if key in value:
                flattened.extend(_jll_asset_values(value[key]))
        return flattened
    return []


def _url_set_hash(values: list[str]) -> str:
    return _sha256(_canonical(sorted(set(values))))


def _jll_asset_contract(
    property_value: Mapping[str, Any],
    normalized: Mapping[str, Any],
    assets: Mapping[str, Any],
) -> bool:
    """Bind every native JLL asset channel to its exact normalized URL set."""
    normalized_assets = normalized.get("assets")
    channels = assets.get("channels")
    if (
        not isinstance(normalized_assets, Mapping)
        or set(normalized_assets) != set(_JLL_ASSET_CHANNELS)
        or not isinstance(channels, Mapping)
        or set(channels) != set(_JLL_ASSET_CHANNELS)
    ):
        return False
    for channel, source_path in _JLL_ASSET_CHANNELS.items():
        evidence = channels.get(channel)
        mapped = normalized_assets.get(channel)
        if not isinstance(evidence, Mapping) or not isinstance(mapped, list):
            return False
        raw_present = channel in property_value
        candidates = (
            _jll_asset_values(property_value.get(channel)) if raw_present else []
        )
        valid = [url for item in candidates if (url := _public_asset_url(item))]
        rejected = len(candidates) - len(valid)
        expected_presence = "present" if valid else "empty" if raw_present else "absent"
        normalized_urls = [url for item in mapped if (url := _public_asset_url(item))]
        if len(normalized_urls) != len(mapped):
            return False
        expected = {
            "source_path": source_path,
            "presence": expected_presence,
            "raw_valid_url_set_sha256": _url_set_hash(valid),
            "accepted_public_url_set_sha256": _url_set_hash(valid),
            "normalized_mapped_url_set_sha256": _url_set_hash(normalized_urls),
            "rejected_invalid_count": rejected,
        }
        if dict(evidence) != expected or set(valid) != set(normalized_urls):
            return False
    return True


def _jll_raw_detail_identity(
    raw: Mapping[str, Any],
    *,
    provider_id: str,
    canonical_url: str,
    source: Mapping[str, Any],
) -> tuple[str, str]:
    """Bind a JLL detail body to the enumerated provider identity and target."""
    property_value = _jll_next_property(raw)
    if property_value is None:
        raise MultisourceError("raw JLL property identity is absent")
    raw_provider_id = property_value.get("id")
    if not isinstance(raw_provider_id, str) or raw_provider_id != provider_id:
        raise MultisourceError(
            "raw JLL property id does not match enumerated provider id"
        )
    raw_page_url = property_value.get("pageUrl")
    raw_canonical_url = _canonical_jll_listing_url(raw_page_url, source)
    if raw_canonical_url != canonical_url:
        raise MultisourceError(
            "raw JLL property page URL does not match enumerated canonical target"
        )
    return raw_provider_id, raw_canonical_url


def _verified_jll_locator_fidelity(
    normalized: Mapping[str, Any],
    locators: Mapping[str, Any],
    assets: Mapping[str, Any],
    raw: Mapping[str, Any],
) -> bool:
    """Verify fixed JLL semantic and native-asset source contracts."""
    fields = normalized.get("fields")
    locator_fields = locators.get("fields")
    property_value = _jll_next_property(raw)
    if (
        not isinstance(fields, Mapping)
        or not isinstance(locator_fields, Mapping)
        or set(locator_fields) != set(_JLL_SEMANTIC_FIELDS)
        or property_value is None
    ):
        return False
    present_fields = set(fields)
    if not present_fields.issubset(_JLL_SEMANTIC_FIELDS):
        return False
    if not _JLL_REQUIRED_SEMANTIC_FIELDS.issubset(present_fields):
        return False
    # Both display name and street address are required to make a detail row a
    # usable property identity.  A name-only search-card remains screening
    # evidence, never a strict experiment member.
    if not {"address", "name"}.issubset(present_fields):
        return False
    for normalized_field in _JLL_SEMANTIC_FIELDS:
        locator = locator_fields.get(normalized_field)
        source_field = _jll_property_source_field(property_value, normalized_field)
        expected_presence = _jll_field_presence(property_value, normalized_field)
        normalized_value = fields.get(normalized_field)
        expected_locator = {"presence": expected_presence}
        if source_field is not None:
            expected_locator.update(
                {
                    "source_path": source_field[0],
                    "value_sha256": _sha256(_canonical(source_field[1])),
                }
            )
        if (
            not isinstance(locator, Mapping)
            or dict(locator) != expected_locator
            or (source_field is None and normalized_field in present_fields)
            or (source_field is not None and normalized_field not in present_fields)
            or (source_field is not None and source_field[1] != normalized_value)
        ):
            return False
    return _jll_asset_contract(property_value, normalized, assets)


def _fidelity_evidence(
    receipt: Mapping[str, Any],
    source: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    root: Path,
) -> tuple[str, str, str, bool, Mapping[str, Any]]:
    """Bind field and asset fidelity to real normalized artifacts, never booleans."""
    values: dict[str, tuple[int, Any]] = {
        "normalized": (MAX_RAW_RECEIPT_BYTES, None),
        "field_locator": (MAX_RAW_RECEIPT_BYTES, None),
        "asset_evidence": (MAX_RAW_RECEIPT_BYTES, None),
    }
    documents: dict[str, Mapping[str, Any]] = {}
    hashes: dict[str, str] = {}
    for name, (maximum, _) in values.items():
        path_value = receipt.get(f"{name}_path")
        if not isinstance(path_value, str):
            raise MultisourceError(f"{name} path is invalid")
        document, actual = _read_private_json(Path(path_value), maximum, root=root)
        expected = _hex_digest(receipt.get(f"{name}_sha256"), label=name)
        if actual != expected:
            raise MultisourceError(f"{name} hash drifted")
        if not isinstance(document, Mapping):
            raise MultisourceError(f"{name} evidence is malformed")
        documents[name] = document
        hashes[name] = actual
    provider_id = receipt["provider_id"]
    canonical_url = _canonical_target_url(receipt["canonical_url"], source)
    normalized = documents["normalized"]
    locators = documents["field_locator"]
    assets = documents["asset_evidence"]
    fields = normalized.get("fields")
    locator_fields = locators.get("fields")
    if (
        normalized.get("provider_id") != provider_id
        or normalized.get("canonical_url") != canonical_url
        or not isinstance(fields, Mapping)
        or locators.get("provider_id") != provider_id
        or not isinstance(locator_fields, Mapping)
        or assets.get("provider_id") != provider_id
    ):
        raise MultisourceError("artifact-bound fidelity evidence is incomplete")
    # v1 intentionally does not fabricate a generic semantic parser. Sources
    # without a reviewed verifier remain valuable screening evidence but cannot
    # become an experiment-ready cohort member.
    verified = source["key"] == "jll" and _verified_jll_locator_fidelity(
        normalized, locators, assets, raw
    )
    return (
        hashes["normalized"],
        hashes["field_locator"],
        hashes["asset_evidence"],
        verified,
        normalized,
    )


def _page_weight_band(total_population: int) -> str:
    """Use one closed, reviewable population rule for the sealed page band."""
    if type(total_population) is not int or total_population < 1:
        raise MultisourceError("enumeration population is invalid for stratification")
    if total_population <= 50:
        return "small"
    if total_population <= 500:
        return "medium"
    return "large"


def _closed_jll_stratum_value(value: Any, vocabulary: Mapping[str, str]) -> str:
    if not isinstance(value, str):
        return "other_verified"
    return vocabulary.get(" ".join(value.casefold().split()), "other_verified")


def _evidence_derived_stratum(
    source: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    population_total: int | None,
    population_verified: bool,
    fidelity_verified: bool,
) -> dict[str, str]:
    """Never trust caller strata; expose only a closed evidence-derived tuple."""
    if not population_verified:
        return {
            "transaction_class": "unverified",
            "property_type": "unverified",
            "page_weight_band": "unverified",
        }
    if population_total is None:
        raise MultisourceError("verified enumeration population is missing")
    page_weight_band = _page_weight_band(population_total)
    fields = normalized.get("fields")
    if (
        source["key"] != "jll"
        or not fidelity_verified
        or not isinstance(fields, Mapping)
    ):
        return {
            "transaction_class": "unverified",
            "property_type": "unverified",
            "page_weight_band": page_weight_band,
        }
    return {
        "transaction_class": _closed_jll_stratum_value(
            fields.get("transaction_type"), _JLL_TRANSACTION_CLASSES
        ),
        "property_type": _closed_jll_stratum_value(
            fields.get("property_type"), _JLL_PROPERTY_TYPES
        ),
        "page_weight_band": page_weight_band,
    }


def _receipt_summary(
    receipt: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    root: Path,
    now_utc: datetime,
    maximum_age: int,
) -> dict[str, Any]:
    required = {
        "source_key",
        "provider_id",
        "enumeration_identity_sha256",
        "enumeration_observed_at",
        "enumeration_receipt_path",
        "enumeration_receipt_sha256",
        "enumeration_body_sha256",
        "enumeration_total",
        "canonical_url",
        "final_url",
        "http_status",
        "content_type",
        "redacted_headers",
        "timing_ms",
        "retry_count",
        "raw_receipt_path",
        "raw_receipt_sha256",
        "request_url",
        "observed_at",
        "extractor_receipt_path",
        "extractor_receipt_sha256",
        "normalized_path",
        "normalized_sha256",
        "field_locator_path",
        "field_locator_sha256",
        "asset_evidence_path",
        "asset_evidence_sha256",
        "parser_sha256",
        "config_sha256",
        "source_config_sha256",
        "classification",
    }
    allowed = required | {"not_found_classifier"}
    if set(receipt) - allowed or not required <= set(receipt):
        raise MultisourceError("receipt has unsupported or missing fields")
    if receipt.get("source_key") != source["key"]:
        raise MultisourceError("receipt is assigned to the wrong source")
    if receipt.get("classification") not in CLASSIFICATIONS:
        raise MultisourceError("receipt classification is invalid")
    if (
        not isinstance(receipt.get("provider_id"), str)
        or not receipt["provider_id"].strip()
    ):
        raise MultisourceError("receipt lacks fresh enumeration provider identity")
    if receipt.get("source_config_sha256") != _source_config_sha256(source):
        raise MultisourceError("receipt source configuration digest is not this cohort")
    (
        identity,
        population_total,
        enumeration_receipt_sha256,
        population_verified,
        resolution_targets,
    ) = _enumeration_binding(
        receipt,
        source,
        root=root,
        now_utc=now_utc,
        maximum_age=maximum_age,
    )
    canonical_url = _canonical_target_url(receipt["canonical_url"], source)
    if source["key"] == "jll" and (
        resolution_targets is None
        or resolution_targets.get(receipt["provider_id"]) != canonical_url
    ):
        raise MultisourceError("receipt is not bound to a resolved JLL detail identity")
    _public_url(receipt.get("request_url"), source)
    _public_url(receipt.get("final_url"), source)
    _observed_at(receipt.get("observed_at"), now_utc=now_utc, maximum_age=maximum_age)
    if (
        type(receipt.get("http_status")) is not int
        or not 100 <= receipt["http_status"] <= 599
    ):
        raise MultisourceError("receipt HTTP status is invalid")
    if not isinstance(receipt.get("content_type"), str) or not receipt["content_type"]:
        raise MultisourceError("receipt transport metadata is invalid")
    _safe_redacted_headers(receipt["redacted_headers"])
    if (
        not _finite_nonnegative_timing(receipt.get("timing_ms"))
        or type(receipt.get("retry_count")) is not int
        or receipt["retry_count"] < 0
    ):
        raise MultisourceError("receipt timing is invalid")
    for key in ("raw_receipt_sha256", "parser_sha256", "config_sha256"):
        _hex_digest(receipt.get(key), label=key)
    _extractor_receipt_binding(receipt, root=root)
    raw_document, raw_hash, raw_body_sha256 = _raw_receipt_binding(
        receipt, source, root=root
    )
    if _canonical_target_url(raw_document["final_url"], source) != canonical_url:
        raise MultisourceError("raw receipt final target is not the enumerated listing")
    (
        normalized_sha256,
        field_locator_sha256,
        asset_evidence_sha256,
        fidelity_verified,
        normalized,
    ) = _fidelity_evidence(receipt, source, raw_document, root=root)
    classification = receipt["classification"]
    raw_classification = _raw_challenge_classification(raw_document)
    if raw_classification is not None and classification != raw_classification:
        raise MultisourceError("receipt classification conflicts with raw evidence")
    if classification == "challenge_or_throttle" and raw_classification is None:
        raise MultisourceError("challenge classification lacks raw evidence")
    if classification == "confirmed_current_attrition":
        classifier = source.get("not_found_classifier")
        if classifier != "jll_next_data_404_no_property" or not _valid_jll_not_found(
            raw_document, receipt
        ):
            raise MultisourceError("provider-specific attrition proof is absent")
    elif classification == "eligible_detail" and receipt["http_status"] != 200:
        raise MultisourceError("eligible detail receipt must be HTTP 200")
    raw_detail_identity = None
    if source["key"] == "jll" and classification == "eligible_detail":
        raw_detail_identity = _jll_raw_detail_identity(
            raw_document,
            provider_id=receipt["provider_id"],
            canonical_url=canonical_url,
            source=source,
        )
    return {
        "provider_id": receipt["provider_id"],
        "enumeration_identity_sha256": identity,
        "enumeration_receipt_sha256": enumeration_receipt_sha256,
        "enumeration_total": population_total,
        "enumeration_population_verified": population_verified,
        "canonical_url": canonical_url,
        "http_status": receipt["http_status"],
        "timing_ms": receipt["timing_ms"],
        "retry_count": receipt["retry_count"],
        "classification": classification,
        "raw_receipt_sha256": raw_hash,
        "raw_body_sha256": raw_body_sha256,
        "raw_detail_identity": raw_detail_identity,
        "normalized_sha256": normalized_sha256,
        "field_locator_sha256": field_locator_sha256,
        "asset_evidence_sha256": asset_evidence_sha256,
        "fidelity_verified": fidelity_verified,
        "parser_sha256": receipt["parser_sha256"],
        "config_sha256": receipt["config_sha256"],
        "stratum": _evidence_derived_stratum(
            source,
            normalized,
            population_total=population_total,
            population_verified=population_verified,
            fidelity_verified=fidelity_verified,
        ),
    }


def _ranked_stratified(
    rows: list[dict[str, Any]], *, count: int, seed: str
) -> list[dict[str, Any]]:
    """Deterministic round-robin strata selection, rather than lexical slicing."""
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row["stratum"][field] for field in STRATUM_FIELDS)
        buckets.setdefault(key, []).append(row)
    for key, values in buckets.items():
        values.sort(
            key=lambda row: _sha256(
                _canonical(
                    {
                        "seed": seed,
                        "stratum": key,
                        "provider_id": row["provider_id"],
                        "enumeration_identity_sha256": row[
                            "enumeration_identity_sha256"
                        ],
                    }
                )
            )
        )
    result: list[dict[str, Any]] = []
    indexes = {key: 0 for key in buckets}
    while len(result) < count:
        added = False
        for key in sorted(buckets):
            index = indexes[key]
            if index < len(buckets[key]) and len(result) < count:
                result.append(buckets[key][index])
                indexes[key] += 1
                added = True
        if not added:
            return result
    return result


def _safe_member(row: Mapping[str, Any]) -> dict[str, Any]:
    """The public cohort manifest never carries restricted paths, URLs, or headers."""
    return {
        key: row[key]
        for key in (
            "provider_id",
            "enumeration_identity_sha256",
            "raw_receipt_sha256",
            "normalized_sha256",
            "field_locator_sha256",
            "asset_evidence_sha256",
            "parser_sha256",
            "fidelity_verified",
            "stratum",
        )
    }


def _positive_throughput(row_count: int, timing_total: float) -> float | None:
    if row_count < 1 or not _finite_positive_timing(timing_total):
        return None
    rate = row_count * 60_000 / timing_total
    return rate if math.isfinite(rate) and rate > 0 else None


def prevalidate_cohort(
    receipts: Mapping[str, Any],
    *,
    config_path: Path = CONFIG,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Build a fixed, immutable multisource cohort from rehashed private receipts."""
    config = load_config(config_path)
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise MultisourceError("admission clock must be UTC-aware")
    now_utc = now_utc.astimezone(timezone.utc)
    if (
        receipts.get("schema_version") != SCHEMA_VERSION
        or receipts.get("kind") != "cre_capacity_multisource_v1_receipts"
        or not isinstance(receipts.get("receipt_root"), str)
        or not isinstance(receipts.get("receipts"), list)
        or set(receipts) != {"schema_version", "kind", "receipt_root", "receipts"}
    ):
        raise MultisourceError("multisource-v1 receipts are invalid")
    root = _private_root(Path(receipts["receipt_root"]))
    by_source = {source["key"]: source for source in config["sources"]}
    summaries: dict[str, list[dict[str, Any]]] = {key: [] for key in by_source}
    config_sha256 = _sha256(_canonical(config))
    seen_provider_ids: set[tuple[str, str]] = set()
    seen_canonical_targets: set[tuple[str, str]] = set()
    seen_raw_detail_ids: set[tuple[str, str]] = set()
    seen_raw_detail_targets: set[tuple[str, str]] = set()
    seen_raw_detail_receipts: set[tuple[str, str]] = set()
    seen_raw_detail_bodies: set[tuple[str, str]] = set()
    for item in receipts["receipts"]:
        if not isinstance(item, dict) or item.get("source_key") not in by_source:
            raise MultisourceError("receipt source is not in the fixed matrix")
        summary = _receipt_summary(
            item,
            by_source[item["source_key"]],
            root=root,
            now_utc=now_utc,
            maximum_age=config["sampling"]["maximum_enumeration_age_seconds"],
        )
        if summary["config_sha256"] != config_sha256:
            raise MultisourceError("receipt configuration digest is not this cohort")
        provider_identity = (item["source_key"], summary["provider_id"])
        if provider_identity in seen_provider_ids:
            raise MultisourceError("fresh enumeration identity is duplicated")
        seen_provider_ids.add(provider_identity)
        target_identity = (item["source_key"], summary["canonical_url"])
        if target_identity in seen_canonical_targets:
            raise MultisourceError("enumerated canonical target is duplicated")
        seen_canonical_targets.add(target_identity)
        raw_detail_identity = summary["raw_detail_identity"]
        if raw_detail_identity is not None:
            raw_provider_id, raw_canonical_url = raw_detail_identity
            raw_provider_identity = (item["source_key"], raw_provider_id)
            raw_target_identity = (item["source_key"], raw_canonical_url)
            raw_receipt_identity = (item["source_key"], summary["raw_receipt_sha256"])
            raw_body_identity = (item["source_key"], summary["raw_body_sha256"])
            if raw_provider_identity in seen_raw_detail_ids:
                raise MultisourceError("raw JLL property identity is replayed")
            if raw_target_identity in seen_raw_detail_targets:
                raise MultisourceError("raw JLL property target is replayed")
            if raw_receipt_identity in seen_raw_detail_receipts:
                raise MultisourceError("raw JLL detail receipt is replayed")
            if raw_body_identity in seen_raw_detail_bodies:
                raise MultisourceError("raw JLL detail body is replayed")
            seen_raw_detail_ids.add(raw_provider_identity)
            seen_raw_detail_targets.add(raw_target_identity)
            seen_raw_detail_receipts.add(raw_receipt_identity)
            seen_raw_detail_bodies.add(raw_body_identity)
        summaries[item["source_key"]].append(summary)
    sampling = config["sampling"]
    cohort_sources = []
    challenged_families = {
        by_source[key]["provider_family"]
        for key, rows in summaries.items()
        if any(row["classification"] == "challenge_or_throttle" for row in rows)
    }
    for key, source in by_source.items():
        rows = summaries[key]
        totals = {row["enumeration_total"] for row in rows}
        enumeration_hashes = {row["enumeration_receipt_sha256"] for row in rows}
        if len(totals) > 1 or len(enumeration_hashes) > 1:
            raise MultisourceError("source rows do not share one fresh enumeration")
        if rows and len(rows) > next(iter(totals)):
            raise MultisourceError("source rows exceed fresh enumeration population")
        population_verified = bool(rows) and all(
            row["enumeration_population_verified"] is True for row in rows
        )
        eligible = [row for row in rows if row["classification"] == "eligible_detail"]
        screening_qualified = [row for row in eligible if row["retry_count"] == 0]
        qualified = [
            row for row in screening_qualified if row["fidelity_verified"] is True
        ]
        attrition = [
            row
            for row in rows
            if row["classification"] == "confirmed_current_attrition"
        ]
        population_total = next(iter(totals)) if totals else 0
        verified_population_total = population_total if population_verified else None
        selected_calibration = _ranked_stratified(
            screening_qualified,
            count=sampling["calibration_per_source"],
            seed=sampling["stratification_seed"],
        )
        selected_core = _ranked_stratified(
            qualified,
            count=sampling["core_per_source"] if population_verified else 0,
            seed=sampling["stratification_seed"],
        )
        timing_values = sorted(row["timing_ms"] for row in rows)
        timing_total = sum(timing_values)
        row_count = len(rows)
        parser_failures = sum(row["classification"] == "parser_failure" for row in rows)
        transport_failures = sum(
            row["classification"] == "transport_failure" for row in rows
        )
        # Artifact hashes alone are only integrity evidence.  A row becomes
        # qualified only when the source-specific verifier tied its normalized
        # field values to raw receipt bytes and explicit locators.
        fidelity_failures = len(eligible) - sum(
            row["fidelity_verified"] is True for row in eligible
        )
        core_target_rows = (
            min(sampling["core_per_source"], population_total)
            if population_verified
            else 0
        )
        qualified_rate = _positive_throughput(len(qualified), timing_total)
        eligible_rate = _positive_throughput(len(eligible), timing_total)
        if rows and not all(_finite_positive_timing(row["timing_ms"]) for row in rows):
            core_state = "invalid_measurement_timing"
        elif source["provider_family"] in challenged_families:
            core_state = "challenge_or_throttle_in_family"
        elif not population_verified:
            core_state = "enumeration_population_unverified"
        elif fidelity_failures:
            core_state = "semantic_fidelity_unverified"
        elif len(qualified) < sampling["minimum_detail_eligible_for_core"]:
            core_state = "insufficient_current_detail_eligibility"
        elif len(selected_core) < core_target_rows:
            core_state = "core_sample_underfilled"
        elif qualified_rate is None:
            core_state = "invalid_measurement_timing"
        else:
            core_state = "ready"
        cohort_sources.append(
            {
                "source_key": key,
                "plane": source["plane"],
                "provider_family": source["provider_family"],
                "exclusive": source.get("exclusive", False),
                "calibration": [_safe_member(row) for row in selected_calibration],
                "core": [_safe_member(row) for row in selected_core]
                if core_state == "ready"
                else [],
                "core_state": core_state,
                "core_target_rows": core_target_rows,
                "core_selected_rows": len(selected_core),
                "fresh_enumeration": {
                    "total_population": verified_population_total,
                    "population_state": (
                        "verified" if population_verified else "unverified"
                    ),
                    "receipt_sha256": next(iter(enumeration_hashes))
                    if enumeration_hashes
                    else None,
                    "complete": population_verified,
                },
                "outcomes": dict(
                    sorted(Counter(row["classification"] for row in rows).items())
                ),
                "http_outcomes": dict(
                    sorted(Counter(str(row["http_status"]) for row in rows).items())
                ),
                "confirmed_current_attrition": len(attrition),
                "current_active_successes": len(eligible),
                "individually_qualified_rows": len(qualified),
                "parser_failures": parser_failures,
                "transport_failures": transport_failures,
                "fidelity_failures": fidelity_failures,
                "semantic_fidelity_verified_rows": len(eligible) - fidelity_failures,
                "row_rates": {
                    "current_active_successes": round(len(eligible) / row_count, 6)
                    if row_count
                    else None,
                    "confirmed_current_attrition": round(len(attrition) / row_count, 6)
                    if row_count
                    else None,
                    "individually_qualified_rows": round(len(qualified) / row_count, 6)
                    if row_count
                    else None,
                    "parser_failures": round(parser_failures / row_count, 6)
                    if row_count
                    else None,
                    "transport_failures": round(transport_failures / row_count, 6)
                    if row_count
                    else None,
                    "fidelity_failures": round(fidelity_failures / row_count, 6)
                    if row_count
                    else None,
                },
                "challenge_or_throttle": sum(
                    row["classification"] == "challenge_or_throttle" for row in rows
                ),
                "retry_count": sum(row["retry_count"] for row in rows),
                "latency_ms": {
                    "p50": timing_values[len(timing_values) // 2]
                    if timing_values
                    else None,
                    "p95": timing_values[
                        min(len(timing_values) - 1, int(len(timing_values) * 0.95))
                    ]
                    if timing_values
                    else None,
                },
                "eligible_detail_rows_per_minute": round(eligible_rate, 3)
                if eligible_rate is not None
                else None,
                "individually_qualified_rows_per_minute": round(qualified_rate, 3)
                if qualified_rate is not None
                else None,
                "structured_field_evidence_bound": len(eligible) - fidelity_failures,
                "asset_evidence_bound": len(eligible) - fidelity_failures,
                "raw_receipts_retained": len(rows),
            }
        )
    ready = [item for item in cohort_sources if item["core_state"] == "ready"]
    plane_results: dict[str, dict[str, Any]] = {}
    expected_plane_sizes = {
        plane: sum(source["plane"] == plane for source in config["sources"])
        for plane in PLANES
    }
    for plane in sorted(PLANES):
        plane_ready = [item for item in ready if item["plane"] == plane]
        if any(
            not _finite_positive_timing(item["individually_qualified_rows_per_minute"])
            for item in plane_ready
        ):
            raise MultisourceError(
                "ready source lacks a finite primary throughput rate"
            )
        if any(
            item["fresh_enumeration"]["population_state"] != "verified"
            or type(item["fresh_enumeration"]["total_population"]) is not int
            for item in plane_ready
        ):
            raise MultisourceError(
                "ready source lacks a verified enumeration population"
            )
        plane_rates = [
            item["individually_qualified_rows_per_minute"] for item in plane_ready
        ]
        populations = sorted(
            item["fresh_enumeration"]["total_population"] for item in plane_ready
        )
        if populations:
            percentile_index = min(
                len(populations) - 1,
                max(
                    0,
                    int(
                        (len(populations) - 1)
                        * config["workload_weighting"]["winsorize_percentile"]
                    ),
                ),
            )
            workload_cap = populations[percentile_index]
        else:
            workload_cap = 0
        plane_weights = [
            (
                item["individually_qualified_rows_per_minute"],
                min(item["fresh_enumeration"]["total_population"], workload_cap),
            )
            for item in plane_ready
        ]
        plane_results[plane] = {
            "sources_in_matrix": expected_plane_sizes[plane],
            "sources_core_ready": len(plane_ready),
            "source_keys": [
                item["source_key"] for item in cohort_sources if item["plane"] == plane
            ],
            "estimand": "individually_qualified_rows_per_minute",
            "equal_source_individually_qualified_rows_per_minute": round(
                sum(plane_rates) / len(plane_rates), 3
            )
            if plane_rates
            else None,
            "workload_weighted_individually_qualified_rows_per_minute": round(
                sum(rate * weight for rate, weight in plane_weights)
                / sum(weight for _, weight in plane_weights),
                3,
            )
            if plane_weights and sum(weight for _, weight in plane_weights)
            else None,
            "workload_weighting": {
                "basis": config["workload_weighting"]["basis"],
                "winsorized_population_cap": workload_cap or None,
                "population_state": "verified" if plane_ready else "unverified",
            },
        }
    complete_matrix = (
        len(ready) == sampling["minimum_sources_for_experiment"]
        and len(cohort_sources) == sampling["minimum_sources_for_experiment"]
        and all(
            plane_results[plane]["sources_core_ready"] == expected_plane_sizes[plane]
            for plane in PLANES
        )
    )
    aggregate = {
        "sources_in_matrix": len(cohort_sources),
        "sources_core_ready": len(ready),
        "state": "ready_for_review" if complete_matrix else "incomplete_screen",
        "aggregation_scope": "per_plane_only",
        "cross_plane_aggregation": "not_computed_distinct_plane_estimands",
        "primary_aggregation": "per_plane_equal_source_individually_qualified_rows_per_minute",
        "secondary_aggregation": "per_plane_workload_weighted_individually_qualified_rows_per_minute",
    }
    # The hash seals every non-sensitive review datum: source outcomes,
    # deterministic calibration/core selections and strata, plane results, and
    # the aggregate state. It intentionally excludes restricted receipt paths,
    # raw URLs, headers, and bodies.
    cohort_sha256 = _sha256(
        _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "config_sha256": config_sha256,
                "sampling": sampling,
                "sources": cohort_sources,
                "planes": plane_results,
                "aggregate": aggregate,
            }
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_capacity_multisource_v1_cohort",
        "config_sha256": config_sha256,
        "cohort_sha256": cohort_sha256,
        "execution": "prevalidation_only_no_adapter_or_runtime_execution",
        "sampling": sampling,
        "sources": cohort_sources,
        "planes": plane_results,
        "aggregate": aggregate,
        "safety": {
            "database_writes": 0,
            "cache_writes": 0,
            "status_writes": 0,
            "scheduler_writes": 0,
            "model_or_ocr_changes": 0,
            "unsupported_attrition": "fail_closed",
            "admission_challenge_gate": "challenge_or_throttle_blocks_all_source_family_core",
            "future_execution_requirements": {
                "P2_provider_family_exclusions": config["profiles"]["P2"][
                    "future_executor_provider_family_exclusions"
                ],
                "challenge_handling": "must_stop_affected_family_without_stopping_other_sources",
            },
            "primary_measurement_retries": 0,
        },
    }


def produce_jll_enumeration_artifacts(
    *,
    receipt_root: Path,
    page_receipt_paths: list[Path],
    detail_receipt_paths: list[Path],
    aggregate_path: Path,
) -> dict[str, Any]:
    """Seal existing private JLL captures into aggregate and resolution receipts.

    This is deliberately an offline producer: callers supply already-captured
    native GraphQL page and detail-page artifacts. It performs no transport and
    writes only the requested aggregate plus its sibling resolution receipts.
    The admission verifier remains the authority that validates the full schema.
    """
    root = _private_root(receipt_root)
    if not aggregate_path.is_absolute():
        raise MultisourceError("aggregate output must be absolute")
    try:
        output_parent = aggregate_path.parent.resolve(strict=True)
        aggregate_path.lstat()
    except FileNotFoundError:
        output_parent = aggregate_path.parent.resolve(strict=True)
    except OSError as exc:
        raise MultisourceError("aggregate output is unavailable") from exc
    else:
        raise MultisourceError("aggregate output must not overwrite an existing file")
    if output_parent != root or aggregate_path.parent != output_parent:
        raise MultisourceError("aggregate output must be directly inside receipt root")
    if not page_receipt_paths or not detail_receipt_paths:
        raise MultisourceError("JLL producer requires page and detail artifacts")
    source = {"key": "jll", "hosts": ["property.jll.com"]}
    page_manifests: list[dict[str, str]] = []
    search_targets: dict[str, str] = {}
    observed: list[datetime] = []
    pages_by_filter: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
    for path in page_receipt_paths:
        page, page_hash = _read_private_json(path, MAX_RAW_RECEIPT_BYTES, root=root)
        required = {
            "kind",
            "request_url",
            "final_url",
            "http_status",
            "content_type",
            "observed_at",
            "timing_ms",
            "operation_name",
            "variables",
            "request_body",
            "query_sha256",
            "body",
        }
        if (
            not isinstance(page, Mapping)
            or set(page) != required
            or page.get("kind") != "jll_graphql_page_receipt_v1"
            or not _valid_jll_graphql_url(page.get("request_url"), source)
            or page.get("final_url") != page.get("request_url")
            or page.get("http_status") != 200
            or not isinstance(page.get("content_type"), str)
            or "application/json" not in page["content_type"].casefold()
            or page.get("operation_name") != "SearchResults"
            or page.get("query_sha256") != _JLL_SEARCH_RESULTS_QUERY_SHA256
            or not isinstance(page.get("request_body"), str)
            or not isinstance(page.get("body"), str)
        ):
            raise MultisourceError("JLL producer page artifact is malformed")
        try:
            request = json.loads(page["request_body"])
            payload = json.loads(page["body"])
            items = payload["data"]["properties"]["items"]
            count = payload["data"]["properties"]["count"]
            page_observed = _observed_at(
                page.get("observed_at"),
                now_utc=datetime.now(timezone.utc),
                maximum_age=365 * 24 * 60 * 60,
            )
        except (KeyError, TypeError, MultisourceError, json.JSONDecodeError) as exc:
            raise MultisourceError("JLL producer page artifact is malformed") from exc
        variables = page.get("variables")
        if (
            not isinstance(items, list)
            or type(count) is not int
            or count < 0
            or not isinstance(request, Mapping)
            or request.get("operationName") != "SearchResults"
            or request.get("variables") != variables
            or not isinstance(request.get("query"), str)
            or _sha256(request["query"].encode()) != _JLL_SEARCH_RESULTS_QUERY_SHA256
            or not isinstance(variables, Mapping)
            or set(variables)
            != {
                "market",
                "language",
                "propertyTypes",
                "tenureTypes",
                "skip",
                "take",
                "orderBy",
            }
            or variables.get("market") != "us"
            or variables.get("language") != "en"
            or not isinstance(variables.get("propertyTypes"), list)
            or len(variables["propertyTypes"]) != 1
            or not isinstance(variables["propertyTypes"][0], str)
            or not isinstance(variables.get("tenureTypes"), list)
            or len(variables["tenureTypes"]) != 1
            or variables["tenureTypes"][0] not in _JLL_SUPPORTED_TENURES
            or type(variables.get("skip")) is not int
            or variables["skip"] < 0
            or variables["skip"] % _JLL_PAGE_TAKE
            or variables.get("take") != _JLL_PAGE_TAKE
            or variables.get("orderBy") != _JLL_GRAPHQL_ORDER_BY
        ):
            raise MultisourceError("JLL producer page artifact is malformed")
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise MultisourceError("JLL producer search identity is malformed")
            target = _canonical_jll_listing_url(item.get("pageUrl"), source)
            existing = search_targets.get(item["id"])
            if existing is not None and existing != target:
                raise MultisourceError("JLL producer search identity is ambiguous")
            if target in search_targets.values() and existing != target:
                raise MultisourceError("JLL producer search target is ambiguous")
            search_targets[item["id"]] = target
        observed.append(
            datetime.fromisoformat(page_observed.removesuffix("Z") + "+00:00")
        )
        pages_by_filter.setdefault(
            (variables["propertyTypes"][0], variables["tenureTypes"][0]), []
        ).append((variables["skip"], count, len(items)))
        page_manifests.append({"path": str(path), "sha256": page_hash})
    if {
        property_type for property_type, _ in pages_by_filter
    } != _JLL_ENUMERATION_PROPERTY_TYPES or len(
        {tenure for _, tenure in pages_by_filter}
    ) != 1:
        raise MultisourceError("JLL producer page scope is incomplete")
    for pages in pages_by_filter.values():
        counts = {count for _, count, _ in pages}
        if len(counts) != 1:
            raise MultisourceError("JLL producer page counts disagree")
        count = next(iter(counts))
        if sorted(skip for skip, _, _ in pages) != list(range(0, count, 50)) or (
            count == 0 and sorted(skip for skip, _, _ in pages) != [0]
        ):
            raise MultisourceError("JLL producer page sequence is incomplete")
        if any(size != min(50, max(0, count - skip)) for skip, _, size in pages):
            raise MultisourceError("JLL producer page cardinality is invalid")
    detail_by_target: dict[str, tuple[Path, str, str]] = {}
    for path in detail_receipt_paths:
        detail, detail_hash = _read_private_json(path, MAX_RAW_RECEIPT_BYTES, root=root)
        if not isinstance(detail, Mapping) or not isinstance(detail.get("body"), str):
            raise MultisourceError("JLL producer detail artifact is malformed")
        property_value = _jll_next_property({"body": {"rawHtml": detail["body"]}})
        detail_id = property_value.get("id") if property_value else None
        detail_url = property_value.get("pageUrl") if property_value else None
        if not isinstance(detail_id, str) or re.fullmatch(r"[0-9]+", detail_id) is None:
            raise MultisourceError("JLL producer detail identity is malformed")
        target = _canonical_jll_listing_url(detail_url, source)
        if target in detail_by_target:
            raise MultisourceError("JLL producer detail target is duplicated")
        detail_by_target[target] = (path, detail_hash, detail_id)
    if set(detail_by_target) != set(search_targets.values()):
        raise MultisourceError("JLL producer has unresolved search targets")
    detail_ids = [detail_by_target[target][2] for target in search_targets.values()]
    if len(set(detail_ids)) != len(detail_ids):
        raise MultisourceError("JLL producer detail identities are duplicated")
    root_fd = _open_private_directory(root)
    stage: Path | None = None
    published: set[str] = set()
    try:
        stage = Path(tempfile.mkdtemp(prefix=".jll-produce-", dir=root))
        stage.chmod(0o700)
        final_resolution_paths = [
            root / f"{aggregate_path.stem}.resolution-{index}.json"
            for index in range(len(search_targets))
        ]
        if any(path.exists() or path.is_symlink() for path in final_resolution_paths):
            raise MultisourceError("JLL producer resolution output already exists")
        staged_manifests: list[dict[str, str]] = []
        final_manifests: list[dict[str, str]] = []
        for index, (search_id, target) in enumerate(sorted(search_targets.items())):
            detail_path, detail_hash, _ = detail_by_target[target]
            resolution_raw = _canonical(
                {
                    "kind": "jll_detail_resolution_receipt_v1",
                    "search_id": search_id,
                    "canonical_url": target,
                    "detail_receipt_path": str(detail_path),
                    "detail_receipt_sha256": detail_hash,
                }
            )
            staged_path = stage / final_resolution_paths[index].name
            _exclusive_private_write(staged_path, resolution_raw)
            digest = _sha256(resolution_raw)
            staged_manifests.append({"path": str(staged_path), "sha256": digest})
            final_manifests.append(
                {"path": str(final_resolution_paths[index]), "sha256": digest}
            )
        aggregate_base = {
            "kind": "jll_graphql_enumeration_aggregate_v1",
            "observed_at": max(observed)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "total": len(detail_ids),
            "complete": True,
            "truncated": False,
            "provider_ids": sorted(detail_ids, key=int),
            "page_receipts": page_manifests,
        }
        staged_aggregate = {**aggregate_base, "resolution_receipts": staged_manifests}
        if not isinstance(
            _verified_jll_enumeration_population(
                staged_aggregate,
                provider_ids=staged_aggregate["provider_ids"],
                total=staged_aggregate["total"],
                root=root,
                source=source,
                now_utc=datetime.now(timezone.utc),
                maximum_age=365 * 24 * 60 * 60,
            ),
            dict,
        ):
            raise MultisourceError("JLL producer output is not consumable")
        final_aggregate = {**aggregate_base, "resolution_receipts": final_manifests}
        aggregate_raw = _canonical(final_aggregate)
        staged_aggregate_path = stage / aggregate_path.name
        _exclusive_private_write(staged_aggregate_path, aggregate_raw)
        for staged_path, final_path in zip(
            [Path(item["path"]) for item in staged_manifests],
            final_resolution_paths,
            strict=True,
        ):
            _publish_staged_file(
                staged_path,
                final_path,
                root_fd=root_fd,
                published=published,
            )
        _publish_staged_file(
            staged_aggregate_path,
            aggregate_path,
            root_fd=root_fd,
            published=published,
        )
        _fsync_directory(root)
        if not isinstance(
            _verified_jll_enumeration_population(
                final_aggregate,
                provider_ids=final_aggregate["provider_ids"],
                total=final_aggregate["total"],
                root=root,
                source=source,
                now_utc=datetime.now(timezone.utc),
                maximum_age=365 * 24 * 60 * 60,
            ),
            dict,
        ):
            raise MultisourceError("published JLL producer output is not consumable")
        return {"path": str(aggregate_path), "sha256": _sha256(aggregate_raw)}
    except Exception as exc:
        if published:
            retained_entries = []
            for name in sorted(published):
                path = root / name
                try:
                    digest = _sha256(
                        _private_regular_bytes(path, MAX_RAW_RECEIPT_BYTES, root=root)
                    )
                except MultisourceError:
                    digest = "unavailable"
                retained_entries.append(f"{path} sha256={digest}")
            retained = ", ".join(retained_entries)
            raise MultisourceError(
                "producer publication failed; retained published outputs: " + retained
            ) from exc
        raise
    finally:
        try:
            if stage is not None:
                for path in stage.iterdir():
                    path.unlink()
                stage.rmdir()
        finally:
            os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(argv)
    try:
        manifest_root = _private_root(args.receipts.parent)
        receipts, _ = _read_private_json(
            args.receipts, MAX_RECEIPT_BYTES, root=manifest_root
        )
        if not isinstance(receipts, dict):
            raise MultisourceError("receipt root must be an object")
        print(
            json.dumps(
                prevalidate_cohort(receipts, config_path=args.config),
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except MultisourceError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
