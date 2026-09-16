"""Repository-owned C10 admission authority and implementation fingerprints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import C10Error, require_sha256, sha256

SCHEMA_VERSION = 1
AUTHORITY_KIND = "cre_capacity_c10_v1_authority"
DEFAULT_AUTHORITY = Path(__file__).parent.parent / "cre_capacity_c10_authority_v1.json"
MAX_AUTHORITY_BYTES = 64 * 1024
PACKAGE_ROOT = Path(__file__).resolve().parent
COLLECTOR_ROOT = PACKAGE_ROOT.parent


def load_authority() -> dict[str, Any]:
    """Load the one repository-owned authority; callers cannot substitute it."""
    try:
        raw = DEFAULT_AUTHORITY.read_bytes()
    except OSError as exc:
        raise C10Error("cannot read canonical C10 authority") from exc
    if not raw or len(raw) > MAX_AUTHORITY_BYTES:
        raise C10Error("canonical C10 authority size is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C10Error("canonical C10 authority JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "approved_cohort_sha256",
        "approved_adapters",
    }:
        raise C10Error("canonical C10 authority schema is invalid")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != AUTHORITY_KIND
    ):
        raise C10Error("canonical C10 authority version is unsupported")
    cohort_sha256 = value.get("approved_cohort_sha256")
    adapters = value.get("approved_adapters")
    if not isinstance(adapters, dict) or any(
        not isinstance(key, str) or not key for key in adapters
    ):
        raise C10Error("canonical C10 adapter authority is invalid")
    for key, digest in adapters.items():
        require_sha256(digest, f"canonical C10 adapter authority {key}")
    if cohort_sha256 is None:
        if adapters:
            raise C10Error("C10 adapters cannot be approved without a cohort")
    else:
        require_sha256(cohort_sha256, "canonical C10 cohort authority")
    return {
        "approved_cohort_sha256": cohort_sha256,
        "approved_adapters": dict(adapters),
    }


def _implementation_files() -> tuple[Path, ...]:
    """Return every source file that can affect C10 verification semantics."""
    files = {
        path.resolve()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".ts"}
        and "__pycache__" not in path.parts
        and "tests" not in path.parts
    }
    pure_root = COLLECTOR_ROOT / "sources" / "pure"
    files.update(path.resolve() for path in pure_root.glob("*.ts") if path.is_file())
    files.update(
        {
            (COLLECTOR_ROOT / "cre_capacity_c10_v1.json").resolve(),
            (COLLECTOR_ROOT / "cre_capacity_c10_profiles_v1.json").resolve(),
        }
    )
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise C10Error("C10 implementation manifest contains an unsafe path")
    return tuple(sorted(files, key=lambda path: path.as_posix()))


def implementation_manifest() -> Mapping[str, str]:
    """Hash actual verifier bytes and shared dependencies, not version labels."""
    manifest: dict[str, str] = {}
    for path in _implementation_files():
        try:
            relative = path.relative_to(COLLECTOR_ROOT).as_posix()
            payload = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise C10Error("cannot hash the C10 implementation manifest") from exc
        manifest[relative] = hashlib.sha256(payload).hexdigest()
    if not manifest:
        raise C10Error("C10 implementation manifest is empty")
    return manifest


def repository_implementation_sha256(adapter_key: str) -> str:
    """Bind one adapter approval to the complete current verifier source tree."""
    if not isinstance(adapter_key, str) or not adapter_key:
        raise C10Error("C10 adapter key is invalid for implementation hashing")
    return sha256({"adapter_key": adapter_key, "files": implementation_manifest()})
