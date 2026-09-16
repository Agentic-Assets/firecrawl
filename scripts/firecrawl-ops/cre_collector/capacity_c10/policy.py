"""Strict, versioned C10 policy loading with no collector-registry fallback."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    EXPECTED_PLANE_COUNTS,
    PLANES,
    C10Error,
    canonical_bytes,
    exact_source_keys,
    sha256,
)

SCHEMA_VERSION = 1
POLICY_KIND = "cre_capacity_c10_v1_policy"
DEFAULT_POLICY = Path(__file__).parent.parent / "cre_capacity_c10_v1.json"
MAX_POLICY_BYTES = 64 * 1024


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise C10Error(f"cannot read C10 policy {path}") from exc
    if not raw or len(raw) > MAX_POLICY_BYTES:
        raise C10Error("C10 policy size is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C10Error("C10 policy JSON is invalid") from exc
    if not isinstance(value, dict):
        raise C10Error("C10 policy root must be an object")
    return value


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    """Load the complete fixed C10 matrix; never infer it from SOURCE_KEYS."""
    raw = _read_json(path)
    if set(raw) != {"schema_version", "kind", "source_workers", "profiles", "sources"}:
        raise C10Error("C10 policy has an unexpected key set")
    if raw.get("schema_version") != SCHEMA_VERSION or raw.get("kind") != POLICY_KIND:
        raise C10Error("C10 policy schema is unsupported")
    if raw.get("source_workers") != 1:
        raise C10Error("C10 P0/P1 policy must remain serial")
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict) or profiles != {"p0": "c10-p0", "p1": "c10-p1"}:
        raise C10Error("C10 profile binding is invalid")
    sources = raw.get("sources")
    if not isinstance(sources, list) or len(sources) != 20:
        raise C10Error("C10 policy must define exactly 20 sources")
    normalized: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise C10Error("C10 source policy must be an object")
        allowed = {"key", "plane", "family", "hosts"}
        if source.get("exclusive") is True:
            allowed.add("exclusive")
        if set(source) != allowed:
            raise C10Error("C10 source policy has an unexpected key set")
        key, plane, family, hosts = (
            source.get("key"),
            source.get("plane"),
            source.get("family"),
            source.get("hosts"),
        )
        if (
            not isinstance(key, str)
            or not isinstance(family, str)
            or plane not in PLANES
        ):
            raise C10Error("C10 source key, family, or plane is invalid")
        if (
            not isinstance(hosts, list)
            or not hosts
            or any(not isinstance(host, str) or not host for host in hosts)
        ):
            raise C10Error("C10 source hosts are invalid")
        if len(set(hosts)) != len(hosts):
            raise C10Error("C10 source hosts must be unique")
        normalized.append(
            {
                "key": key,
                "plane": plane,
                "family": family,
                "hosts": tuple(hosts),
                "exclusive": source.get("exclusive", False) is True,
            }
        )
    exact_source_keys(normalized)
    counts = Counter(source["plane"] for source in normalized)
    if dict(counts) != EXPECTED_PLANE_COUNTS:
        raise C10Error("C10 policy must preserve the 12/8 plane floor")
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": POLICY_KIND,
        "source_workers": 1,
        "profiles": profiles,
        "sources": normalized,
    }
    return {**document, "policy_sha256": sha256(document)}


def policy_document(policy: Mapping[str, Any]) -> bytes:
    """Expose the canonical non-derived policy bytes for fixture verification."""
    normalized = {key: value for key, value in policy.items() if key != "policy_sha256"}
    return canonical_bytes(normalized)
