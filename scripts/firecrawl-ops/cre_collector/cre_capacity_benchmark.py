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
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import cre_capacity_experiment as experiment
import cre_capacity_runtime as capacity_runtime
import cre_capacity_telemetry as capacity_telemetry
from cre_checkpoint_refresh import (
    BENCHMARK_QUARANTINE_MARKER,
    LockHeldError,
    SharedLock,
    canonical_shared_lock_dir,
)

SCHEMA_VERSION = 1
SAMPLE_KIND = "cre_jll_capacity_sample"
ADMISSION_KIND = "cre_capacity_runtime_admission"
RESULT_KIND = "cre_jll_capacity_benchmark"
SUPPORTED_BASELINE_ADMISSION_AVAILABLE = False
MAX_SAMPLE_BYTES = 8 * 1024 * 1024
MAX_CACHE_RECORD_BYTES = 4 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 512 * 1024 * 1024
REVIEW_BENCHMARK_GRANT_MAX_BYTES = 64 * 1024
NEXT_DATA = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
WORKER_ENV_ALLOWLIST = frozenset(
    {"PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
)
ALLOWED_API_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOOPBACK_ENDPOINTS = {
    "api_url": "http://127.0.0.1:3102",
    "browser_health_url": "http://127.0.0.1:3103/health",
}
SETTLEMENT_TIMEOUT_SECONDS = 60
SETTLEMENT_POLL_SECONDS = 2
EXPECTED_FRESHNESS_POLICY = {
    "require_fresh_details": True,
    "require_fresh_property_details": True,
    "detail_cache_minimum": "replicate_start",
    "firecrawl_max_age": 0,
}
IMPLEMENTATION_PATHS = (
    "scripts/firecrawl-ops/cre_collector/cre_capacity_benchmark.py",
    "scripts/firecrawl-ops/cre_collector/cre_capacity_experiment.py",
    "scripts/firecrawl-ops/cre_collector/cre_capacity_runtime.py",
    "scripts/firecrawl-ops/cre_collector/cre_capacity_telemetry.py",
    "scripts/firecrawl-ops/cre_collector/cre_checkpoint_refresh.py",
    "scripts/firecrawl-ops/cre_collector/sources/jll.ts",
    "scripts/firecrawl-ops/cre_collector/lib/broker.ts",
    "scripts/firecrawl-ops/cre_collector/lib/config.ts",
    "scripts/firecrawl-ops/cre_collector/lib/freshness.ts",
    "scripts/firecrawl-ops/cre_collector/lib/harvest.ts",
    "scripts/firecrawl-ops/cre_collector/lib/html.ts",
    "scripts/firecrawl-ops/cre_collector/lib/parse.ts",
    "scripts/firecrawl-ops/cre_collector/lib/performance.ts",
    "scripts/firecrawl-ops/cre_collector/lib/scrape.ts",
    "scripts/firecrawl-ops/cre_collector/lib/util.ts",
    "scripts/firecrawl-ops/cre_collector/types.ts",
    "scripts/firecrawl-ops/cre_collector/package.json",
    "scripts/firecrawl-ops/cre_collector/package-lock.json",
    "scripts/firecrawl-ops/cre_collector/tsconfig.json",
)
REVIEW_BENCHMARK_GRANT_CONSUMER = r"""
import json
import os
import re
import secrets
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent = os.path.dirname(path)
name = os.path.basename(path)
uid = os.getuid()
euid = os.geteuid()
if uid == 0 or euid == 0 or uid != euid:
    raise SystemExit("grant consumer requires a non-root unswitched operating account")
if not re.fullmatch(r"\.cre-capacity-benchmark-grant-[0-9a-f]{64}\.json", name):
    raise SystemExit("grant path is invalid")
file_stat = os.lstat(path)
parent_stat = os.lstat(parent)
if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1
        or file_stat.st_uid != euid or stat.S_IMODE(file_stat.st_mode) != 0o600):
    raise SystemExit("grant is not a singly linked operator-owned mode 0600 file")
if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != euid
        or stat.S_IMODE(parent_stat.st_mode) != 0o700):
    raise SystemExit("grant parent is not operator-owned mode 0700")
def fsync_parent():
    descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
consumed = os.path.join(
    parent,
    "." + name + ".consumed-" + secrets.token_hex(16),
)
os.rename(path, consumed)
fsync_parent()
try:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(consumed, flags)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != euid or stat.S_IMODE(opened.st_mode) != 0o600):
            raise SystemExit("consumed grant ownership changed")
        chunks = []
        remaining = 65537
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 65536:
            raise SystemExit("grant is too large")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("grant JSON is invalid")
    if not isinstance(value, dict):
        raise SystemExit("grant JSON root is invalid")
    sys.stdout.buffer.write(raw)
finally:
    os.unlink(consumed)
    fsync_parent()
"""


class BenchmarkError(ValueError):
    """The benchmark cannot safely proceed."""


def _operator_uid() -> int:
    """Return the ordinary operating-account UID or fail on privilege switching."""
    uid = os.getuid()
    euid = os.geteuid()
    if uid == 0 or euid == 0 or uid != euid:
        raise BenchmarkError(
            "capacity benchmark requires a non-root unswitched operating account"
        )
    return euid


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes or fail closed."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BenchmarkError(
            "technical admission consumption directory is not durable"
        ) from exc


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_source_sha(value: Any) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40,64}", value) is not None
    )


def _same_typed_value(observed: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        return bool(
            isinstance(observed, Mapping)
            and set(observed) == set(expected)
            and all(_same_typed_value(observed[key], expected[key]) for key in expected)
        )
    if isinstance(expected, list):
        return bool(
            isinstance(observed, list)
            and len(observed) == len(expected)
            and all(
                _same_typed_value(left, right)
                for left, right in zip(observed, expected, strict=True)
            )
        )
    return type(observed) is type(expected) and observed == expected


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_manifest(repo_root: Path) -> dict[str, Any]:
    """Hash only executable benchmark inputs, independent of Git/config state."""
    files: dict[str, str] = {}
    for relative in IMPLEMENTATION_PATHS:
        path = repo_root / relative
        if path.is_symlink() or not path.is_file():
            raise BenchmarkError(
                f"benchmark implementation input is unavailable: {relative}"
            )
        files[relative] = _file_sha256(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_capacity_benchmark_implementation",
        "files": files,
        "sha256": _sha256(_canonical(files)),
    }


def _require_clean_git(repo_root: Path) -> str:
    """Require a clean index and worktree, including untracked files."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError("cannot verify clean benchmark source") from exc
    if completed.returncode != 0:
        raise BenchmarkError("cannot verify clean benchmark source")
    if completed.stdout:
        raise BenchmarkError("benchmark requires a clean Git index and worktree")
    return _git_head(repo_root)


def _verify_implementation_manifest(
    repo_root: Path, expected: Mapping[str, Any]
) -> dict[str, Any]:
    _require_clean_git(repo_root)
    observed = _implementation_manifest(repo_root)
    if observed != expected:
        raise BenchmarkError("benchmark implementation changed between replicates")
    return observed


def _validate_review_grant_freshness(
    grant: Mapping[str, Any], *, now: datetime | None = None
) -> None:
    """Enforce the review-bound clock, never a renewed local admission timestamp."""
    created_value = grant.get("review_approval_created_at")
    expiry = grant.get("expires_after_seconds")
    if (
        not isinstance(created_value, str)
        or type(expiry) is not int
        or expiry != capacity_runtime.RECEIPT_MAX_AGE_SECONDS
    ):
        raise BenchmarkError("review benchmark grant expiry contract is invalid")
    try:
        created = datetime.fromisoformat(created_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkError("review benchmark grant timestamp is invalid") from exc
    if created.tzinfo is None:
        raise BenchmarkError("review benchmark grant timestamp is invalid")
    age = ((now or datetime.now(UTC)) - created.astimezone(UTC)).total_seconds()
    if age < 0 or age > expiry:
        raise BenchmarkError("review benchmark grant is stale")


def _consume_admission(
    admission_path: Path,
    admission: Mapping[str, Any],
    *,
    canonical_lock_path: Path | None = None,
) -> Path:
    """Consume the review grant, then retain a non-authoritative local audit."""
    operator_uid = _operator_uid()
    candidate = admission_path.expanduser()
    try:
        admission_stat = candidate.lstat()
    except OSError as exc:
        raise BenchmarkError("technical admission path is unavailable") from exc
    if (
        not stat.S_ISREG(admission_stat.st_mode)
        or admission_stat.st_nlink != 1
        or admission_stat.st_uid != operator_uid
        or stat.S_IMODE(admission_stat.st_mode) != 0o600
    ):
        raise BenchmarkError("technical admission path is not a regular file")
    resolved = candidate.resolve()
    if _canonical(_read_json(resolved)) != _canonical(admission):
        raise BenchmarkError("technical admission changed after validation")
    nonce_sha256 = admission.get("review_approval_nonce_sha256")
    if not _is_sha256(nonce_sha256):
        raise BenchmarkError("technical admission review approval binding is invalid")
    grant_value = admission.get("review_benchmark_grant_path")
    if not isinstance(grant_value, str):
        raise BenchmarkError("review benchmark grant path is missing")
    grant_path = Path(grant_value)
    expected_name = f".cre-capacity-benchmark-grant-{nonce_sha256}.json"
    if (
        not grant_path.is_absolute()
        or grant_path.name != expected_name
        or grant_path.parent == grant_path
    ):
        raise BenchmarkError("review benchmark grant path is invalid")
    try:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                REVIEW_BENCHMARK_GRANT_CONSUMER,
                str(grant_path),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError("review benchmark grant consumer is unavailable") from exc
    if completed.returncode != 0:
        raise BenchmarkError("review benchmark grant consumption failed")
    raw = completed.stdout
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > REVIEW_BENCHMARK_GRANT_MAX_BYTES
    ):
        raise BenchmarkError("review benchmark grant response is invalid")
    try:
        grant = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("review benchmark grant response is invalid") from exc
    expected_grant = {
        "schema_version": SCHEMA_VERSION,
        "kind": capacity_runtime.BENCHMARK_GRANT_KIND,
        "profile": admission.get("profile"),
        "config_sha256": admission.get("config_sha256"),
        "transition_receipt_sha256": admission.get("transition_receipt_sha256"),
        "source_git_sha": admission.get("source_git_sha"),
        "review_approval_nonce_sha256": nonce_sha256,
        "review_approval_created_at": admission.get("review_approval_created_at"),
        "expires_after_seconds": admission.get("expires_after_seconds"),
        "approved": True,
    }
    if not _same_typed_value(grant, expected_grant):
        raise BenchmarkError("review benchmark grant does not bind this admission")
    _validate_review_grant_freshness(grant)
    admission_sha256 = _sha256(_canonical(admission))
    grant_sha256 = _sha256(_canonical(grant))
    if canonical_lock_path is None:
        try:
            lock_path = canonical_shared_lock_dir(Path(__file__).resolve().parents[3])
        except (OSError, subprocess.SubprocessError) as exc:
            raise BenchmarkError(
                "canonical admission consumption path is unavailable"
            ) from exc
    else:
        lock_path = canonical_lock_path
    lock_path = lock_path.resolve()
    if lock_path.name != ".cre.lock" or lock_path.parent.name != "daily":
        raise BenchmarkError("canonical admission consumption path is invalid")
    consumption_root = lock_path.parent.parent / ".capacity-admission-consumption"
    consumption_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        consumption_stat = consumption_root.lstat()
    except OSError as exc:
        raise BenchmarkError(
            "canonical admission consumption directory is unsafe"
        ) from exc
    if (
        not stat.S_ISDIR(consumption_stat.st_mode)
        or consumption_stat.st_uid != operator_uid
        or stat.S_IMODE(consumption_stat.st_mode) != 0o700
    ):
        raise BenchmarkError("canonical admission consumption directory is unsafe")
    _fsync_directory(consumption_root.parent)
    marker = consumption_root / f"{nonce_sha256}.json"
    payload = (
        _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "cre_capacity_admission_consumption",
                "admission_sha256": admission_sha256,
                "review_benchmark_grant_sha256": grant_sha256,
                "review_approval_nonce_sha256": nonce_sha256,
                "review_approval_created_at": grant["review_approval_created_at"],
                "expires_after_seconds": grant["expires_after_seconds"],
                "consumed_at": _now(),
                "pid": os.getpid(),
            }
        )
        + b"\n"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError as exc:
        raise BenchmarkError(
            "technical admission was already consumed; a fresh operator admission is required"
        ) from exc
    except OSError as exc:
        raise BenchmarkError(
            "technical admission consumption could not be recorded"
        ) from exc
    marker_created = True
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != operator_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise BenchmarkError("technical admission consumption marker is unsafe")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise BenchmarkError("technical admission consumption write was short")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        if marker_created:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(descriptor)
    try:
        _fsync_directory(consumption_root)
    except BenchmarkError:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        raise
    return marker


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
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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


def _experiment_contract() -> dict[str, Any]:
    """Load both treatments and the workload from the one central JSON file."""
    document = _read_json(experiment.DEFAULT_CONFIG, experiment.MAX_CONFIG_BYTES)
    if (
        not isinstance(document, Mapping)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != experiment.SCHEMA_VERSION
        or not isinstance(document.get("profiles"), Mapping)
    ):
        raise BenchmarkError("central experiment configuration is invalid")
    profiles = document["profiles"]
    baseline_name = document.get("default_profile")
    candidate_names = [
        name
        for name, value in profiles.items()
        if isinstance(name, str)
        and isinstance(value, Mapping)
        and value.get("kind") == "experiment"
    ]
    if (
        not isinstance(baseline_name, str)
        or not isinstance(profiles.get(baseline_name), Mapping)
        or profiles[baseline_name].get("kind") != "baseline"
        or len(candidate_names) != 1
    ):
        raise BenchmarkError("central experiment treatments are ambiguous")
    candidate_name = candidate_names[0]
    try:
        baseline, baseline_digest = experiment.load_profile(
            experiment.DEFAULT_CONFIG, baseline_name
        )
        candidate, candidate_digest = experiment.load_profile(
            experiment.DEFAULT_CONFIG, candidate_name
        )
    except experiment.ProfileError as exc:
        raise BenchmarkError("central experiment configuration is invalid") from exc
    workload = candidate.get("workload")
    if baseline_digest != candidate_digest or not isinstance(workload, dict):
        raise BenchmarkError("central experiment configuration is inconsistent")
    return {
        "config_sha256": candidate_digest,
        "workload": dict(workload),
        "profiles": {"baseline": baseline_name, "candidate": candidate_name},
        "requested": {
            "baseline": dict(baseline["requested"]),
            "candidate": dict(candidate["requested"]),
        },
    }


def _transaction_type(transaction_class: Any) -> str:
    if transaction_class in {"sale", "sale_or_lease"}:
        return "Sale"
    if transaction_class == "lease":
        return "Lease"
    raise BenchmarkError("sample transaction class is invalid")


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


def _present(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return False


def _structural_fidelity(fields: Mapping[str, Mapping[str, bool]]) -> dict[str, Any]:
    normalized = {
        channel: {key: value is True for key, value in sorted(channel_fields.items())}
        for channel, channel_fields in sorted(fields.items())
    }
    channels = {
        channel: any(channel_fields.values())
        for channel, channel_fields in normalized.items()
    }
    return {
        "fields": normalized,
        "channels": channels,
        "supported_fields": sorted(
            f"{channel}.{key}"
            for channel, channel_fields in normalized.items()
            for key, present in channel_fields.items()
            if present
        ),
        "supported_channels": sorted(
            channel for channel, present in channels.items() if present
        ),
        "shape_sha256": _sha256(_canonical(normalized)),
    }


def _historic_fidelity(
    property_value: Mapping[str, Any],
    page_props: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    brokers = page_props.get("brokers") or property_value.get("brokers")
    return _structural_fidelity(
        {
            "address": {
                "street": _present(property_value.get("address")),
                "city": _present(property_value.get("city")),
                "state": _present(property_value.get("state")),
                "postal_code": _present(property_value.get("postcode")),
            },
            "price_rate": {
                "sale": _present(property_value.get("salePrice")),
                "lease": _present(property_value.get("rentPrice")),
            },
            "size": {"surface_area": _present(property_value.get("surfaceArea"))},
            "brokers_contacts": {"contacts": _present(brokers)},
            "documents": {
                "brochures": bool(_string_urls(property_value.get("brochures"))),
                "floor_plans": bool(_floor_plan_urls(property_value.get("floorPlans"))),
            },
            "images": {"photos": bool(_string_urls(property_value.get("images")))},
            "media": {
                "videos": bool(_string_urls(property_value.get("videos"))),
                "tours_360": bool(
                    _string_urls(
                        property_value.get("virtualTours")
                        if isinstance(property_value.get("virtualTours"), list)
                        else [property_value.get("virtualTours")]
                    )
                    or _string_urls(
                        property_value.get("view360URLs")
                        if isinstance(property_value.get("view360URLs"), list)
                        else [property_value.get("view360URLs")]
                    )
                ),
                "other": bool(
                    _string_urls(
                        property_value.get("media")
                        if isinstance(property_value.get("media"), list)
                        else [property_value.get("media")]
                    )
                ),
            },
            "markdown": {"body": _present(record.get("markdown"))},
        }
    )


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
            "fidelity": _historic_fidelity(property_value, page_props, record),
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


def _valid_structural_fidelity(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    fields = value.get("fields")
    channels = value.get("channels")
    if (
        not isinstance(fields, dict)
        or not isinstance(channels, dict)
        or set(fields) != set(channels)
        or not all(
            isinstance(channel_fields, dict)
            and channel_fields
            and all(type(item) is bool for item in channel_fields.values())
            for channel_fields in fields.values()
        )
    ):
        return False
    expected = _structural_fidelity(fields)
    return value == expected


def validate_sample(value: Any, expected_details: int = 128) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value.get("kind") != SAMPLE_KIND
    ):
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
        fidelity = historic.get("fidelity") if isinstance(historic, dict) else None
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
            or not _valid_structural_fidelity(fidelity)
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
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value.get("kind") != ADMISSION_KIND
    ):
        raise BenchmarkError("technical admission receipt kind is invalid")
    contract = _experiment_contract()
    if (
        profile_name != contract["profiles"]["candidate"]
        or config_sha256 != contract["config_sha256"]
        or profile.get("requested") != contract["requested"]["candidate"]
        or profile.get("workload") != contract["workload"]
    ):
        raise BenchmarkError("technical admission is not the central experiment")
    if value.get("admitted") is not True:
        raise BenchmarkError("technical admission has not admitted execution")
    if (
        value.get("profile") != profile_name
        or not _is_sha256(value.get("config_sha256"))
        or value.get("config_sha256") != config_sha256
    ):
        raise BenchmarkError("technical admission is not bound to this profile/config")
    if (
        not _is_source_sha(value.get("source_git_sha"))
        or value.get("source_git_sha") != source_git_sha
    ):
        raise BenchmarkError("technical admission source SHA does not match HEAD")
    if value.get("writes") != "forbidden":
        raise BenchmarkError("technical admission does not forbid writes")
    if not _is_sha256(value.get("transition_receipt_sha256")):
        raise BenchmarkError(
            "technical admission transition receipt binding is invalid"
        )
    if not _is_sha256(value.get("review_approval_nonce_sha256")):
        raise BenchmarkError("technical admission review approval binding is invalid")
    grant_path = value.get("review_benchmark_grant_path")
    expected_grant_name = (
        f".cre-capacity-benchmark-grant-{value['review_approval_nonce_sha256']}.json"
    )
    if (
        not isinstance(grant_path, str)
        or not Path(grant_path).is_absolute()
        or Path(grant_path).name != expected_grant_name
    ):
        raise BenchmarkError("technical admission review benchmark grant is invalid")
    if value.get("expires_after_seconds") != capacity_runtime.RECEIPT_MAX_AGE_SECONDS:
        raise BenchmarkError("technical admission expiry contract is invalid")
    _validate_review_grant_freshness(value, now=now)
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
        or not _is_source_sha(repo.get("git_sha"))
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
    return dict(value)


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


def _effective_runtime_evidence(
    public: Mapping[str, Any], requested: Mapping[str, Any]
) -> dict[str, Any]:
    """Select treatment and stable identity fields from a live runtime capture."""
    try:
        api = public["api"]
        browser = public["browser"]
        host = public["host"]
        repo = public["repo"]
        api_env = api["env"]
        browser_env = browser["env"]
        browser_nano = browser["nano_cpus"]
        api_nano = api["nano_cpus"]
        page_slots = browser["page_slots"]
        if (
            type(browser_nano) is not int
            or browser_nano % 1_000_000_000
            or type(api_nano) is not int
            or api_nano % 1_000_000_000
            or not isinstance(page_slots, str)
            or not page_slots.isdigit()
        ):
            raise KeyError("runtime treatment")
        treatment = dict(requested)
        treatment.update(
            {
                "browser_cpus": browser_nano // 1_000_000_000,
                "global_pages": int(page_slots),
                "browser_pids": browser["pids_limit"],
                "api_cpus": api_nano // 1_000_000_000,
            }
        )
        identity = {
            "api": {
                "image": api["image"],
                "environment_keys_sha256": api_env["keys_sha256"],
                "environment_values_sha256": api_env["values_sha256"],
                "network_mode": api["network_mode"],
                "port_bindings": api["port_bindings"],
                "mounts_sha256": api["mounts_sha256"],
                "security_opt": api["security_opt"],
                "cap_drop": api["cap_drop"],
            },
            "browser": {
                "image": browser["image"],
                "environment_keys_sha256": browser_env["keys_sha256"],
                "environment_except_pages_sha256": browser_env[
                    "excluding_pages_sha256"
                ],
                "network_mode": browser["network_mode"],
                "port_bindings": browser["port_bindings"],
                "mounts_sha256": browser["mounts_sha256"],
                "security_opt": browser["security_opt"],
                "cap_drop": browser["cap_drop"],
            },
            "topology": {
                "docker_context": host["docker_context"],
                "compose_sha256": repo["compose_sha256"],
                "override_sha256": repo["override_sha256"],
                "execution_inputs_sha256": repo["execution_inputs_sha256"],
            },
        }
    except (KeyError, TypeError) as exc:
        raise BenchmarkError("effective runtime identity is incomplete") from exc
    return {"treatment": treatment, "identity": identity}


def _valid_effective_runtime(value: Any, expected_requested: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"treatment", "identity"}:
        return False
    if not _same_typed_value(value.get("treatment"), expected_requested):
        return False
    identity = value.get("identity")
    if not isinstance(identity, Mapping) or set(identity) != {
        "api",
        "browser",
        "topology",
    }:
        return False
    required = {
        "api": {
            "image",
            "environment_keys_sha256",
            "environment_values_sha256",
            "network_mode",
            "port_bindings",
            "mounts_sha256",
            "security_opt",
            "cap_drop",
        },
        "browser": {
            "image",
            "environment_keys_sha256",
            "environment_except_pages_sha256",
            "network_mode",
            "port_bindings",
            "mounts_sha256",
            "security_opt",
            "cap_drop",
        },
    }
    for label, keys in required.items():
        container = identity.get(label)
        if (
            not isinstance(container, Mapping)
            or set(container) != keys
            or not isinstance(container.get("image"), str)
            or not container["image"]
            or not isinstance(container.get("network_mode"), str)
            or not container["network_mode"]
            or not isinstance(container.get("port_bindings"), Mapping)
            or any(
                not _is_sha256(container.get(key))
                for key in keys
                if key.endswith("_sha256")
            )
        ):
            return False
    topology = identity.get("topology")
    inputs = (
        topology.get("execution_inputs_sha256")
        if isinstance(topology, Mapping)
        else None
    )
    return bool(
        isinstance(topology, Mapping)
        and set(topology)
        == {
            "docker_context",
            "compose_sha256",
            "override_sha256",
            "execution_inputs_sha256",
        }
        and topology.get("docker_context") == "orbstack"
        and _is_sha256(topology.get("compose_sha256"))
        and _is_sha256(topology.get("override_sha256"))
        and isinstance(inputs, Mapping)
        and set(inputs) == set(capacity_runtime.EXECUTION_INPUTS)
        and all(_is_sha256(digest) for digest in inputs.values())
    )


def _valid_live_admission(
    value: Any,
    *,
    effective_runtime: Any,
    expected_requested: Mapping[str, Any],
) -> bool:
    required = {
        "observed_at",
        "snapshot_sha256",
        "transition_sha256",
        "checks",
        "other_collector_process_active",
        "effective_runtime",
    }
    checks = value.get("checks") if isinstance(value, Mapping) else None
    return bool(
        isinstance(value, Mapping)
        and set(value) == required
        and isinstance(value.get("observed_at"), str)
        and _is_sha256(value.get("snapshot_sha256"))
        and _is_sha256(value.get("transition_sha256"))
        and isinstance(checks, Mapping)
        and bool(checks)
        and all(item is True for item in checks.values())
        and value.get("other_collector_process_active") is False
        and value.get("effective_runtime") == effective_runtime
        and _valid_effective_runtime(effective_runtime, expected_requested)
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
        "effective_runtime": _effective_runtime_evidence(public, profile["requested"]),
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


def _settlement_backends(api_url: str) -> dict[str, Any]:
    active = _http_json(f"{api_url}/v2/crawl/active")
    crawls = active.get("crawls")
    if crawls is None and isinstance(active.get("data"), Mapping):
        crawls = active["data"].get("crawls")
    if not isinstance(crawls, list):
        raise BenchmarkError("active crawl settlement is invalid")

    def command(argv: list[str]) -> str:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BenchmarkError("backend settlement telemetry is unavailable") from exc
        if completed.returncode != 0 or len(completed.stdout) > 128 * 1024:
            raise BenchmarkError("backend settlement telemetry is unavailable")
        return completed.stdout

    rabbit_rows = command(
        [
            "docker",
            "exec",
            capacity_runtime.RABBIT_CONTAINER,
            "rabbitmqctl",
            "list_queues",
            "name",
            "messages_ready",
            "messages_unacknowledged",
            "--quiet",
        ]
    )
    nuq_rows = command(
        [
            "docker",
            "exec",
            capacity_runtime.NUQ_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-At",
            "-F",
            "|",
            "-c",
            (
                "select 'queue_scrape_total', count(*) from nuq.queue_scrape union all "
                "select 'queue_scrape_backlog_total', count(*) from nuq.queue_scrape_backlog union all "
                "select 'queue_crawl_finished_total', count(*) from nuq.queue_crawl_finished order by 1;"
            ),
        ]
    )
    try:
        rabbit_settlement = capacity_telemetry.parse_rabbitmq_settlement(rabbit_rows)
        nuq_settlement = capacity_telemetry.parse_nuq_settlement(nuq_rows)
    except capacity_telemetry.CapacityTelemetryError as exc:
        raise BenchmarkError(str(exc)) from exc
    return {
        "active_crawls": len(crawls),
        **rabbit_settlement,
        **nuq_settlement,
    }


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
    backends = _settlement_backends(api_url)
    backend_idle = (
        backends["active_crawls"] == 0
        and backends["rabbitmq_queue_count"] > 0
        and backends["rabbitmq_ready"] == 0
        and backends["rabbitmq_unacknowledged"] == 0
        and all(value == 0 for value in backends["nuq"].values())
    )
    return {
        "queue": {"active": active, "waiting": waiting, "total": total},
        "browser_active_pages": browser_active,
        **backends,
        "idle": active == waiting == total == browser_active == 0 and backend_idle,
        "observed_at": _now(),
    }


def _await_idle_settlement(
    api_url: str,
    browser_url: str,
    *,
    timeout_seconds: int = SETTLEMENT_TIMEOUT_SECONDS,
    poll_seconds: int = SETTLEMENT_POLL_SECONDS,
) -> dict[str, Any]:
    """Poll bounded loopback evidence until all observed work is idle."""
    deadline = time.monotonic() + timeout_seconds
    observations: list[dict[str, Any]] = []
    while True:
        try:
            snapshot = _settlement_snapshot(api_url, browser_url)
            observations.append(snapshot)
            if snapshot["idle"]:
                return {
                    **snapshot,
                    "state": "idle",
                    "polls": len(observations),
                    "observations": observations,
                }
        except BenchmarkError:
            observations.append(
                {
                    "idle": False,
                    "observed_at": _now(),
                    "error": "settlement_telemetry_unavailable",
                }
            )
        if time.monotonic() >= deadline:
            return {
                "idle": False,
                "state": "unknown",
                "polls": len(observations),
                "observed_at": _now(),
                "observations": observations,
                "error": "bounded_idle_settlement_not_proven",
            }
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


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


def _worker_contract(expected_details: int, concurrency: int) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_jll_capacity_worker_contract",
        "source": "jll",
        "details": expected_details,
        "concurrency": concurrency,
        "transaction_type_mapping": {
            "sale": "Sale",
            "sale_or_lease": "Sale",
            "lease": "Lease",
        },
    }
    return {**payload, "sha256": _sha256(_canonical(payload))}


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
const workerContractSha256 = __WORKER_CONTRACT_SHA256__;
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
const present = (value) => typeof value === "number" || (typeof value === "string" && value.trim().length > 0) || (Array.isArray(value) && value.length > 0) || (!!value && typeof value === "object" && Object.keys(value).length > 0);
function transactionTypeFor(value) {
  if (value === "sale" || value === "sale_or_lease") return "Sale";
  if (value === "lease") return "Lease";
  throw new Error("invalid JLL benchmark transaction class");
}
function structuralEvidence(fields) {
  const ordered = Object.fromEntries(Object.entries(fields).sort().map(([channel, entries]) => [channel, Object.fromEntries(Object.entries(entries).sort().map(([key, value]) => [key, value === true]))]));
  const channels = Object.fromEntries(Object.entries(ordered).map(([channel, entries]) => [channel, Object.values(entries).some(Boolean)]));
  const supportedFields = Object.entries(ordered).flatMap(([channel, entries]) => Object.entries(entries).filter(([, value]) => value).map(([key]) => `${channel}.${key}`)).sort();
  const supportedChannels = Object.entries(channels).filter(([, value]) => value).map(([channel]) => channel).sort();
  return { fields: ordered, channels, supported_fields: supportedFields, supported_channels: supportedChannels, shape_sha256: createHash("sha256").update(JSON.stringify(ordered)).digest("hex") };
}
function normalizedEvidence(normalized) {
  const documents = Array.isArray(normalized?.documents) ? normalized.documents : [];
  const media = Array.isArray(normalized?.media) ? normalized.media : [];
  return structuralEvidence({
    address: { street: present(normalized?.street), city: present(normalized?.city), state: present(normalized?.state), postal_code: present(normalized?.postalCode) },
    price_rate: { sale: present(normalized?.salePriceText) || present(normalized?.salePriceUsd), lease: present(normalized?.leaseRateText) },
    size: { surface_area: present(normalized?.sizeText) || present(normalized?.buildingSizeSqft) },
    brokers_contacts: { contacts: present(normalized?.contactsDetailed) || present(normalized?.brokerIds) },
    documents: { brochures: present(normalized?.brochures), floor_plans: documents.some((item) => String(item?.docType ?? item?.documentType ?? item?.type ?? "").toLowerCase().includes("floor")) },
    images: { photos: present(normalized?.photos) },
    media: { videos: media.some((item) => item?.mediaType === "video"), tours_360: media.some((item) => item?.mediaType === "virtual_tour" || item?.mediaType === "matterport"), other: media.some((item) => item?.mediaType === "other") },
    markdown: { body: present(normalized?.markdown) },
  });
}
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
    const expectedTransactionType = transactionTypeFor(item.transaction_class);
    const base = { id: item.id, url: item.url, transactionType: expectedTransactionType, assetType: item.property_types.join(", ") };
    const normalized = await enrichJllListing(base);
    const signal = providerSignal(normalized, item);
    if (signal && !providerStop) providerStop = { signal, sample_index: index, sample_id: item.sample_id };
    const native = normalized?.detailError ? null : nativeEvidence(item);
    return { sample_index: index, sample_id: item.sample_id, transaction_type: expectedTransactionType, latency_ms: Number((performance.now() - started).toFixed(3)), normalized, native, fidelity: normalizedEvidence(normalized) };
  }, () => providerStop !== null));
  recordSourceCompleted("jll", "sale", { outcome: providerStop ? "failed" : "succeeded", listingsEmitted: rows.length });
} catch (error) {
  recordSourceCompleted("jll", "sale", { outcome: "failed" }); flushPerformance({ terminal: true }); throw error;
}
flushPerformance({ terminal: true });
writeFileSync(outputPath, JSON.stringify({ schema_version: 1, kind: "cre_jll_capacity_worker", worker_contract_sha256: workerContractSha256, generation, started_at: startedAt, finished_at: new Date().toISOString(), provider_stop: providerStop, rows }));
"""


def _worker_source(
    repo_root: Path, *, expected_details: int = 128, concurrency: int = 10
) -> str:
    jll = (repo_root / "scripts/firecrawl-ops/cre_collector/sources/jll.ts").as_uri()
    performance_module = (
        repo_root / "scripts/firecrawl-ops/cre_collector/lib/performance.ts"
    ).as_uri()
    contract = _worker_contract(expected_details, concurrency)
    return (
        WORKER_TEMPLATE.replace("__JLL_IMPORT__", json.dumps(jll))
        .replace("__PERFORMANCE_IMPORT__", json.dumps(performance_module))
        .replace("__WORKER_SCHEDULER__", WORKER_SCHEDULER_JS)
        .replace("__EXPECTED_DETAILS__", str(expected_details))
        .replace("__EXPECTED_CONCURRENCY__", str(concurrency))
        .replace("__WORKER_CONTRACT_SHA256__", json.dumps(contract["sha256"]))
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


def _signal_as_interrupt(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"benchmark interrupted by signal {signum}")


@contextmanager
def _benchmark_signal_handlers():
    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _signal_as_interrupt)
    except ValueError as exc:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise BenchmarkError("benchmark must run in the main process thread") from exc
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_worker(
    *,
    repo_root: Path,
    sample_path: Path,
    replicate_dir: Path,
    requested: Mapping[str, Any],
    api_url: str,
    timeout_seconds: int,
    expected_details: int,
    review_grant: Mapping[str, Any] | None = None,
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
            "CRE_SCRAPE_MAX_ATTEMPTS": "1",
            "JLL_GRAPHQL_RETRIES": "1",
            "JLL_DETAIL_WAIT_MS": "1000",
            "JLL_DETAIL_FALLBACK_WAIT_MS": "1000",
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
    pending_exception: BaseException | None = None
    try:
        if review_grant is not None:
            _validate_review_grant_freshness(review_grant)
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
    except KeyboardInterrupt as exc:
        if process is None:
            raise
        termination_reason = "operator_interrupt"
        monitor_error = type(exc).__name__
        pending_exception = exc
        _terminate(process)
    except (BenchmarkError, OSError) as exc:
        if process is None:
            raise BenchmarkError("benchmark worker could not be started") from exc
        termination_reason = "monitor_telemetry_failure"
        monitor_error = type(exc).__name__
        _terminate(process)
    except BaseException as exc:
        if process is None:
            raise
        termination_reason = "worker_monitor_unexpected_failure"
        monitor_error = type(exc).__name__
        pending_exception = exc
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
    if pending_exception is not None:
        raise pending_exception
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


def _missing_structural_fields(
    historic: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    missing: list[str] = []
    historic_fields = historic.get("fields")
    current_fields = current.get("fields")
    if not isinstance(historic_fields, Mapping) or not isinstance(
        current_fields, Mapping
    ):
        return ["structural_evidence"]
    for channel, fields in historic_fields.items():
        observed = current_fields.get(channel)
        if not isinstance(fields, Mapping) or not isinstance(observed, Mapping):
            missing.append(str(channel))
            continue
        missing.extend(
            f"{channel}.{key}"
            for key, present in fields.items()
            if present is True and observed.get(key) is not True
        )
    return sorted(missing)


def _performance_telemetry_complete(
    value: Any, *, expected_details: int, concurrency: int
) -> bool:
    if (
        not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value.get("kind") != "cre_scrape_performance"
        or value.get("terminal") is not True
        or value.get("degraded") is not False
    ):
        return False
    metrics = value.get("metrics")
    requests = metrics.get("requests") if isinstance(metrics, Mapping) else None
    if not isinstance(requests, Mapping):
        return False
    logical = metrics.get("logical_scrape_calls")
    retry = requests.get("retry")
    errors = requests.get("error_categories")
    by_source = requests.get("by_source_transaction")
    exact_source = {
        "source": "jll",
        "transaction": "sale",
        "attempts_started": expected_details,
        "attempts_completed": expected_details,
        "succeeded": expected_details,
        "failed": 0,
        "fresh_requested": expected_details,
    }
    exact_errors = {
        "timeout": 0,
        "http_4xx": 0,
        "http_5xx": 0,
        "transport": 0,
        "empty_response": 0,
        "unknown": 0,
    }
    request_count_keys = (
        "attempts_started",
        "attempts_completed",
        "succeeded",
        "failed",
        "fresh_requested",
        "active_locally_awaited",
        "max_active_locally_awaited",
        "timed_out_remote_settlement_unknown",
        "other_valid_statuses",
    )
    retry_counts_are_ints = isinstance(retry, Mapping) and all(
        isinstance(counts, Mapping)
        and all(type(count) is int for count in counts.values())
        for counts in retry.values()
    )
    source_counts_are_ints = (
        isinstance(by_source, list)
        and len(by_source) == 1
        and isinstance(by_source[0], Mapping)
        and all(
            type(by_source[0].get(key)) is int
            for key in (
                "attempts_started",
                "attempts_completed",
                "succeeded",
                "failed",
                "fresh_requested",
            )
        )
    )
    return (
        _same_typed_value(logical, {"raw": 0, "doc": expected_details, "json": 0})
        and all(type(requests.get(key)) is int for key in request_count_keys)
        and requests.get("attempts_started") == expected_details
        and requests.get("attempts_completed") == expected_details
        and requests.get("succeeded") == expected_details
        and requests.get("failed") == 0
        and requests.get("fresh_requested") == expected_details
        and requests.get("active_locally_awaited") == 0
        and type(requests.get("max_active_locally_awaited")) is int
        and 1 <= requests["max_active_locally_awaited"] <= concurrency
        and requests.get("timed_out_remote_settlement_unknown") == 0
        and requests.get("status_counts") == {}
        and requests.get("other_valid_statuses") == 0
        and retry
        == {
            "http_helper": {
                "retry_attempts": 0,
                "backoff_ms": 0,
                "terminal_backoff_ms": 0,
            },
            "json_parse": {
                "retry_attempts": 0,
                "backoff_ms": 0,
                "terminal_backoff_ms": 0,
            },
        }
        and retry_counts_are_ints
        and _same_typed_value(errors, exact_errors)
        and source_counts_are_ints
        and _same_typed_value(by_source, [exact_source])
    )


def summarize_replicate(
    replicate_dir: Path,
    sample: Mapping[str, Any],
    wall_seconds: float,
    *,
    sample_canonical_sha256: str,
    worker_contract: Mapping[str, Any],
) -> dict[str, Any]:
    worker_path = replicate_dir / "worker-output.json"
    worker = _read_json(worker_path, MAX_WORKER_OUTPUT_BYTES)
    performance = _read_json(replicate_dir / "performance.json")
    if (
        not isinstance(worker, dict)
        or type(worker.get("schema_version")) is not int
        or worker["schema_version"] != SCHEMA_VERSION
        or worker.get("kind") != "cre_jll_capacity_worker"
        or worker.get("worker_contract_sha256") != worker_contract.get("sha256")
        or not _is_sha256(sample_canonical_sha256)
    ):
        raise BenchmarkError("worker output contract is invalid")
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
    native_value_matches = 0
    fidelity_matches = 0
    fidelity_drops: list[dict[str, Any]] = []
    freshness_matches = 0
    record_evidence: list[dict[str, Any]] = []
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
        identity_match = identity == (expected["id"], expected["url"])
        if not identity_match:
            errors.append({"sample_id": expected["sample_id"], "kind": "identity"})
        expected_transaction_type = _transaction_type(expected["transaction_class"])
        transaction_type = normalized.get("transactionType")
        transaction_match = (
            actual.get("transaction_type") == expected_transaction_type
            and transaction_type == expected_transaction_type
        )
        if not transaction_match:
            errors.append(
                {
                    "sample_id": expected["sample_id"],
                    "kind": "transaction_type",
                    "expected": expected_transaction_type,
                }
            )
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
        freshness_match = (
            provenance.get("cacheDisposition") == "live"
            and provenance.get("generationId") == worker_generation
            and observed_ms is not None
            and worker_started_ms is not None
            and observed_ms >= worker_started_ms
        )
        if freshness_match:
            freshness_matches += 1
        else:
            errors.append({"sample_id": expected["sample_id"], "kind": "freshness"})
        current_native = actual.get("native") if isinstance(actual, dict) else None
        historic_native = expected.get("historic", {}).get("native")
        native_missing = []
        native_shape_valid = False
        if isinstance(current_native, dict) and isinstance(historic_native, dict):
            current_counts = current_native.get("counts")
            historic_counts = historic_native.get("counts")
            if isinstance(current_counts, dict) and isinstance(historic_counts, dict):
                native_shape_valid = True
                native_missing = sorted(
                    channel
                    for channel, count in historic_counts.items()
                    if type(count) is int
                    and count > 0
                    and not (
                        type(current_counts.get(channel)) is int
                        and current_counts[channel] > 0
                    )
                )
        native_complete = native_shape_valid and not native_missing
        if native_complete:
            native_matches += 1
        else:
            errors.append(
                {
                    "sample_id": expected["sample_id"],
                    "kind": "native_asset_channel_drop",
                    "fields": native_missing or ["native_evidence"],
                }
            )
        if (
            isinstance(current_native, dict)
            and isinstance(historic_native, dict)
            and (
                current_native.get("fingerprints")
                == historic_native.get("fingerprints")
            )
        ):
            native_value_matches += 1
        current_fidelity = actual.get("fidelity")
        historic_fidelity = expected.get("historic", {}).get("fidelity")
        missing_fields = (
            _missing_structural_fields(historic_fidelity, current_fidelity)
            if isinstance(historic_fidelity, Mapping)
            and isinstance(current_fidelity, Mapping)
            and _valid_structural_fidelity(current_fidelity)
            else ["structural_evidence"]
        )
        structural_complete = not missing_fields
        if not structural_complete:
            fidelity_drops.append(
                {"sample_id": expected["sample_id"], "fields": missing_fields}
            )
            errors.append(
                {
                    "sample_id": expected["sample_id"],
                    "kind": "normalized_supported_field_drop",
                    "fields": missing_fields,
                }
            )
        else:
            fidelity_matches += 1
        record_evidence.append(
            {
                "sample_index": index,
                "sample_id": expected["sample_id"],
                "identity_match": identity_match,
                "identity_sha256": _sha256(_canonical(identity)),
                "freshness_match": freshness_match,
                "native_complete": native_complete,
                "native_evidence_sha256": _sha256(_canonical(current_native)),
                "structural_complete": structural_complete,
                "structural_evidence_sha256": _sha256(_canonical(current_fidelity)),
                "transaction_type": transaction_type,
                "transaction_type_match": transaction_match,
            }
        )
    requests = performance.get("metrics", {}).get("requests", {})
    telemetry_complete = _performance_telemetry_complete(
        performance,
        expected_details=len(expected_rows),
        concurrency=int(worker_contract["concurrency"]),
    )
    remote_unknown = requests.get("timed_out_remote_settlement_unknown")
    if type(remote_unknown) is not int:
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
    sample_ids = [entry["sample_id"] for entry in record_evidence]
    records_sha256 = _sha256(_canonical(record_evidence))
    worker_output_sha256 = _file_sha256(worker_path)
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
        "historic_native_value_matches": native_value_matches,
        "normalized_structural_matches": fidelity_matches,
        "normalized_structural_drops": fidelity_drops,
        "quality_errors": errors,
        "performance": performance,
        "performance_telemetry_complete": telemetry_complete,
        "worker_contract_sha256": worker_contract["sha256"],
        "worker_output_sha256": worker_output_sha256,
        "record_evidence": record_evidence,
        "record_evidence_manifest": {
            "schema_version": SCHEMA_VERSION,
            "kind": "cre_jll_capacity_record_evidence",
            "sample_canonical_sha256": sample_canonical_sha256,
            "worker_output_sha256": worker_output_sha256,
            "record_count": len(record_evidence),
            "sample_ids_sha256": _sha256(_canonical(sample_ids)),
            "records_sha256": records_sha256,
        },
        "remote_settlement_unknown": remote_unknown,
        "provider_cooldown": {
            "required": bool(provider_signals),
            "signals": sorted(set(provider_signals)),
            "resume": "fresh_operator_admission_required",
        },
        "comparison_state": (
            "measured"
            if not errors and telemetry_complete
            else "quality_failed"
            if errors
            else "inconclusive"
        ),
    }


def _valid_settlement(value: Any, *, final: bool) -> bool:
    if not isinstance(value, Mapping) or value.get("idle") is not True:
        return False
    expected_keys = {
        "queue",
        "browser_active_pages",
        "active_crawls",
        "rabbitmq_queue_count",
        "rabbitmq_ready",
        "rabbitmq_unacknowledged",
        "nuq",
        "idle",
        "observed_at",
    }
    if final:
        expected_keys.update({"state", "polls", "observations"})
    if set(value) != expected_keys:
        return False
    queue = value.get("queue")
    nuq = value.get("nuq")
    if (
        not isinstance(queue, Mapping)
        or set(queue) != {"active", "waiting", "total"}
        or any(type(queue.get(key)) is not int or queue[key] != 0 for key in queue)
        or type(value.get("browser_active_pages")) is not int
        or value["browser_active_pages"] != 0
        or type(value.get("active_crawls")) is not int
        or value["active_crawls"] != 0
        or type(value.get("rabbitmq_queue_count")) is not int
        or value["rabbitmq_queue_count"] <= 0
        or type(value.get("rabbitmq_ready")) is not int
        or value["rabbitmq_ready"] != 0
        or type(value.get("rabbitmq_unacknowledged")) is not int
        or value["rabbitmq_unacknowledged"] != 0
        or not isinstance(nuq, Mapping)
        or set(nuq)
        != {
            "queue_scrape_total",
            "queue_scrape_backlog_total",
            "queue_crawl_finished_total",
        }
        or any(type(count) is not int or count != 0 for count in nuq.values())
        or not isinstance(value.get("observed_at"), str)
    ):
        return False
    if final:
        observations = value.get("observations")
        return (
            value.get("state") == "idle"
            and type(value.get("polls")) is int
            and value["polls"] >= 1
            and isinstance(observations, list)
            and len(observations) == value["polls"]
            and bool(observations)
            and _valid_settlement(observations[-1], final=False)
        )
    return True


def _valid_resource_snapshot(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or value.get("complete") is not True
        or not isinstance(value.get("observed_at"), str)
    ):
        return False
    required_values = {
        "memory_current",
        "memory_peak",
        "pids_current",
        "pids_peak",
        "pids_max",
        "memory_events",
        "pids_events",
        "cpu_stat",
    }
    for label in ("api", "browser"):
        current = value.get(label)
        if not isinstance(current, Mapping) or set(current) != required_values:
            return False
        for key in ("memory_current", "memory_peak", "pids_current", "pids_peak"):
            if type(current.get(key)) is not int or current[key] < 0:
                return False
        if (
            type(current.get("pids_max")) is not int
            and current.get("pids_max") != "max"
        ):
            return False
        for key in ("memory_events", "pids_events", "cpu_stat"):
            counters = current.get(key)
            if (
                not isinstance(counters, Mapping)
                or not counters
                or any(
                    not isinstance(name, str) or type(count) is not int or count < 0
                    for name, count in counters.items()
                )
            ):
                return False
    return True


def _valid_host_samples(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, Mapping)
            and set(item) == {"observed_at", "host_cpu_percent"}
            and isinstance(item.get("observed_at"), str)
            and type(item.get("host_cpu_percent")) in {int, float}
            and 0 <= item["host_cpu_percent"] <= 100
            for item in value
        )
    )


def _valid_guard_telemetry(
    value: Any,
    *,
    host_samples: Any,
    worker_source_sha256: Any,
) -> bool:
    required = {
        "samples",
        "triggered",
        "termination_reason",
        "monitor_error_type",
        "worker_exit_code",
        "stderr_sha256",
        "stderr_bytes",
        "stderr_file",
        "worker_source_sha256",
    }
    return bool(
        isinstance(value, Mapping)
        and set(value) == required
        and value.get("triggered") is False
        and value.get("termination_reason") is None
        and value.get("monitor_error_type") is None
        and value.get("worker_exit_code") == 0
        and value.get("samples") == host_samples
        and _valid_host_samples(value.get("samples"))
        and _is_sha256(value.get("stderr_sha256"))
        and type(value.get("stderr_bytes")) is int
        and value["stderr_bytes"] >= 0
        and isinstance(value.get("stderr_file"), str)
        and bool(value["stderr_file"])
        and value.get("worker_source_sha256") == worker_source_sha256
        and _is_sha256(worker_source_sha256)
    )


def _valid_implementation_manifest(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "kind", "files", "sha256"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value.get("kind") != "cre_capacity_benchmark_implementation"
    ):
        return False
    files = value.get("files")
    return (
        isinstance(files, Mapping)
        and set(files) == set(IMPLEMENTATION_PATHS)
        and all(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in files.values()
        )
        and value.get("sha256") == _sha256(_canonical(files))
    )


def _valid_worker_contract(
    value: Any, *, expected_details: int, expected_concurrency: int
) -> bool:
    return bool(
        isinstance(value, Mapping)
        and type(value.get("schema_version")) is int
        and value["schema_version"] == SCHEMA_VERSION
        and type(value.get("details")) is int
        and type(value.get("concurrency")) is int
        and _same_typed_value(
            value, _worker_contract(expected_details, expected_concurrency)
        )
    )


def _valid_record_evidence(
    replicate: Mapping[str, Any],
    *,
    details: int,
    sample_canonical_sha256: str,
    worker_contract_sha256: str,
) -> bool:
    records = replicate.get("record_evidence")
    manifest = replicate.get("record_evidence_manifest")
    if (
        not isinstance(records, list)
        or len(records) != details
        or not isinstance(manifest, Mapping)
        or set(manifest)
        != {
            "schema_version",
            "kind",
            "sample_canonical_sha256",
            "worker_output_sha256",
            "record_count",
            "sample_ids_sha256",
            "records_sha256",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest.get("kind") != "cre_jll_capacity_record_evidence"
        or manifest.get("sample_canonical_sha256") != sample_canonical_sha256
        or manifest.get("record_count") != details
        or not _is_sha256(replicate.get("worker_output_sha256"))
        or manifest.get("worker_output_sha256") != replicate.get("worker_output_sha256")
        or replicate.get("worker_contract_sha256") != worker_contract_sha256
    ):
        return False
    sample_ids: list[str] = []
    required = {
        "sample_index",
        "sample_id",
        "identity_match",
        "identity_sha256",
        "freshness_match",
        "native_complete",
        "native_evidence_sha256",
        "structural_complete",
        "structural_evidence_sha256",
        "transaction_type",
        "transaction_type_match",
    }
    for index, record in enumerate(records):
        if (
            not isinstance(record, Mapping)
            or set(record) != required
            or type(record.get("sample_index")) is not int
            or record.get("sample_index") != index
            or not isinstance(record.get("sample_id"), str)
            or not re.fullmatch(r"[0-9a-f]{24}", record["sample_id"])
            or any(
                record.get(key) is not True
                for key in (
                    "identity_match",
                    "freshness_match",
                    "native_complete",
                    "structural_complete",
                    "transaction_type_match",
                )
            )
            or record.get("transaction_type") not in {"Sale", "Lease"}
            or any(
                not _is_sha256(record.get(key))
                for key in (
                    "identity_sha256",
                    "native_evidence_sha256",
                    "structural_evidence_sha256",
                )
            )
        ):
            return False
        sample_ids.append(record["sample_id"])
    return bool(
        len(set(sample_ids)) == details
        and manifest.get("sample_ids_sha256") == _sha256(_canonical(sample_ids))
        and manifest.get("records_sha256") == _sha256(_canonical(records))
    )


def _comparison_evidence(
    value: Any, label: str, contract: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if not isinstance(value, dict):
        return None, [f"{label}_result_not_object"]
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value.get("kind") != RESULT_KIND
        or value.get("mode") != "run"
    ):
        reasons.append(f"{label}_result_kind")
    if (
        value.get("completed") is not True
        or value.get("comparison_state") != "complete"
    ):
        reasons.append(f"{label}_not_complete")
    workload = contract["workload"]
    details = int(workload["details"])
    replicate_count = int(workload["replicates"])
    if not _same_typed_value(value.get("workload"), workload):
        reasons.append(f"{label}_workload")
    if value.get("profile") != contract["profiles"][label]:
        reasons.append(f"{label}_profile")
    requested = contract["requested"][label]
    if not _same_typed_value(value.get("requested"), requested):
        reasons.append(f"{label}_requested")
    if value.get("config_sha256") != contract["config_sha256"]:
        reasons.append(f"{label}_config")
    expected_worker = _worker_contract(details, requested["jll_detail_concurrency"])
    if not _valid_worker_contract(
        value.get("worker_contract"),
        expected_details=details,
        expected_concurrency=requested["jll_detail_concurrency"],
    ):
        reasons.append(f"{label}_worker_contract")
    if not _valid_effective_runtime(value.get("effective_runtime"), requested):
        reasons.append(f"{label}_effective_runtime")
    if not _valid_live_admission(
        value.get("live_admission"),
        effective_runtime=value.get("effective_runtime"),
        expected_requested=requested,
    ):
        reasons.append(f"{label}_live_admission")
    replicates = value.get("replicates")
    if not isinstance(replicates, list) or len(replicates) != replicate_count:
        reasons.append(f"{label}_replicate_count")
        return None, reasons
    throughput: list[float] = []
    latency: list[dict[str, Any]] = []
    qualified_rows: list[int] = []
    freshness_rows: list[int] = []
    native_matches: list[int] = []
    structural_matches: list[int] = []
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
            or replicate.get("normalized_structural_matches") != details
            or replicate.get("normalized_structural_drops") != []
            or replicate.get("quality_errors") != []
            or replicate.get("performance_telemetry_complete") is not True
        ):
            reasons.append(f"{prefix}_quality")
        if type(replicate.get("qualified_fresh_unique_rows")) is int:
            qualified_rows.append(replicate["qualified_fresh_unique_rows"])
        if type(replicate.get("freshness_matches")) is int:
            freshness_rows.append(replicate["freshness_matches"])
        if type(replicate.get("historic_native_asset_matches")) is int:
            native_matches.append(replicate["historic_native_asset_matches"])
        if type(replicate.get("normalized_structural_matches")) is int:
            structural_matches.append(replicate["normalized_structural_matches"])
        if (
            replicate.get("resource_verdict", {}).get("state") != "measured"
            or not _valid_resource_snapshot(replicate.get("resources_before"))
            or not _valid_resource_snapshot(replicate.get("resources_after"))
        ):
            reasons.append(f"{prefix}_resources")
        if (
            replicate.get("source_owned_settlement") != "locally_awaited_terminal"
            or not _valid_settlement(replicate.get("settlement_before"), final=False)
            or not _valid_settlement(replicate.get("settlement_after"), final=True)
            or replicate.get("remote_settlement_unknown") != 0
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
        if not _valid_host_samples(replicate.get("host_samples")):
            reasons.append(f"{prefix}_host_telemetry")
        if not _valid_guard_telemetry(
            replicate.get("guard_telemetry"),
            host_samples=replicate.get("host_samples"),
            worker_source_sha256=value.get("worker_source_sha256"),
        ):
            reasons.append(f"{prefix}_guard_telemetry")
        if not _performance_telemetry_complete(
            replicate.get("performance"),
            expected_details=details,
            concurrency=requested["jll_detail_concurrency"],
        ):
            reasons.append(f"{prefix}_performance_telemetry")
        if not _valid_record_evidence(
            replicate,
            details=details,
            sample_canonical_sha256=value.get("sample_canonical_sha256", ""),
            worker_contract_sha256=expected_worker["sha256"],
        ):
            reasons.append(f"{prefix}_record_evidence")
        latency_value = replicate.get("latency_ms")
        if not isinstance(latency_value, dict) or any(
            type(latency_value.get(key)) not in {int, float}
            for key in ("p50", "p95", "p99")
        ):
            reasons.append(f"{prefix}_latency")
        else:
            latency.append(latency_value)
    required_sha256 = (
        "sample_inventory_sha256",
        "sample_manifest_sha256",
        "sample_canonical_sha256",
        "worker_source_sha256",
        "config_sha256",
        "admission_sha256",
        "review_approval_nonce_sha256",
        "admission_consumption_sha256",
        "review_benchmark_grant_sha256",
    )
    if any(
        not _is_sha256(value.get(key)) for key in required_sha256
    ) or not _is_source_sha(value.get("source_git_sha")):
        reasons.append(f"{label}_provenance")
    if not _same_typed_value(value.get("freshness_policy"), EXPECTED_FRESHNESS_POLICY):
        reasons.append(f"{label}_freshness_policy")
    if not _valid_implementation_manifest(value.get("implementation_manifest")):
        reasons.append(f"{label}_implementation_manifest")
    if value.get("shared_lock", {}).get("canonical") is not True:
        reasons.append(f"{label}_shared_lock")
    if not _valid_settlement(value.get("final_settlement"), final=True):
        reasons.append(f"{label}_final_settlement")
    if (
        value.get("safety", {}).get("database_writes") != 0
        or value.get("safety", {}).get("canonical_cache_writes") != 0
        or value.get("safety", {}).get("provider_retry_policy")
        != {
            "jll_graphql_attempts": 1,
            "jll_detail_fallback": "disabled",
            "shared_scrape_helper_attempts": 1,
        }
        or value.get("safety", {}).get("cancellation_limitation")
        != "no_supported_scrape_job_cancel_endpoint_or_job_ids; idle_settlement_required_before_lock_release"
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
            "normalized_structural_matches_per_replicate": structural_matches,
            "native_asset_deltas_per_replicate": [0, 0, 0],
            "all_quality_gates_passed": True,
        },
    }, []


def compare_results(baseline: Any, candidate: Any) -> dict[str, Any]:
    try:
        contract = _experiment_contract()
    except BenchmarkError:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "cre_jll_capacity_comparison",
            "state": "inconclusive",
            "decision": "no_adoption_decision",
            "reasons": ["central_experiment_configuration_unavailable"],
        }
    baseline_summary, baseline_reasons = _comparison_evidence(
        baseline, "baseline", contract
    )
    candidate_summary, candidate_reasons = _comparison_evidence(
        candidate, "candidate", contract
    )
    reasons = baseline_reasons + candidate_reasons
    if not SUPPORTED_BASELINE_ADMISSION_AVAILABLE:
        reasons.append("supported_baseline_admission_unavailable")
    match_fields = (
        "sample_inventory_sha256",
        "sample_manifest_sha256",
        "sample_canonical_sha256",
        "implementation_manifest",
        "config_sha256",
        "freshness_policy",
        "workload",
    )
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        reasons.extend(
            f"mismatch_{key}"
            for key in match_fields
            if baseline.get(key) != candidate.get(key)
        )
        baseline_runtime = baseline.get("effective_runtime")
        candidate_runtime = candidate.get("effective_runtime")
        baseline_identity = (
            baseline_runtime.get("identity")
            if isinstance(baseline_runtime, Mapping)
            else None
        )
        candidate_identity = (
            candidate_runtime.get("identity")
            if isinstance(candidate_runtime, Mapping)
            else None
        )
        if baseline_identity != candidate_identity:
            reasons.append("mismatch_effective_runtime_identity")
        baseline_ids = [
            row.get("record_evidence_manifest", {}).get("sample_ids_sha256")
            for row in baseline.get("replicates", [])
            if isinstance(row, Mapping)
        ]
        candidate_ids = [
            row.get("record_evidence_manifest", {}).get("sample_ids_sha256")
            for row in candidate.get("replicates", [])
            if isinstance(row, Mapping)
        ]
        if baseline_ids != candidate_ids:
            reasons.append("mismatch_record_sample_ids")
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


def _replicate_state(entry: Mapping[str, Any], details: int) -> str:
    failed = (
        entry.get("worker_exit_code") != 0
        or entry.get("termination_reason") is not None
        or entry.get("guard_triggered") is not False
        or entry.get("provider_cooldown", {}).get("required") is True
        or entry.get("comparison_state") == "quality_failed"
        or entry.get("resource_verdict", {}).get("state") == "failed"
        or entry.get("settlement_after", {}).get("state") == "unknown"
    )
    if failed:
        return "failed"
    measured = (
        entry.get("qualified_fresh_unique_rows") == details
        and entry.get("freshness_matches") == details
        and entry.get("historic_native_asset_matches") == details
        and entry.get("normalized_structural_matches") == details
        and entry.get("quality_errors") == []
        and entry.get("normalized_structural_drops") == []
        and entry.get("comparison_state") == "measured"
        and entry.get("performance_telemetry_complete") is True
        and _valid_host_samples(entry.get("host_samples"))
        and _valid_resource_snapshot(entry.get("resources_before"))
        and _valid_resource_snapshot(entry.get("resources_after"))
        and entry.get("resource_verdict", {}).get("state") == "measured"
        and _valid_settlement(entry.get("settlement_before"), final=False)
        and _valid_settlement(entry.get("settlement_after"), final=True)
        and entry.get("remote_settlement_unknown") == 0
        and entry.get("source_owned_settlement") == "locally_awaited_terminal"
    )
    return "measured" if measured else "inconclusive"


def _quarantine_shared_lock(
    lock: SharedLock,
    *,
    artifact_result_path: Path,
    admission_sha256: str,
    review_approval_nonce_sha256: str,
) -> dict[str, Any]:
    """Make an unknown-settlement lock non-reclaimable until operator recovery."""
    lock_path = lock.path
    if not lock.held or lock.lease_token is None or not lock_path.is_dir():
        raise BenchmarkError("canonical shared lock cannot be quarantined")
    evidence_path = lock_path / BENCHMARK_QUARANTINE_MARKER
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_capacity_benchmark_lock_quarantine",
        "state": "quarantined",
        "reason": "bounded_idle_settlement_not_proven",
        "quarantined_at": _now(),
        "admission_sha256": admission_sha256,
        "review_approval_nonce_sha256": review_approval_nonce_sha256,
        "result_path": str(artifact_result_path),
        "lock_path": str(lock_path),
        "recovery": {
            "automatic_reclaim": "disabled_missing_pid_and_lease",
            "required_evidence": "all_loopback_queue_browser_rabbitmq_nuq_and_active_crawl_counters_idle",
            "required_action": "operator_review_then_remove_this_exact_canonical_lock_directory",
        },
    }
    try:
        (lock_path / "pid").unlink()
        (lock_path / "lease").unlink()
        lock_path.chmod(0o700)
        _atomic_private_json(evidence_path, evidence)
    except (OSError, BenchmarkError) as exc:
        raise BenchmarkError("canonical shared lock quarantine failed") from exc
    return {
        "state": "quarantined",
        "evidence_path": str(evidence_path),
        "evidence_sha256": _file_sha256(evidence_path),
        "recovery": evidence["recovery"],
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
    admission_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    replicates = int(profile["workload"]["replicates"])
    implementation = _implementation_manifest(repo_root)
    endpoints = LOOPBACK_ENDPOINTS
    lock_path = canonical_shared_lock_dir(repo_root)
    try:
        with SharedLock(lock_path) as shared_lock:
            _verify_implementation_manifest(repo_root, implementation)
            locked_sample = validate_sample(
                _read_json(sample_path), int(profile["workload"]["details"])
            )
            if _canonical(locked_sample) != _canonical(sample):
                raise BenchmarkError("benchmark sample changed before lock acquisition")
            sample_provenance = verify_sample_provenance(locked_sample)
            sample_manifest_sha256 = _file_sha256(sample_path)
            sample_canonical_sha256 = _sha256(_canonical(locked_sample))
            live_admission = verify_live_admission(admission, profile)
            marker = _consume_admission(
                admission_path, admission, canonical_lock_path=lock_path
            )
            authorized_grant_timing = {
                "review_approval_created_at": admission["review_approval_created_at"],
                "expires_after_seconds": admission["expires_after_seconds"],
            }
            audit = _read_json(marker)
            worker_contract = _worker_contract(
                int(profile["workload"]["details"]),
                int(profile["requested"]["jll_detail_concurrency"]),
            )
            result: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "kind": RESULT_KIND,
                "mode": "run",
                "profile": profile_name,
                "config_sha256": config_sha256,
                "source_git_sha": admission["source_git_sha"],
                "implementation_manifest": implementation,
                "worker_source_sha256": _sha256(
                    _worker_source(
                        repo_root,
                        expected_details=int(profile["workload"]["details"]),
                        concurrency=int(profile["requested"]["jll_detail_concurrency"]),
                    ).encode()
                ),
                "worker_contract": worker_contract,
                "freshness_policy": EXPECTED_FRESHNESS_POLICY,
                "workload": profile["workload"],
                "requested": profile["requested"],
                "sample_inventory_sha256": sample["inventory_sha256"],
                "sample_manifest_sha256": sample_manifest_sha256,
                "sample_canonical_sha256": sample_canonical_sha256,
                "sample_provenance": sample_provenance,
                "admission_sha256": _sha256(_canonical(admission)),
                "review_approval_nonce_sha256": admission[
                    "review_approval_nonce_sha256"
                ],
                "admission_consumption_sha256": _file_sha256(marker),
                "review_benchmark_grant_sha256": audit["review_benchmark_grant_sha256"],
                "live_admission": live_admission,
                "effective_runtime": live_admission["effective_runtime"],
                "shared_lock": {"canonical": True, "path": str(lock_path)},
                "started_at": _now(),
                "replicates": [],
                "safety": {
                    "database_writes": 0,
                    "canonical_cache_writes": 0,
                    "raw_bodies": "retained_in_private_replicate_cache",
                    "admission": "single_use",
                    "review_grant": "same_user_atomic_rename_read_delete",
                    "provider_retry_policy": {
                        "jll_graphql_attempts": 1,
                        "jll_detail_fallback": "disabled",
                        "shared_scrape_helper_attempts": 1,
                    },
                    "cancellation_limitation": "no_supported_scrape_job_cancel_endpoint_or_job_ids; idle_settlement_required_before_lock_release",
                },
            }
            details = int(profile["workload"]["details"])
            worker_may_have_launched = False
            interlock_armed = False
            pending_error: BaseException | None = None
            try:
                with _benchmark_signal_handlers():
                    for number in range(1, replicates + 1):
                        _verify_implementation_manifest(repo_root, implementation)
                        if _file_sha256(sample_path) != sample_manifest_sha256:
                            raise BenchmarkError(
                                "benchmark sample changed between replicates"
                            )
                        replicate_dir = artifact_root / f"replicate-{number}"
                        before = _settlement_snapshot(
                            endpoints["api_url"], endpoints["browser_health_url"]
                        )
                        if not before["idle"]:
                            raise BenchmarkError(
                                "runtime was not idle before the replicate"
                            )
                        resources_before = _resource_snapshot()
                        started = time.monotonic()
                        if not interlock_armed:
                            _validate_review_grant_freshness(authorized_grant_timing)
                            shared_lock.arm_benchmark(
                                {
                                    "schema_version": SCHEMA_VERSION,
                                    "kind": "cre_capacity_benchmark_active",
                                    "state": "active",
                                    "armed_at": _now(),
                                    "pid": os.getpid(),
                                    "admission_sha256": result["admission_sha256"],
                                    "review_approval_nonce_sha256": result[
                                        "review_approval_nonce_sha256"
                                    ],
                                    "result_path": str(artifact_root / "result.json"),
                                }
                            )
                            interlock_armed = True
                        worker_may_have_launched = True
                        try:
                            code, host_samples, termination_reason = _run_worker(
                                repo_root=repo_root,
                                sample_path=sample_path,
                                replicate_dir=replicate_dir,
                                requested=profile["requested"],
                                api_url=endpoints["api_url"],
                                timeout_seconds=timeout_seconds,
                                expected_details=details,
                                review_grant=authorized_grant_timing
                                if number == 1
                                else None,
                            )
                        except BenchmarkError:
                            code, host_samples, termination_reason = (
                                -1,
                                [],
                                "worker_execution_failure",
                            )
                        wall = time.monotonic() - started
                        after = _await_idle_settlement(
                            endpoints["api_url"], endpoints["browser_health_url"]
                        )
                        resources_after = _resource_snapshot()
                        resource_verdict = _resource_verdict(
                            resources_before, resources_after, profile["requested"]
                        )
                        guard_path = replicate_dir / "guard.json"
                        try:
                            guard_telemetry = (
                                _read_json(guard_path) if guard_path.is_file() else None
                            )
                        except BenchmarkError:
                            guard_telemetry = None
                        entry: dict[str, Any] = {
                            "replicate": number,
                            "worker_exit_code": code,
                            "guard_triggered": termination_reason == "host_cpu_guard",
                            "termination_reason": termination_reason,
                            "host_samples": host_samples,
                            "guard_telemetry": guard_telemetry,
                            "settlement_before": before,
                            "settlement_after": after,
                            "settlement_error_type": after.get("error"),
                            "resources_before": resources_before,
                            "resources_after": resources_after,
                            "resource_verdict": resource_verdict,
                            "source_owned_settlement": "locally_awaited_terminal"
                            if after.get("state") == "idle"
                            else "unknown",
                        }
                        if code == 0 and termination_reason is None:
                            try:
                                summary = summarize_replicate(
                                    replicate_dir,
                                    locked_sample,
                                    wall,
                                    sample_canonical_sha256=sample_canonical_sha256,
                                    worker_contract=worker_contract,
                                )
                            except (BenchmarkError, KeyError, TypeError, ValueError):
                                summary = {
                                    "comparison_state": "inconclusive",
                                    "performance_telemetry_complete": False,
                                    "provider_cooldown": {
                                        "required": False,
                                        "signals": [],
                                        "resume": "fresh_operator_admission_required",
                                    },
                                }
                            entry.update(summary)
                            if summary.get("remote_settlement_unknown") != 0:
                                entry["source_owned_settlement"] = "unknown"
                        entry["execution_state"] = _replicate_state(entry, details)
                        result["replicates"].append(entry)
                        _atomic_private_json(
                            artifact_root / "result.in-progress.json", result
                        )
                        if entry["execution_state"] != "measured":
                            result["stop_reason"] = (
                                "provider_cooldown_required"
                                if entry.get("provider_cooldown", {}).get("required")
                                is True
                                else "replicate_failed"
                                if entry["execution_state"] == "failed"
                                else "replicate_inconclusive"
                            )
                            break
            except BaseException as exc:  # noqa: BLE001 - cleanup must cover signals/exits
                pending_error = exc
                result["stop_reason"] = "interrupted_or_unexpected_failure"
                result["interruption_type"] = type(exc).__name__
            finally:
                if worker_may_have_launched:
                    try:
                        with _benchmark_signal_handlers():
                            final_settlement = _await_idle_settlement(
                                endpoints["api_url"],
                                endpoints["browser_health_url"],
                            )
                    except BaseException as cleanup_exc:  # noqa: BLE001 - persist unknown
                        final_settlement = {
                            "idle": False,
                            "state": "unknown",
                            "polls": 0,
                            "observed_at": _now(),
                            "observations": [],
                            "error": "final_settlement_cleanup_interrupted_or_failed",
                            "error_type": type(cleanup_exc).__name__,
                        }
                        if pending_error is None:
                            pending_error = cleanup_exc
                    result["final_settlement"] = final_settlement
                else:
                    result["final_settlement"] = {
                        "idle": False,
                        "state": "not_started",
                        "polls": 0,
                        "observed_at": _now(),
                        "observations": [],
                    }
                if result["final_settlement"].get("state") != "idle":
                    result["stop_reason"] = "final_settlement_unknown"
                states = [
                    row["execution_state"]
                    for row in result["replicates"]
                    if isinstance(row, Mapping) and "execution_state" in row
                ]
                result["finished_at"] = _now()
                result["completed"] = (
                    pending_error is None
                    and len(states) == replicates
                    and all(state == "measured" for state in states)
                    and result["final_settlement"].get("state") == "idle"
                )
                result["comparison_state"] = (
                    "complete"
                    if result["completed"]
                    else "failed"
                    if pending_error is not None
                    or "failed" in states
                    or result["final_settlement"].get("state") == "unknown"
                    else "inconclusive"
                )
                if (
                    worker_may_have_launched
                    and result["final_settlement"].get("state") != "idle"
                ):
                    try:
                        result["lock_quarantine"] = _quarantine_shared_lock(
                            shared_lock,
                            artifact_result_path=artifact_root / "result.json",
                            admission_sha256=result["admission_sha256"],
                            review_approval_nonce_sha256=result[
                                "review_approval_nonce_sha256"
                            ],
                        )
                    except BenchmarkError as quarantine_exc:
                        result["lock_quarantine"] = {
                            "state": "quarantine_evidence_unknown",
                            "error_type": type(quarantine_exc).__name__,
                        }
                        if pending_error is None:
                            pending_error = quarantine_exc
                _atomic_private_json(artifact_root / "result.json", result)
                if (
                    interlock_armed
                    and pending_error is None
                    and result["final_settlement"].get("state") == "idle"
                ):
                    shared_lock.disarm_benchmark()
            if pending_error is not None:
                raise pending_error
            return result
    except LockHeldError as exc:
        raise BenchmarkError("canonical CRE shared lock is already held") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
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
        contract = _experiment_contract()
        if args.config.resolve() != experiment.DEFAULT_CONFIG.resolve():
            raise BenchmarkError("benchmark configuration must be the central JSON")
        profile_name = args.profile or contract["profiles"]["candidate"]
        artifact_root = _private_artifact_root(args.artifact_root, repo_root)
        profile, digest = experiment.load_profile(args.config, profile_name)
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
        dry_plan = plan(profile, profile_name, digest, sample_value)
        _atomic_private_json(artifact_root / "plan.json", dry_plan)
        if not args.run:
            print(json.dumps(dry_plan, sort_keys=True, indent=2))
            return 0
        if not args.sample or not args.admission:
            raise BenchmarkError("--run requires --sample and --admission")
        admission = validate_admission(
            _read_json(args.admission),
            profile,
            profile_name,
            digest,
            source_git_sha=_git_head(repo_root),
        )
        result = run_benchmark(
            repo_root=repo_root,
            artifact_root=artifact_root,
            sample_path=args.sample.resolve(),
            sample=validate_sample(sample_value, int(profile["workload"]["details"])),
            profile=profile,
            profile_name=profile_name,
            config_sha256=digest,
            admission=admission,
            admission_path=args.admission,
            timeout_seconds=args.replicate_timeout_seconds,
        )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["completed"] else 75
    except (BenchmarkError, experiment.ProfileError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
