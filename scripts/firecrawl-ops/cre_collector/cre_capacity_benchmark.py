#!/usr/bin/env python3
"""Prepare and run the bounded, no-database JLL capacity benchmark.

Dry-run planning is the default.  ``--prepare-sample`` builds an exact,
source-linked 128-detail manifest from an existing JLL detail cache without a
network or database call.  ``--run`` additionally requires a separately
reviewed technical-admission receipt and executes three fresh replicates
through the real JLL adapter and local Firecrawl scrape path.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import cre_capacity_experiment as experiment
import cre_capacity_runtime as capacity_runtime

SCHEMA_VERSION = 1
SAMPLE_KIND = "cre_jll_capacity_sample"
ADMISSION_KIND = "cre_capacity_runtime_admission"
RESULT_KIND = "cre_jll_capacity_benchmark"
MAX_SAMPLE_BYTES = 8 * 1024 * 1024
MAX_CACHE_RECORD_BYTES = 4 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 512 * 1024 * 1024
NEXT_DATA = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
WORKER_ENV_ALLOWLIST = frozenset(
    {"PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
)
ALLOWED_API_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class BenchmarkError(ValueError):
    """The benchmark cannot safely proceed."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_private_json(path: Path, value: Any) -> None:
    encoded = _canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not path.is_file():
        raise BenchmarkError(f"output is not a regular file: {path}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise BenchmarkError(f"short write for {path}")
            remaining = remaining[written:]
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _private_artifact_root(path: Path, repo_root: Path) -> Path:
    resolved = path.resolve()
    allowed = (repo_root / "tasks" / "tmp").resolve()
    if allowed not in resolved.parents or not resolved.name.startswith(
        "cre-capacity-benchmark-"
    ):
        raise BenchmarkError("artifact root must be tasks/tmp/cre-capacity-benchmark-*")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    if resolved.is_symlink() or not resolved.is_dir():
        raise BenchmarkError("artifact root must be a real directory")
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        raise BenchmarkError("artifact root must not be group/world accessible")
    return resolved


def _read_json(path: Path, maximum: int = MAX_SAMPLE_BYTES) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"cannot read {path}") from exc
    if not raw or len(raw) > maximum:
        raise BenchmarkError(f"invalid size for {path}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON in {path}") from exc


def _string_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            item
            for item in value
            if isinstance(item, str)
            and urllib.parse.urlsplit(item).scheme in {"http", "https"}
        }
    )


def _normalize_jll_url(value: str) -> str:
    if value.startswith("/"):
        value = f"https://property.jll.com{value}"
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "property.jll.com"
        or not parsed.path.startswith("/listings/")
    ):
        raise BenchmarkError("historic JLL cache row has a non-listing URL")
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", "property.jll.com", path, "", ""))


def _floor_plan_urls(value: Any) -> list[str]:
    rows: list[Any]
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = [
            item
            for key in ("images", "files")
            for item in (value.get(key) if isinstance(value.get(key), list) else [])
        ]
    else:
        rows = []
    urls: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            urls.update(_string_urls([row]))
        elif isinstance(row, dict):
            urls.update(_string_urls([row.get("url"), row.get("image")]))
    return sorted(urls)


def _native_evidence(property_value: Mapping[str, Any]) -> dict[str, Any]:
    channels = {
        "images": _string_urls(property_value.get("images")),
        "brochures": _string_urls(property_value.get("brochures")),
        "floor_plans": _floor_plan_urls(property_value.get("floorPlans")),
        "videos": _string_urls(property_value.get("videos")),
        "virtual_tours": _string_urls(
            property_value.get("virtualTours")
            if isinstance(property_value.get("virtualTours"), list)
            else [property_value.get("virtualTours")]
        ),
        "view_360": _string_urls(
            property_value.get("view360URLs")
            if isinstance(property_value.get("view360URLs"), list)
            else [property_value.get("view360URLs")]
        ),
    }
    return {
        "counts": {key: len(values) for key, values in channels.items()},
        "fingerprints": {
            key: _sha256("\n".join(values).encode()) for key, values in channels.items()
        },
        "shape": sorted(key for key, values in channels.items() if values),
    }


def _cache_candidate(path: Path) -> dict[str, Any] | None:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_CACHE_RECORD_BYTES:
        return None
    record = json.loads(raw)
    html = record.get("rawHtml")
    if not isinstance(html, str):
        return None
    match = NEXT_DATA.search(html)
    if not match:
        return None
    payload = json.loads(match.group(1))
    page_props = payload.get("props", {}).get("pageProps", {})
    property_value = page_props.get("property")
    if not isinstance(property_value, dict):
        return None
    property_id = property_value.get("id")
    url = property_value.get("pageUrl") or record.get("url")
    if not isinstance(url, str):
        return None
    url = _normalize_jll_url(url)
    if not isinstance(property_id, (str, int)) or isinstance(property_id, bool):
        return None
    tenures = property_value.get("tenureTypes")
    if isinstance(tenures, str):
        tenures = [tenures]
    normalized_tenures = sorted(
        {str(item).strip().lower() for item in tenures or [] if str(item).strip()}
    )
    supports_sale = "sale" in normalized_tenures
    supports_lease = "rent" in normalized_tenures or "lease" in normalized_tenures
    if not supports_sale and not supports_lease:
        return None
    transaction_class = (
        "sale_or_lease"
        if supports_sale and supports_lease
        else "sale"
        if supports_sale
        else "lease"
    )
    property_types = property_value.get("propertyTypes")
    if isinstance(property_types, str):
        property_types = [property_types]
    normalized_types = sorted(
        {
            str(item).strip().lower()
            for item in property_types or []
            if str(item).strip()
        }
    ) or ["unknown"]
    native = _native_evidence(property_value)
    return {
        "id": str(property_id),
        "url": url,
        "transaction_class": transaction_class,
        "supports_sale": supports_sale,
        "supports_lease": supports_lease,
        "property_types": normalized_types,
        "primary_property_type": normalized_types[0],
        "historic": {
            "cache_file": path.name,
            "cache_record_sha256": _sha256(raw),
            "raw_html_sha256": _sha256(html.encode()),
            "raw_html_bytes": len(html.encode()),
            "cached_at": record.get("cachedAt"),
            "detail_observed_at": record.get("detailObservedAt"),
            "native": native,
        },
    }


def _quantile(values: Sequence[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def build_sample(cache_dir: Path, details: int = 128) -> dict[str, Any]:
    """Build a deterministic, visibly stratified sample from prior raw evidence."""
    if details != 128:
        raise BenchmarkError("the admitted JLL benchmark requires exactly 128 details")
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for path in sorted(cache_dir.glob("*.json")):
        try:
            candidate = _cache_candidate(path)
        except (BenchmarkError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not candidate:
            continue
        if candidate["id"] in seen_ids or candidate["url"] in seen_urls:
            continue
        seen_ids.add(candidate["id"])
        seen_urls.add(candidate["url"])
        candidates.append(candidate)
    if len(candidates) < details:
        raise BenchmarkError("fewer than 128 unique parseable JLL cache rows")
    sizes = [row["historic"]["raw_html_bytes"] for row in candidates]
    light_cutoff, heavy_cutoff = _quantile(sizes, 0.25), _quantile(sizes, 0.75)
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        size = row["historic"]["raw_html_bytes"]
        band = (
            "light"
            if size <= light_cutoff
            else "heavy"
            if size >= heavy_cutoff
            else "medium"
        )
        row["page_weight_band"] = band
        key = (row["transaction_class"], row["primary_property_type"], band)
        buckets[key].append(row)
    for rows in buckets.values():
        rows.sort(key=lambda row: _sha256(f"{row['id']}\0{row['url']}".encode()))
    selected: list[dict[str, Any]] = []
    keys = sorted(buckets, key=lambda key: (len(buckets[key]), key))
    while len(selected) < details:
        advanced = False
        for key in keys:
            if buckets[key] and len(selected) < details:
                selected.append(buckets[key].pop(0))
                advanced = True
        if not advanced:
            break
    if len(selected) != details:
        raise BenchmarkError("could not construct an exact 128-row sample")
    transaction_counts = Counter(row["transaction_class"] for row in selected)
    type_counts = Counter(row["primary_property_type"] for row in selected)
    weight_counts = Counter(row["page_weight_band"] for row in selected)
    shape_counts = Counter(
        "+".join(row["historic"]["native"]["shape"]) or "none" for row in selected
    )
    if not any(row["supports_sale"] for row in selected) or not any(
        row["supports_lease"] for row in selected
    ):
        raise BenchmarkError("sample does not span sale and lease JLL listings")
    if len(type_counts) < 4 or not {"light", "heavy"}.issubset(weight_counts):
        raise BenchmarkError("sample lacks required property-type or page-weight span")
    rows = []
    for index, row in enumerate(selected):
        rows.append(
            {
                "sample_index": index,
                "sample_id": _sha256(f"{row['id']}\0{row['url']}".encode())[:24],
                **row,
            }
        )
    inventory_fingerprint = _sha256(
        _canonical([(row["id"], row["url"]) for row in rows])
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SAMPLE_KIND,
        "source": "jll",
        "created_at": _now(),
        "selection": "deterministic_round_robin_across_transaction_property_type_and_page_weight",
        "population": {
            "cache_directory": str(cache_dir.resolve()),
            "unique_parseable_rows": len(candidates),
            "light_cutoff_bytes": light_cutoff,
            "heavy_cutoff_bytes": heavy_cutoff,
        },
        "coverage": {
            "transactions": dict(sorted(transaction_counts.items())),
            "supports_sale": sum(row["supports_sale"] for row in rows),
            "supports_lease": sum(row["supports_lease"] for row in rows),
            "primary_property_types": dict(sorted(type_counts.items())),
            "page_weight_bands": dict(sorted(weight_counts.items())),
            "native_asset_shapes": dict(sorted(shape_counts.items())),
        },
        "inventory_sha256": inventory_fingerprint,
        "details": rows,
        "representation_claim": (
            "stratified capacity sample spanning observed transactions, property types, "
            "page weights, and native asset shapes; not a population-weighted estimate"
        ),
    }


def validate_sample(value: Any, expected_details: int = 128) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") != SAMPLE_KIND:
        raise BenchmarkError("sample manifest kind is invalid")
    if value.get("source") != "jll":
        raise BenchmarkError("benchmark sample must be JLL, never JLL Investor")
    rows = value.get("details")
    if not isinstance(rows, list) or len(rows) != expected_details:
        raise BenchmarkError(f"sample must contain exactly {expected_details} details")
    ids, urls = set(), set()
    transaction_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    weight_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    supports_sale = 0
    supports_lease = 0
    hash_pattern = re.compile(r"[0-9a-f]{64}\Z")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise BenchmarkError("sample row identity is invalid")
        if not isinstance(row.get("url"), str):
            raise BenchmarkError("sample row URL is invalid")
        try:
            normalized_url = _normalize_jll_url(row["url"])
        except BenchmarkError as exc:
            raise BenchmarkError("sample row URL is not a JLL listing") from exc
        if normalized_url != row["url"]:
            raise BenchmarkError("sample row URL is not canonical")
        expected_sample_id = _sha256(f"{row['id']}\0{row['url']}".encode())[:24]
        if (
            row.get("sample_index") != index
            or row.get("sample_id") != expected_sample_id
        ):
            raise BenchmarkError("sample row binding is invalid")
        transaction = row.get("transaction_class")
        if transaction not in {"sale", "lease", "sale_or_lease"}:
            raise BenchmarkError("sample transaction class is invalid")
        sale, lease = row.get("supports_sale"), row.get("supports_lease")
        if type(sale) is not bool or type(lease) is not bool or not (sale or lease):
            raise BenchmarkError("sample transaction support is invalid")
        if transaction != (
            "sale_or_lease" if sale and lease else "sale" if sale else "lease"
        ):
            raise BenchmarkError("sample transaction class is inconsistent")
        property_types = row.get("property_types")
        if (
            not isinstance(property_types, list)
            or not property_types
            or not all(isinstance(item, str) and item for item in property_types)
            or row.get("primary_property_type") != property_types[0]
        ):
            raise BenchmarkError("sample property types are invalid")
        weight = row.get("page_weight_band")
        if weight not in {"light", "medium", "heavy"}:
            raise BenchmarkError("sample page weight is invalid")
        historic = row.get("historic")
        native = historic.get("native") if isinstance(historic, dict) else None
        fingerprints = native.get("fingerprints") if isinstance(native, dict) else None
        counts = native.get("counts") if isinstance(native, dict) else None
        shape = native.get("shape") if isinstance(native, dict) else None
        cache_file = historic.get("cache_file") if isinstance(historic, dict) else None
        if (
            not isinstance(cache_file, str)
            or Path(cache_file).name != cache_file
            or not cache_file.endswith(".json")
            or not isinstance(historic.get("raw_html_bytes"), int)
            or historic["raw_html_bytes"] <= 0
            or not hash_pattern.fullmatch(str(historic.get("cache_record_sha256", "")))
            or not hash_pattern.fullmatch(str(historic.get("raw_html_sha256", "")))
            or not isinstance(fingerprints, dict)
            or not isinstance(counts, dict)
            or set(fingerprints) != set(counts)
            or not all(
                hash_pattern.fullmatch(str(item)) for item in fingerprints.values()
            )
            or not all(type(item) is int and item >= 0 for item in counts.values())
            or not isinstance(shape, list)
            or shape != sorted(key for key, count in counts.items() if count)
        ):
            raise BenchmarkError("sample historic provenance is invalid")
        ids.add(row["id"])
        urls.add(row["url"])
        transaction_counts[transaction] += 1
        type_counts[property_types[0]] += 1
        weight_counts[weight] += 1
        shape_counts["+".join(shape) or "none"] += 1
        supports_sale += int(sale)
        supports_lease += int(lease)
    if len(ids) != expected_details or len(urls) != expected_details:
        raise BenchmarkError("sample identities and URLs must be unique")
    expected = _sha256(_canonical([(row["id"], row["url"]) for row in rows]))
    if value.get("inventory_sha256") != expected:
        raise BenchmarkError("sample inventory fingerprint does not match")
    expected_coverage = {
        "transactions": dict(sorted(transaction_counts.items())),
        "supports_sale": supports_sale,
        "supports_lease": supports_lease,
        "primary_property_types": dict(sorted(type_counts.items())),
        "page_weight_bands": dict(sorted(weight_counts.items())),
        "native_asset_shapes": dict(sorted(shape_counts.items())),
    }
    if value.get("coverage") != expected_coverage:
        raise BenchmarkError("sample coverage does not match its rows")
    if (
        not supports_sale
        or not supports_lease
        or len(type_counts) < 4
        or not {"light", "heavy"}.issubset(weight_counts)
    ):
        raise BenchmarkError("sample lacks required JLL representation")
    return value


def verify_sample_provenance(sample: Mapping[str, Any]) -> dict[str, Any]:
    """Re-read every selected historic cache record before a live benchmark."""
    population = sample.get("population")
    cache_value = (
        population.get("cache_directory") if isinstance(population, dict) else None
    )
    declared_population = (
        population.get("unique_parseable_rows")
        if isinstance(population, dict)
        else None
    )
    if (
        not isinstance(cache_value, str)
        or not cache_value
        or type(declared_population) is not int
        or declared_population < len(sample["details"])
    ):
        raise BenchmarkError("sample preparation population is invalid")
    cache_dir = Path(cache_value)
    if cache_dir.is_symlink() or not cache_dir.is_absolute() or not cache_dir.is_dir():
        raise BenchmarkError("sample preparation cache is unavailable")
    cache_files = list(cache_dir.glob("*.json"))
    if len(cache_files) < declared_population:
        raise BenchmarkError("sample preparation population is no longer present")
    verified_files: set[str] = set()
    for row in sample["details"]:
        filename = row["historic"]["cache_file"]
        path = cache_dir / filename
        if filename in verified_files or path.is_symlink() or not path.is_file():
            raise BenchmarkError("sample historic cache record is unavailable")
        verified_files.add(filename)
        try:
            candidate = _cache_candidate(path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BenchmarkError("sample historic cache record is unreadable") from exc
        if candidate is None:
            raise BenchmarkError("sample historic cache record is not parseable JLL")
        for key in (
            "id",
            "url",
            "transaction_class",
            "supports_sale",
            "supports_lease",
            "property_types",
            "primary_property_type",
            "historic",
        ):
            if candidate[key] != row[key]:
                raise BenchmarkError("sample historic provenance does not match cache")
    return {
        "cache_directory": str(cache_dir),
        "verified_records": len(verified_files),
        "verified_at": _now(),
    }


def _loopback_url(value: str, label: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in ALLOWED_API_HOSTS:
        raise BenchmarkError(f"{label} must be a loopback HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BenchmarkError(f"{label} must not contain credentials or query data")
    return value.rstrip("/")


def validate_admission(
    value: Any,
    profile: Mapping[str, Any],
    profile_name: str,
    config_sha256: str,
    *,
    source_git_sha: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != ADMISSION_KIND
    ):
        raise BenchmarkError("technical admission receipt kind is invalid")
    if value.get("admitted") is not True:
        raise BenchmarkError("technical admission has not admitted execution")
    if (
        value.get("profile") != profile_name
        or value.get("config_sha256") != config_sha256
    ):
        raise BenchmarkError("technical admission is not bound to this profile/config")
    if value.get("source_git_sha") != source_git_sha:
        raise BenchmarkError("technical admission source SHA does not match HEAD")
    if value.get("writes") != "forbidden":
        raise BenchmarkError("technical admission does not forbid writes")
    if value.get("expires_after_seconds") != capacity_runtime.RECEIPT_MAX_AGE_SECONDS:
        raise BenchmarkError("technical admission expiry contract is invalid")
    try:
        created = datetime.fromisoformat(
            str(value.get("created_at")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise BenchmarkError("technical admission timestamp is invalid") from exc
    if created.tzinfo is None:
        raise BenchmarkError("technical admission timestamp is invalid")
    age = ((now or datetime.now(UTC)) - created.astimezone(UTC)).total_seconds()
    if age < 0 or age > capacity_runtime.RECEIPT_MAX_AGE_SECONDS:
        raise BenchmarkError("technical admission is stale")
    checks = value.get("checks")
    if (
        not isinstance(checks, dict)
        or not checks
        or not all(item is True for item in checks.values())
    ):
        raise BenchmarkError("technical admission checks are not all true")
    effective = value.get("effective")
    if not isinstance(effective, dict):
        raise BenchmarkError("technical admission effective state is missing")
    if effective.get("snapshot_sha256") != capacity_runtime.snapshot_fingerprint(
        effective
    ):
        raise BenchmarkError("technical admission snapshot fingerprint is invalid")
    if effective.get("transition_sha256") != capacity_runtime.transition_fingerprint(
        effective
    ):
        raise BenchmarkError("technical admission transition fingerprint is invalid")
    repo = effective.get("repo")
    if (
        not isinstance(repo, dict)
        or repo.get("git_sha") != source_git_sha
        or repo.get("dirty") is not False
    ):
        raise BenchmarkError("technical admission repository state is invalid")
    try:
        state_checks = capacity_runtime.evaluate_state(effective, profile, "candidate")
    except (KeyError, TypeError, capacity_runtime.RuntimeAdmissionError) as exc:
        raise BenchmarkError(
            "technical admission effective state is malformed"
        ) from exc
    state_checks.pop("collector_idle", None)
    if not state_checks or not all(state_checks.values()):
        raise BenchmarkError("technical admission effective state is not the candidate")
    result = dict(value)
    result["endpoints"] = {
        "api_url": "http://127.0.0.1:3102",
        "browser_health_url": "http://127.0.0.1:3103/health",
    }
    return result


def _git_head(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError("cannot read benchmark source SHA") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise BenchmarkError("cannot read benchmark source SHA")
    return value


def _other_collector_process_active() -> bool:
    """Ignore this benchmark's process tree, but fail on any peer collector."""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError("cannot verify collector process isolation") from exc
    if completed.returncode != 0:
        raise BenchmarkError("cannot verify collector process isolation")
    processes: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        processes[int(parts[0])] = (int(parts[1]), parts[2])
    ignored: set[int] = set()
    current = os.getpid()
    while current > 0 and current not in ignored:
        ignored.add(current)
        current = processes.get(current, (0, ""))[0]
    markers = (
        "cre_checkpoint_series",
        "cre_capacity_benchmark",
        "collect.ts",
        "cre_daily_update",
        "cre_checkpoint_refresh",
    )
    return any(
        pid not in ignored and any(marker in command for marker in markers)
        for pid, (_parent, command) in processes.items()
    )


def verify_live_admission(
    admission: Mapping[str, Any], profile: Mapping[str, Any]
) -> dict[str, Any]:
    """Re-observe the candidate immediately before any workload starts."""
    try:
        capture = capacity_runtime.capture_runtime()
    except capacity_runtime.RuntimeAdmissionError as exc:
        raise BenchmarkError("live technical admission capture failed") from exc
    public = capture.public
    if public.get("snapshot_sha256") != capacity_runtime.snapshot_fingerprint(public):
        raise BenchmarkError("live technical admission fingerprint is invalid")
    admitted = admission.get("effective")
    if not isinstance(admitted, Mapping):
        raise BenchmarkError("technical admission effective state is missing")
    if public.get("transition_sha256") != capacity_runtime.transition_fingerprint(
        public
    ):
        raise BenchmarkError(
            "live technical admission transition fingerprint is invalid"
        )
    if public.get("transition_sha256") != admitted.get("transition_sha256"):
        raise BenchmarkError("live transition state changed after admission")
    if _other_collector_process_active():
        raise BenchmarkError("another CRE collector process is active")
    settlement = public.get("settlement")
    if not isinstance(settlement, Mapping):
        raise BenchmarkError("live settlement state is missing")
    adjusted = dict(public)
    adjusted["settlement"] = dict(settlement)
    adjusted["settlement"]["cre_process_active"] = False
    try:
        checks = capacity_runtime.evaluate_state(adjusted, profile, "candidate")
    except (KeyError, TypeError, capacity_runtime.RuntimeAdmissionError) as exc:
        raise BenchmarkError("live technical admission state is malformed") from exc
    if not checks or not all(checks.values()):
        failed = sorted(key for key, value in checks.items() if not value)
        raise BenchmarkError(
            "live technical admission checks failed: " + ", ".join(failed)
        )
    return {
        "observed_at": _now(),
        "snapshot_sha256": public["snapshot_sha256"],
        "transition_sha256": public["transition_sha256"],
        "checks": checks,
        "other_collector_process_active": False,
    }


def scrub_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Build a minimal process environment with no inherited credentials."""
    return {
        key: value for key, value in environment.items() if key in WORKER_ENV_ALLOWLIST
    }


class _CpuInfo(ctypes.Structure):
    _fields_ = [("ticks", ctypes.c_uint32 * 4)]


def _cpu_ticks() -> tuple[int, ...]:
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_host_self.restype = ctypes.c_uint32
        info = _CpuInfo()
        count = ctypes.c_uint32(4)
        result = library.host_statistics(
            library.mach_host_self(), 3, ctypes.byref(info), ctypes.byref(count)
        )
        if result != 0 or count.value < 4:
            raise BenchmarkError("Darwin host CPU telemetry is unavailable")
        return tuple(int(value) for value in info.ticks)
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    return tuple(int(value) for value in fields)


def _cpu_percent(before: tuple[int, ...], after: tuple[int, ...]) -> float:
    if len(before) != len(after) or len(before) < 4:
        raise BenchmarkError("host CPU telemetry shape changed")
    delta = [max(0, right - left) for left, right in zip(before, after, strict=True)]
    total = sum(delta)
    if total <= 0:
        raise BenchmarkError("host CPU clock did not advance")
    idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
    return 100.0 * (total - idle) / total


def _http_json(url: str, maximum: int = 64 * 1024) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            raw = response.read(maximum + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise BenchmarkError(f"runtime evidence unavailable at {url}") from exc
    if len(raw) > maximum:
        raise BenchmarkError("runtime evidence response exceeds size bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchmarkError("runtime evidence response is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BenchmarkError("runtime evidence response must be an object")
    return value


def _settlement_snapshot(api_url: str, browser_url: str) -> dict[str, Any]:
    queue = _http_json(f"{api_url}/v2/team/queue-status")
    browser = _http_json(browser_url)
    active = queue.get("activeJobsInQueue")
    waiting = queue.get("waitingJobsInQueue")
    total = queue.get("jobsInQueue")
    browser_active = browser.get("activePages")
    values = [active, waiting, total, browser_active]
    if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
        raise BenchmarkError("queue/browser settlement counters are invalid")
    return {
        "queue": {"active": active, "waiting": waiting, "total": total},
        "browser_active_pages": browser_active,
        "idle": active == waiting == total == browser_active == 0,
        "observed_at": _now(),
    }


def _cgroup_text(container: str, name: str) -> str:
    try:
        completed = subprocess.run(
            ["docker", "exec", container, "cat", f"/sys/fs/cgroup/{name}"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError("container resource telemetry is unavailable") from exc
    if completed.returncode != 0 or len(completed.stdout) > 16 * 1024:
        raise BenchmarkError("container resource telemetry is unavailable")
    return completed.stdout.strip()


def _counter_map(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in value.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise BenchmarkError("container counter telemetry is invalid")
        result[parts[0]] = int(parts[1])
    return result


def _resource_snapshot() -> dict[str, Any]:
    containers = {
        "api": capacity_runtime.API_CONTAINER,
        "browser": capacity_runtime.BROWSER_CONTAINER,
    }
    result: dict[str, Any] = {"observed_at": _now(), "complete": True}
    try:
        for label, container in containers.items():
            integer_values: dict[str, int | str] = {}
            for name in (
                "memory.current",
                "memory.peak",
                "pids.current",
                "pids.peak",
                "pids.max",
            ):
                raw = _cgroup_text(container, name)
                if raw != "max" and not raw.isdigit():
                    raise BenchmarkError("container resource telemetry is invalid")
                integer_values[name.replace(".", "_")] = (
                    raw if raw == "max" else int(raw)
                )
            result[label] = {
                **integer_values,
                "memory_events": _counter_map(_cgroup_text(container, "memory.events")),
                "pids_events": _counter_map(_cgroup_text(container, "pids.events")),
                "cpu_stat": _counter_map(_cgroup_text(container, "cpu.stat")),
            }
    except BenchmarkError:
        return {
            "observed_at": result["observed_at"],
            "complete": False,
            "error": "container_resource_telemetry_unavailable",
        }
    return result


def _resource_verdict(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    if before.get("complete") is not True or after.get("complete") is not True:
        return {"state": "inconclusive", "reasons": ["telemetry_incomplete"]}
    reasons: list[str] = []
    for label in ("api", "browser"):
        left, right = before[label], after[label]
        for event in ("oom", "oom_kill"):
            if right["memory_events"].get(event, 0) > left["memory_events"].get(
                event, 0
            ):
                reasons.append(f"{label}_{event}")
        if right["pids_events"].get("max", 0) > left["pids_events"].get("max", 0):
            reasons.append(f"{label}_pids_limit_event")
    browser_peak = after["browser"].get("pids_peak")
    if type(browser_peak) is int and browser_peak >= requested["browser_pids"]:
        reasons.append("browser_pids_peak_at_limit")
    return {"state": "failed" if reasons else "measured", "reasons": reasons}


WORKER_SCHEDULER_JS = r"""async function pmap(values, width, callback, shouldStop) {
  const result = new Array(values.length); let cursor = 0;
  async function worker() { while (true) { if (shouldStop()) return; const index = cursor++; if (index >= values.length) return; result[index] = await callback(values[index], index); } }
  await Promise.all(Array.from({ length: Math.min(width, values.length) }, worker));
  return result.filter((value) => value !== undefined);
}"""


WORKER_TEMPLATE = r"""import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { performance } from "node:perf_hooks";
import {
  enrichJllListing, jllDetailCachePath, jllNextData, jllStrandedDocs,
  jllStrandedMedia, jllStringUrls,
} from __JLL_IMPORT__;
import {
  flushPerformance, recordSourceCompleted, recordSourceStarted, withPerformanceSource,
} from __PERFORMANCE_IMPORT__;

const samplePath = process.env.CRE_BENCHMARK_SAMPLE;
const outputPath = process.env.CRE_BENCHMARK_OUTPUT;
const expectedDetails = __EXPECTED_DETAILS__;
const expectedConcurrency = __EXPECTED_CONCURRENCY__;
const concurrency = Number(process.env.JLL_DETAIL_CONCURRENCY);
if (!samplePath || !outputPath || !Number.isInteger(concurrency) || concurrency !== expectedConcurrency) {
  throw new Error("invalid benchmark worker configuration");
}
const sample = JSON.parse(readFileSync(samplePath, "utf8"));
if (sample.kind !== "cre_jll_capacity_sample" || sample.source !== "jll" || sample.details.length !== expectedDetails) {
  throw new Error("invalid exact JLL benchmark sample");
}
const fingerprint = (values) => createHash("sha256").update([...new Set(values)].sort().join("\n")).digest("hex");
const urls = (value) => Array.isArray(value) ? value.filter((item) => typeof item === "string" && /^https?:\/\//.test(item)) : [];
const itemUrls = (value) => Array.isArray(value) ? value.flatMap((item) => typeof item === "string" ? [item] : item && typeof item.url === "string" ? [item.url] : []) : [];
function nativeEvidence(row) {
  const cached = JSON.parse(readFileSync(jllDetailCachePath(row.url), "utf8"));
  const property = jllNextData(cached.rawHtml)?.props?.pageProps?.property;
  if (!property || String(property.id) !== String(row.id)) throw new Error("native identity mismatch");
  const channels = {
    images: jllStringUrls(property.images), brochures: jllStringUrls(property.brochures),
    floor_plans: itemUrls(jllStrandedDocs(property)), videos: urls(property.videos),
    virtual_tours: itemUrls(jllStrandedMedia({ virtualTours: property.virtualTours })),
    view_360: itemUrls(jllStrandedMedia({ view360URLs: property.view360URLs })),
  };
  return {
    counts: Object.fromEntries(Object.entries(channels).map(([key, value]) => [key, value.length])),
    fingerprints: Object.fromEntries(Object.entries(channels).map(([key, value]) => [key, fingerprint(value)])),
    shape: Object.entries(channels).filter(([, value]) => value.length).map(([key]) => key).sort(),
    raw_cache_file: jllDetailCachePath(row.url), raw_cache_sha256: createHash("sha256").update(readFileSync(jllDetailCachePath(row.url))).digest("hex"),
  };
}
__WORKER_SCHEDULER__
const startedAt = new Date().toISOString();
const generation = process.env.CRE_REFRESH_GENERATION;
const performancePath = process.env.CRE_PERFORMANCE_PATH;
let providerStop = null;
function performanceHas429() {
  flushPerformance({ terminal: false });
  if (!performancePath) return false;
  const value = JSON.parse(readFileSync(performancePath, "utf8"));
  return Number(value?.metrics?.requests?.status_counts?.["429"] ?? 0) > 0;
}
function providerSignal(normalized, item) {
  const error = String(normalized?.detailError ?? "").toLowerCase();
  if (/\b429\b/.test(error)) return "detail_error_http_429";
  if (error.includes("challenge")) return "detail_error_challenge";
  try {
    const body = readFileSync(jllDetailCachePath(item.url), "utf8").toLowerCase();
    if (body.includes("cf-chl-") || body.includes("challenge-platform") || body.includes("just a moment")) return "raw_provider_challenge";
  } catch {}
  return performanceHas429() ? "http_429" : null;
}
recordSourceStarted("jll", "sale");
let rows;
try {
  rows = await withPerformanceSource("jll", "sale", () => pmap(sample.details, concurrency, async (item, index) => {
    const started = performance.now();
    const base = { id: item.id, url: item.url, transactionType: item.transaction_class, assetType: item.property_types.join(", ") };
    const normalized = await enrichJllListing(base);
    const signal = providerSignal(normalized, item);
    if (signal && !providerStop) providerStop = { signal, sample_index: index, sample_id: item.sample_id };
    const native = normalized?.detailError ? null : nativeEvidence(item);
    return { sample_index: index, sample_id: item.sample_id, latency_ms: Number((performance.now() - started).toFixed(3)), normalized, native };
  }, () => providerStop !== null));
  recordSourceCompleted("jll", "sale", { outcome: providerStop ? "failed" : "succeeded", listingsEmitted: rows.length });
} catch (error) {
  recordSourceCompleted("jll", "sale", { outcome: "failed" }); flushPerformance({ terminal: true }); throw error;
}
flushPerformance({ terminal: true });
writeFileSync(outputPath, JSON.stringify({ schema_version: 1, kind: "cre_jll_capacity_worker", generation, started_at: startedAt, finished_at: new Date().toISOString(), provider_stop: providerStop, rows }));
"""


def _worker_source(
    repo_root: Path, *, expected_details: int = 128, concurrency: int = 10
) -> str:
    jll = (repo_root / "scripts/firecrawl-ops/cre_collector/sources/jll.ts").as_uri()
    performance_module = (
        repo_root / "scripts/firecrawl-ops/cre_collector/lib/performance.ts"
    ).as_uri()
    return (
        WORKER_TEMPLATE.replace("__JLL_IMPORT__", json.dumps(jll))
        .replace("__PERFORMANCE_IMPORT__", json.dumps(performance_module))
        .replace("__WORKER_SCHEDULER__", WORKER_SCHEDULER_JS)
        .replace("__EXPECTED_DETAILS__", str(expected_details))
        .replace("__EXPECTED_CONCURRENCY__", str(concurrency))
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise BenchmarkError("benchmark worker could not be terminated") from exc


def _run_worker(
    *,
    repo_root: Path,
    sample_path: Path,
    replicate_dir: Path,
    requested: Mapping[str, Any],
    api_url: str,
    timeout_seconds: int,
    expected_details: int,
) -> tuple[int, list[dict[str, Any]], str | None]:
    replicate_dir.mkdir(mode=0o700)
    worker_path = replicate_dir / "worker.ts"
    worker_source = _worker_source(
        repo_root,
        expected_details=expected_details,
        concurrency=int(requested["jll_detail_concurrency"]),
    )
    worker_path.write_text(worker_source, encoding="utf-8")
    worker_path.chmod(0o600)
    output_path = replicate_dir / "worker-output.json"
    performance_path = replicate_dir / "performance.json"
    cache_dir = replicate_dir / "raw-cache"
    cache_dir.mkdir(mode=0o700)
    generation = (
        datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ") + f"-{os.urandom(6).hex()}"
    )
    environment = scrub_environment(os.environ)
    environment.update(
        {
            "FIRECRAWL_API_URL": api_url,
            "FIRECRAWL_API_KEY": "local-self-hosted",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "JLL_DETAIL_CONCURRENCY": str(requested["jll_detail_concurrency"]),
            "JLL_DETAIL_CACHE_DIR": str(cache_dir),
            "JLL_DETAIL_CACHE_MIN_CACHED_AT": _now(),
            "CRE_REQUIRE_FRESH_DETAILS": "1",
            "CRE_REQUIRE_FRESH_PROPERTY_DETAILS": "1",
            "CRE_REFRESH_GENERATION": generation,
            "CRE_REFRESH_STARTED_AT": _now(),
            "CRE_PERFORMANCE_PATH": str(performance_path),
            "CRE_PERFORMANCE_COMMAND_ID": os.urandom(16).hex(),
            "CRE_BENCHMARK_SAMPLE": str(sample_path),
            "CRE_BENCHMARK_OUTPUT": str(output_path),
        }
    )
    tsx = repo_root / "scripts/firecrawl-ops/cre_collector/node_modules/.bin/tsx"
    if not tsx.is_file():
        raise BenchmarkError("collector tsx dependency is unavailable")
    previous = _cpu_ticks()
    stderr_path = replicate_dir / "worker.stderr"
    stderr_descriptor = os.open(
        stderr_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    samples: list[dict[str, Any]] = []
    high_since: float | None = None
    deadline = time.monotonic() + timeout_seconds
    termination_reason: str | None = None
    sample_seconds = requested["host_cpu_sample_seconds"]
    monitor_error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["/usr/bin/nice", "-n", "15", str(tsx), str(worker_path)],
            cwd=repo_root / "scripts/firecrawl-ops/cre_collector",
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=stderr_descriptor,
            start_new_session=True,
        )
        while process.poll() is None:
            try:
                process.wait(timeout=sample_seconds)
                break
            except subprocess.TimeoutExpired:
                current = _cpu_ticks()
                percent = _cpu_percent(previous, current)
                previous = current
                samples.append(
                    {"observed_at": _now(), "host_cpu_percent": round(percent, 2)}
                )
                if percent >= requested["host_cpu_guard_percent"]:
                    high_since = high_since or (time.monotonic() - sample_seconds)
                    if (
                        time.monotonic() - high_since
                        >= requested["host_cpu_guard_seconds"]
                    ):
                        termination_reason = "host_cpu_guard"
                        _terminate(process)
                        break
                else:
                    high_since = None
                if time.monotonic() >= deadline:
                    termination_reason = "replicate_deadline"
                    _terminate(process)
                    break
    except (BenchmarkError, OSError, KeyboardInterrupt) as exc:
        if process is None:
            raise BenchmarkError("benchmark worker could not be started") from exc
        termination_reason = "monitor_telemetry_failure"
        monitor_error = type(exc).__name__
        _terminate(process)
    finally:
        os.close(stderr_descriptor)
    if process is None:
        raise BenchmarkError("benchmark worker did not start")
    stderr_bytes = stderr_path.stat().st_size
    _atomic_private_json(
        replicate_dir / "guard.json",
        {
            "samples": samples,
            "triggered": termination_reason is not None,
            "termination_reason": termination_reason,
            "monitor_error_type": monitor_error,
            "worker_exit_code": process.returncode,
            "stderr_sha256": _file_sha256(stderr_path),
            "stderr_bytes": stderr_bytes,
            "stderr_file": str(stderr_path),
            "worker_source_sha256": _sha256(worker_source.encode()),
        },
    )
    return (
        process.returncode if process.returncode is not None else -1,
        samples,
        termination_reason,
    )


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999))
    )
    return round(ordered[index], 3)


def summarize_replicate(
    replicate_dir: Path, sample: Mapping[str, Any], wall_seconds: float
) -> dict[str, Any]:
    worker = _read_json(replicate_dir / "worker-output.json", MAX_WORKER_OUTPUT_BYTES)
    performance = _read_json(replicate_dir / "performance.json")
    rows = worker.get("rows") if isinstance(worker, dict) else None
    if not isinstance(rows, list):
        raise BenchmarkError("worker output has no rows")
    expected_rows = sample["details"]
    worker_generation = worker.get("generation")
    worker_started_at = worker.get("started_at")
    worker_started_ms = (
        datetime.fromisoformat(worker_started_at.replace("Z", "+00:00")).timestamp()
        if isinstance(worker_started_at, str)
        else None
    )
    errors: list[dict[str, Any]] = []
    latencies: list[float] = []
    identities: set[tuple[str, str]] = set()
    native_matches = 0
    freshness_matches = 0
    actual_by_index = {
        actual.get("sample_index"): actual
        for actual in rows
        if isinstance(actual, dict)
        and type(actual.get("sample_index")) is int
        and 0 <= actual["sample_index"] < len(expected_rows)
    }
    if len(rows) != len(expected_rows) or len(actual_by_index) != len(rows):
        errors.append({"kind": "row_count_or_index", "observed": len(rows)})
    for index, expected in enumerate(expected_rows):
        actual = actual_by_index.get(index)
        if not isinstance(actual, dict):
            errors.append({"sample_id": expected["sample_id"], "kind": "missing_row"})
            continue
        if actual.get("sample_id") != expected["sample_id"]:
            errors.append(
                {"sample_id": expected["sample_id"], "kind": "sample_binding"}
            )
        normalized = actual.get("normalized", {})
        latency = actual.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            latencies.append(float(latency))
        identity = (str(normalized.get("id", "")), str(normalized.get("url", "")))
        identities.add(identity)
        if identity != (expected["id"], expected["url"]):
            errors.append({"sample_id": expected["sample_id"], "kind": "identity"})
        if normalized.get("detailError"):
            errors.append({"sample_id": expected["sample_id"], "kind": "detail_error"})
        provenance = normalized.get("freshnessProvenance", {})
        observed_at = normalized.get("detailObservedAt")
        try:
            observed_ms = (
                datetime.fromisoformat(observed_at.replace("Z", "+00:00")).timestamp()
                if isinstance(observed_at, str)
                else None
            )
        except ValueError:
            observed_ms = None
        if (
            provenance.get("cacheDisposition") == "live"
            and provenance.get("generationId") == worker_generation
            and observed_ms is not None
            and worker_started_ms is not None
            and observed_ms >= worker_started_ms
        ):
            freshness_matches += 1
        else:
            errors.append({"sample_id": expected["sample_id"], "kind": "freshness"})
        current_native = actual.get("native") if isinstance(actual, dict) else None
        historic_native = expected.get("historic", {}).get("native")
        if isinstance(current_native, dict) and (
            current_native.get("fingerprints") == historic_native.get("fingerprints")
        ):
            native_matches += 1
        else:
            errors.append(
                {"sample_id": expected["sample_id"], "kind": "native_asset_delta"}
            )
    requests = performance.get("metrics", {}).get("requests", {})
    remote_unknown = requests.get("timed_out_remote_settlement_unknown")
    if not isinstance(remote_unknown, int):
        remote_unknown = None
    status_counts = requests.get("status_counts", {})
    provider_signals: list[str] = []
    provider_stop = worker.get("provider_stop")
    if isinstance(provider_stop, dict) and isinstance(provider_stop.get("signal"), str):
        provider_signals.append(provider_stop["signal"])
    if isinstance(status_counts, dict) and status_counts.get("429", 0):
        provider_signals.append("http_429")
    for actual in rows:
        normalized = actual.get("normalized", {}) if isinstance(actual, dict) else {}
        detail_error = str(normalized.get("detailError", "")).lower()
        if "challenge" in detail_error or re.search(r"\b429\b", detail_error):
            provider_signals.append("detail_error_429_or_challenge")
            break
    qualified = len(rows) if not errors and len(identities) == len(rows) else 0
    return {
        "wall_seconds": round(wall_seconds, 3),
        "rows": len(rows),
        "unique_id_url_pairs": len(identities),
        "qualified_fresh_unique_rows": qualified,
        "qualified_fresh_unique_per_minute": round(qualified * 60 / wall_seconds, 3)
        if wall_seconds > 0
        else None,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "freshness_matches": freshness_matches,
        "historic_native_asset_matches": native_matches,
        "native_asset_deltas": len(rows) - native_matches,
        "quality_errors": errors,
        "performance": performance,
        "remote_settlement_unknown": remote_unknown,
        "provider_cooldown": {
            "required": bool(provider_signals),
            "signals": sorted(set(provider_signals)),
            "resume": "fresh_operator_admission_required",
        },
        "comparison_state": "measured" if not errors else "quality_failed",
    }


def _comparison_evidence(
    value: Any, label: str
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if not isinstance(value, dict):
        return None, [f"{label}_result_not_object"]
    if value.get("kind") != RESULT_KIND or value.get("mode") != "run":
        reasons.append(f"{label}_result_kind")
    if (
        value.get("completed") is not True
        or value.get("comparison_state") != "complete"
    ):
        reasons.append(f"{label}_not_complete")
    workload = value.get("workload")
    details = workload.get("details") if isinstance(workload, dict) else None
    replicate_count = workload.get("replicates") if isinstance(workload, dict) else None
    if type(details) is not int or details <= 0 or replicate_count != 3:
        reasons.append(f"{label}_workload")
    replicates = value.get("replicates")
    if not isinstance(replicates, list) or len(replicates) != 3:
        reasons.append(f"{label}_replicate_count")
        return None, reasons
    throughput: list[float] = []
    latency: list[dict[str, Any]] = []
    qualified_rows: list[int] = []
    freshness_rows: list[int] = []
    native_matches: list[int] = []
    for index, replicate in enumerate(replicates, 1):
        prefix = f"{label}_replicate_{index}"
        if not isinstance(replicate, dict):
            reasons.append(f"{prefix}_invalid")
            continue
        rate = replicate.get("qualified_fresh_unique_per_minute")
        if type(rate) not in {int, float} or rate <= 0:
            reasons.append(f"{prefix}_throughput")
        else:
            throughput.append(float(rate))
        if (
            replicate.get("comparison_state") != "measured"
            or replicate.get("qualified_fresh_unique_rows") != details
            or replicate.get("freshness_matches") != details
            or replicate.get("historic_native_asset_matches") != details
            or replicate.get("native_asset_deltas") != 0
            or replicate.get("quality_errors") != []
        ):
            reasons.append(f"{prefix}_quality")
        if type(replicate.get("qualified_fresh_unique_rows")) is int:
            qualified_rows.append(replicate["qualified_fresh_unique_rows"])
        if type(replicate.get("freshness_matches")) is int:
            freshness_rows.append(replicate["freshness_matches"])
        if type(replicate.get("historic_native_asset_matches")) is int:
            native_matches.append(replicate["historic_native_asset_matches"])
        if replicate.get("resource_verdict", {}).get("state") != "measured":
            reasons.append(f"{prefix}_resources")
        if (
            replicate.get("source_owned_settlement") != "locally_awaited_terminal"
            or replicate.get("settlement_after", {}).get("idle") is not True
            or replicate.get("remote_settlement_unknown") not in {0, None}
        ):
            reasons.append(f"{prefix}_settlement")
        if replicate.get("provider_cooldown", {}).get("required") is not False:
            reasons.append(f"{prefix}_provider")
        if (
            replicate.get("guard_triggered") is not False
            or replicate.get("termination_reason") is not None
            or replicate.get("worker_exit_code") != 0
        ):
            reasons.append(f"{prefix}_execution")
        latency_value = replicate.get("latency_ms")
        if not isinstance(latency_value, dict) or any(
            type(latency_value.get(key)) not in {int, float}
            for key in ("p50", "p95", "p99")
        ):
            reasons.append(f"{prefix}_latency")
        else:
            latency.append(latency_value)
    required_strings = (
        "sample_inventory_sha256",
        "sample_manifest_sha256",
        "source_git_sha",
        "worker_source_sha256",
    )
    if any(
        not isinstance(value.get(key), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", value[key])
        for key in required_strings
    ):
        reasons.append(f"{label}_provenance")
    if not isinstance(value.get("freshness_policy"), dict):
        reasons.append(f"{label}_freshness_policy")
    if (
        value.get("safety", {}).get("database_writes") != 0
        or value.get("safety", {}).get("canonical_cache_writes") != 0
    ):
        reasons.append(f"{label}_write_boundary")
    if reasons:
        return None, sorted(set(reasons))
    return {
        "median_qualified_fresh_unique_per_minute": round(median(throughput), 3),
        "latency_ms": {
            key: round(median(float(item[key]) for item in latency), 3)
            for key in ("p50", "p95", "p99")
        },
        "details": details,
        "replicates": replicate_count,
        "completeness_fidelity": {
            "qualified_fresh_unique_rows_per_replicate": qualified_rows,
            "freshness_matches_per_replicate": freshness_rows,
            "historic_native_asset_matches_per_replicate": native_matches,
            "native_asset_deltas_per_replicate": [0, 0, 0],
            "all_quality_gates_passed": True,
        },
    }, []


def compare_results(baseline: Any, candidate: Any) -> dict[str, Any]:
    baseline_summary, baseline_reasons = _comparison_evidence(baseline, "baseline")
    candidate_summary, candidate_reasons = _comparison_evidence(candidate, "candidate")
    reasons = baseline_reasons + candidate_reasons
    match_fields = (
        "sample_inventory_sha256",
        "sample_manifest_sha256",
        "source_git_sha",
        "freshness_policy",
        "workload",
    )
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        reasons.extend(
            f"mismatch_{key}"
            for key in match_fields
            if baseline.get(key) != candidate.get(key)
        )
    if reasons or baseline_summary is None or candidate_summary is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "cre_jll_capacity_comparison",
            "state": "inconclusive",
            "decision": "no_adoption_decision",
            "reasons": sorted(set(reasons or ["missing_comparable_evidence"])),
        }
    baseline_rate = baseline_summary["median_qualified_fresh_unique_per_minute"]
    candidate_rate = candidate_summary["median_qualified_fresh_unique_per_minute"]
    gain = round((candidate_rate - baseline_rate) * 100 / baseline_rate, 3)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_jll_capacity_comparison",
        "state": "measured",
        "decision": "adoptable" if gain >= 15 else "do_not_adopt",
        "criterion_percent": 15,
        "gain_percent": gain,
        "baseline": baseline_summary,
        "candidate": candidate_summary,
        "matched": {key: baseline[key] for key in match_fields},
        "variants": {
            "baseline": {
                "requested": baseline.get("requested"),
                "worker_source_sha256": baseline["worker_source_sha256"],
            },
            "candidate": {
                "requested": candidate.get("requested"),
                "worker_source_sha256": candidate["worker_source_sha256"],
            },
        },
    }


def plan(
    profile: Mapping[str, Any], profile_name: str, config_sha256: str, sample: Any
) -> dict[str, Any]:
    workload = profile.get("workload")
    if not isinstance(workload, dict) or workload.get("source") != "jll":
        raise BenchmarkError("profile workload must be JLL")
    validated = validate_sample(sample, int(workload["details"])) if sample else None
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "mode": "dry_run",
        "profile": profile_name,
        "config_sha256": config_sha256,
        "requested": profile["requested"],
        "workload": workload,
        "sample": {
            "validated": validated is not None,
            "inventory_sha256": validated.get("inventory_sha256")
            if validated
            else None,
            "coverage": validated.get("coverage") if validated else None,
        },
        "safety": {
            "database_writes": "forbidden",
            "database_environment": "scrubbed",
            "canonical_cache_writes": "forbidden",
            "artifact_cache": "replicate_scoped_private",
            "explicit_run_required": True,
            "live_run_performed": False,
        },
        "execution": {
            "startable": False,
            "blockers": [
                "explicit_run_flag_required",
                "matching_technical_admission_receipt_required",
            ],
        },
    }


def run_benchmark(
    *,
    repo_root: Path,
    artifact_root: Path,
    sample_path: Path,
    sample: Mapping[str, Any],
    profile: Mapping[str, Any],
    profile_name: str,
    config_sha256: str,
    admission: Mapping[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    replicates = int(profile["workload"]["replicates"])
    sample_provenance = verify_sample_provenance(sample)
    live_admission = verify_live_admission(admission, profile)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "mode": "run",
        "profile": profile_name,
        "config_sha256": config_sha256,
        "source_git_sha": admission["source_git_sha"],
        "worker_source_sha256": _sha256(
            _worker_source(
                repo_root,
                expected_details=int(profile["workload"]["details"]),
                concurrency=int(profile["requested"]["jll_detail_concurrency"]),
            ).encode()
        ),
        "freshness_policy": {
            "require_fresh_details": True,
            "require_fresh_property_details": True,
            "detail_cache_minimum": "replicate_start",
            "firecrawl_max_age": 0,
        },
        "workload": profile["workload"],
        "requested": profile["requested"],
        "sample_inventory_sha256": sample["inventory_sha256"],
        "sample_manifest_sha256": _file_sha256(sample_path),
        "sample_provenance": sample_provenance,
        "admission_sha256": _sha256(_canonical(admission)),
        "live_admission": live_admission,
        "started_at": _now(),
        "replicates": [],
        "safety": {
            "database_writes": 0,
            "canonical_cache_writes": 0,
            "raw_bodies": "retained_in_private_replicate_cache",
        },
    }
    endpoints = admission["endpoints"]
    for number in range(1, replicates + 1):
        replicate_dir = artifact_root / f"replicate-{number}"
        before = _settlement_snapshot(
            endpoints["api_url"], endpoints["browser_health_url"]
        )
        if not before["idle"]:
            raise BenchmarkError("runtime was not idle before the replicate")
        resources_before = _resource_snapshot()
        started = time.monotonic()
        code, host_samples, termination_reason = _run_worker(
            repo_root=repo_root,
            sample_path=sample_path,
            replicate_dir=replicate_dir,
            requested=profile["requested"],
            api_url=endpoints["api_url"],
            timeout_seconds=timeout_seconds,
            expected_details=int(profile["workload"]["details"]),
        )
        wall = time.monotonic() - started
        settlement_error: str | None = None
        try:
            after = _settlement_snapshot(
                endpoints["api_url"], endpoints["browser_health_url"]
            )
        except BenchmarkError as exc:
            settlement_error = type(exc).__name__
            after = {
                "idle": False,
                "observed_at": _now(),
                "error": "post_worker_settlement_unavailable",
            }
        resources_after = _resource_snapshot()
        resource_verdict = _resource_verdict(
            resources_before, resources_after, profile["requested"]
        )
        entry: dict[str, Any] = {
            "replicate": number,
            "worker_exit_code": code,
            "guard_triggered": termination_reason == "host_cpu_guard",
            "termination_reason": termination_reason,
            "host_samples": host_samples,
            "settlement_before": before,
            "settlement_after": after,
            "settlement_error_type": settlement_error,
            "resources_before": resources_before,
            "resources_after": resources_after,
            "resource_verdict": resource_verdict,
            "source_owned_settlement": "unknown"
            if code != 0
            or termination_reason is not None
            or settlement_error is not None
            else "locally_awaited_terminal",
        }
        if code == 0 and termination_reason is None and settlement_error is None:
            summary = summarize_replicate(replicate_dir, sample, wall)
            entry.update(summary)
            if summary["remote_settlement_unknown"] not in {0, None}:
                entry["source_owned_settlement"] = "unknown"
        result["replicates"].append(entry)
        _atomic_private_json(artifact_root / "result.in-progress.json", result)
        if (
            code != 0
            or termination_reason is not None
            or not after["idle"]
            or resource_verdict["state"] != "measured"
            or entry["source_owned_settlement"] == "unknown"
            or entry.get("comparison_state") != "measured"
            or entry.get("provider_cooldown", {}).get("required") is True
        ):
            result["stop_reason"] = (
                "provider_cooldown_required"
                if entry.get("provider_cooldown", {}).get("required") is True
                else "replicate_failed_or_unsettled"
            )
            break
    result["finished_at"] = _now()
    result["completed"] = len(result["replicates"]) == replicates and all(
        row.get("qualified_fresh_unique_rows") == int(profile["workload"]["details"])
        for row in result["replicates"]
    )
    result["comparison_state"] = (
        "complete" if result["completed"] else "inconclusive_or_failed"
    )
    _atomic_private_json(artifact_root / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="bold-jll-128")
    parser.add_argument("--config", type=Path, default=experiment.DEFAULT_CONFIG)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--prepare-sample", type=Path, metavar="CACHE_DIR")
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--compare-baseline", type=Path)
    parser.add_argument("--compare-candidate", type=Path)
    parser.add_argument("--replicate-timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[3]
    try:
        if args.compare_baseline or args.compare_candidate:
            if not args.compare_baseline or not args.compare_candidate:
                comparison = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "cre_jll_capacity_comparison",
                    "state": "inconclusive",
                    "decision": "no_adoption_decision",
                    "reasons": ["both_comparison_results_required"],
                }
            else:
                try:
                    comparison = compare_results(
                        _read_json(args.compare_baseline, MAX_WORKER_OUTPUT_BYTES),
                        _read_json(args.compare_candidate, MAX_WORKER_OUTPUT_BYTES),
                    )
                except BenchmarkError:
                    comparison = {
                        "schema_version": SCHEMA_VERSION,
                        "kind": "cre_jll_capacity_comparison",
                        "state": "inconclusive",
                        "decision": "no_adoption_decision",
                        "reasons": ["comparison_result_unavailable_or_invalid"],
                    }
            print(json.dumps(comparison, sort_keys=True, indent=2))
            return 0
        if not args.artifact_root:
            raise BenchmarkError("--artifact-root is required outside compare mode")
        artifact_root = _private_artifact_root(args.artifact_root, repo_root)
        profile, digest = experiment.load_profile(args.config, args.profile)
        if args.prepare_sample:
            sample = build_sample(args.prepare_sample)
            target = artifact_root / "jll-128-sample.json"
            _atomic_private_json(target, sample)
            print(
                json.dumps(
                    {"sample": str(target), "coverage": sample["coverage"]}, indent=2
                )
            )
            return 0
        sample_value = _read_json(args.sample) if args.sample else None
        dry_plan = plan(profile, args.profile, digest, sample_value)
        _atomic_private_json(artifact_root / "plan.json", dry_plan)
        if not args.run:
            print(json.dumps(dry_plan, sort_keys=True, indent=2))
            return 0
        if not args.sample or not args.admission:
            raise BenchmarkError("--run requires --sample and --admission")
        admission = validate_admission(
            _read_json(args.admission),
            profile,
            args.profile,
            digest,
            source_git_sha=_git_head(repo_root),
        )
        result = run_benchmark(
            repo_root=repo_root,
            artifact_root=artifact_root,
            sample_path=args.sample.resolve(),
            sample=validate_sample(sample_value, int(profile["workload"]["details"])),
            profile=profile,
            profile_name=args.profile,
            config_sha256=digest,
            admission=admission,
            timeout_seconds=args.replicate_timeout_seconds,
        )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["completed"] else 75
    except (BenchmarkError, experiment.ProfileError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
