"""Resolve and record a no-write CRE capacity experiment profile.

This planner never reads an environment file, contacts Docker, starts a
collector, or writes to a database.  It centralizes the requested settings and
creates a redaction-safe record that an admitted execution adapter must bind to
later.  The current checkpoint series is serial and is intentionally not used
as a substitute for the required 128-detail full-path no-write benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path(__file__).with_name("cre_capacity_experiment_profiles.json")
MAX_CONFIG_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
BINARY_GIBIBYTE = 1024**3
# Docker Compose's `8G` shared-memory setting is a binary 8 GiB limit when
# reported by Docker's HostConfig. Keep this contract centralized so an
# otherwise healthy rendered Compose baseline cannot fail runtime admission.
GOVERNED_BROWSER_SHM_BYTES = 8 * BINARY_GIBIBYTE
REQUIRED_RUNTIME = frozenset(
    {
        "orbstack_memory_mib",
        "minimum_docker_memtotal_basis_points",
        "browser_memory_bytes",
        "browser_swap_bytes",
        "api_memory_bytes",
        "api_swap_bytes",
        "browser_cpus",
        "global_pages",
        "browser_pids",
        "browser_shm_bytes",
        "api_cpus",
    }
)
REQUESTED_LIMITS = {
    "browser_cpus": (1, 16),
    "global_pages": (1, 32),
    "jll_detail_concurrency": (1, 16),
    "browser_pids": (64, 2048),
    "api_cpus": (1, 16),
    "host_cpu_guard_percent": (1, 99),
    "host_cpu_guard_seconds": (2, 600),
    "host_cpu_sample_seconds": (1, 60),
}
JLL_BENCHMARK_WORKLOAD = {
    "source": "jll",
    "details": 128,
    "replicates": 3,
    "writes": "forbidden",
}
C10_BASELINE_WORKLOAD = {
    "source": "multisource-c10",
    "details": "immutable_cohort",
    "replicates": 4,
    "writes": "forbidden",
}
C10_BASELINE_PLANNED = {
    "source_parallelism": "serial",
    "full_path_no_write_adapter": "capacity_c10",
    "provider_429_challenge_cooldown": "required-at-execution",
}
C10_EXPERIMENT_PLANNED = {
    "source_parallelism": "serial",
    "full_path_no_write_adapter": "capacity_c10",
    "provider_429_challenge_cooldown": "required-at-execution",
}


class ProfileError(ValueError):
    """The profile cannot safely be used."""


def _read_json(path: Path, *, maximum: int = MAX_CONFIG_BYTES) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read {path}") from exc
    if not raw or len(raw) > maximum:
        raise ProfileError(f"invalid size for {path}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"JSON root in {path} must be an object")
    return value


def _strict_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ProfileError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{name} must be an object")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def load_profile(path: Path, profile_name: str) -> tuple[dict[str, Any], str]:
    document = _read_json(path)
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ProfileError("unsupported experiment profile schema")
    profiles = _object(document.get("profiles"), "profiles")
    if (
        not isinstance(document.get("default_profile"), str)
        or document["default_profile"] not in profiles
    ):
        raise ProfileError("default_profile must name a configured profile")
    raw = profiles.get(profile_name)
    profile = dict(_object(raw, f"profile {profile_name}"))
    if profile.get("kind") not in {"baseline", "experiment"}:
        raise ProfileError("profile kind must be baseline or experiment")
    runtime = _object(profile.get("runtime_baseline"), "runtime_baseline")
    if set(runtime) != REQUIRED_RUNTIME:
        raise ProfileError("runtime_baseline has an unexpected key set")
    for key in REQUIRED_RUNTIME:
        _strict_int(runtime[key], key, 0, 2**63 - 1)
    if runtime["orbstack_memory_mib"] != 32768:
        raise ProfileError("OrbStack configured memory must be exactly 32768 MiB")
    if runtime["browser_shm_bytes"] != GOVERNED_BROWSER_SHM_BYTES:
        raise ProfileError(
            "browser shared memory must match the governed Compose 8 GiB baseline"
        )
    if not 1 <= runtime["minimum_docker_memtotal_basis_points"] <= 10000:
        raise ProfileError(
            "minimum Docker memory basis points must be between 1 and 10000"
        )
    requested = _object(profile.get("requested"), "requested")
    if set(requested) != set(REQUESTED_LIMITS):
        raise ProfileError("requested settings have an unexpected key set")
    normalized_requested = {
        key: _strict_int(requested[key], key, *limits)
        for key, limits in REQUESTED_LIMITS.items()
    }
    if (
        normalized_requested["host_cpu_sample_seconds"]
        > normalized_requested["host_cpu_guard_seconds"]
    ):
        raise ProfileError("CPU sample interval cannot exceed guard duration")
    if (
        normalized_requested["jll_detail_concurrency"]
        > normalized_requested["global_pages"]
    ):
        raise ProfileError(
            "JLL detail concurrency cannot exceed the global page budget"
        )
    provider_budget = profile.get("provider_budget")
    if provider_budget is not None:
        budget = _object(provider_budget, "provider_budget")
        if set(budget) != {"global_pages", "later_two_provider_split"}:
            raise ProfileError("provider_budget has an unexpected key set")
        if budget["global_pages"] != normalized_requested["global_pages"]:
            raise ProfileError(
                "provider global page budget must match requested global pages"
            )
        split = budget["later_two_provider_split"]
        if not isinstance(split, list) or len(split) != 2:
            raise ProfileError("later provider split must contain exactly two values")
        if (
            sum(_strict_int(item, "later provider split", 1, 32) for item in split)
            != budget["global_pages"]
        ):
            raise ProfileError("later provider split must equal the global page budget")
    planned = _object(profile.get("planned"), "planned")
    workload = profile.get("workload")
    workload_value = _object(workload, "workload")
    is_jll_profile = workload_value == JLL_BENCHMARK_WORKLOAD
    is_c10_profile = workload_value == C10_BASELINE_WORKLOAD
    if not is_jll_profile and not is_c10_profile:
        raise ProfileError("capacity workload contract has an unexpected value")
    if is_jll_profile:
        expected_planned = (
            {
                "source_parallelism": "unimplemented",
                "full_path_no_write_adapter": "cre_capacity_benchmark",
                "provider_429_challenge_cooldown": "required-at-execution",
            }
            if profile["kind"] == "experiment"
            else {
                "source_parallelism": "serial",
                "full_path_no_write_adapter": "cre_capacity_benchmark",
                "provider_429_challenge_cooldown": "required-at-execution",
            }
        )
    else:
        expected_planned = (
            C10_EXPERIMENT_PLANNED
            if profile["kind"] == "experiment"
            else C10_BASELINE_PLANNED
        )
    if planned != expected_planned:
        raise ProfileError("capacity execution contract has an unexpected value")
    profile["runtime_baseline"] = dict(runtime)
    profile["requested"] = normalized_requested
    return profile, hashlib.sha256(_canonical(document)).hexdigest()


def _compare_effective(
    expected: Mapping[str, Any], supplied: Mapping[str, Any] | None
) -> dict[str, Any]:
    if supplied is None:
        return {"state": "unverified", "drift": [], "missing": sorted(expected)}
    unknown = sorted(set(supplied) - REQUIRED_RUNTIME)
    if unknown:
        raise ProfileError("effective settings contain unsupported keys")
    missing = sorted(set(expected) - set(supplied))
    drift = [
        {"field": key, "expected": expected[key], "actual": supplied[key]}
        for key in sorted(set(expected) & set(supplied))
        if type(supplied[key]) is not int or supplied[key] != expected[key]
    ]
    return {
        "state": "match" if not missing and not drift else "drift",
        "drift": drift,
        "missing": missing,
    }


def resolve(
    profile: Mapping[str, Any],
    profile_name: str,
    config_sha256: str,
    effective: Mapping[str, Any] | None,
) -> dict[str, Any]:
    runtime = _compare_effective(
        _object(profile["runtime_baseline"], "runtime_baseline"), effective
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cre_capacity_experiment_plan",
        "profile": profile_name,
        "profile_kind": profile["kind"],
        "config_sha256": config_sha256,
        "runtime_baseline_check": runtime,
        "requested": profile["requested"],
        "provider_budget": profile.get("provider_budget"),
        "workload": profile.get("workload"),
        "planned": profile["planned"],
        "execution": {
            "dry_run_only": True,
            "writes": "forbidden",
            # Both treatment arms issue the same external, no-write workload.
            # A baseline result is admissible only when it has the same fresh,
            # bound admission evidence as the candidate result.
            "technical_admission_required": True,
            "startable": False,
            "blockers": ["runtime_evidence_unverified"]
            if runtime["state"] == "unverified"
            else ["effective_runtime_drift"]
            if runtime["state"] == "drift"
            else ["technical_admission_required"],
        },
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _canonical(value) + b"\n"
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ProfileError("resolved settings record is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise ProfileError("settings record path is not a regular file")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.write(fd, encoded)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--effective-settings", type=Path)
    parser.add_argument("--write-plan", type=Path)
    args = parser.parse_args(argv)
    try:
        profile_name = args.profile
        if profile_name is None:
            configured_default = _read_json(args.config).get("default_profile")
            if not isinstance(configured_default, str):
                raise ProfileError("default_profile must be a string")
            profile_name = configured_default
        profile, digest = load_profile(args.config, profile_name)
        effective = (
            _read_json(args.effective_settings) if args.effective_settings else None
        )
        result = resolve(profile, profile_name, digest, effective)
        if args.write_plan:
            if result["runtime_baseline_check"]["state"] != "match":
                raise ProfileError(
                    "refusing an admissible settings record without matching runtime evidence"
                )
            _write_json(args.write_plan, result)
        print(json.dumps(result, sort_keys=True, indent=2))
    except ProfileError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
