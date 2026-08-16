#!/usr/bin/env python3
"""Plan and apply one guarded local Firecrawl model or OCR transition.

This is intentionally an operator handoff, not an agent-facing convenience
switch.  It is dry-run by default, uses only bounded loopback GETs before a
write, and records a body-free receipt.  A same-user process can always set an
environment variable, so the command line approval fields are an accountable
audit boundary rather than an authentication system.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_API_URL = "http://localhost:3002"
DEFAULT_ADAPTER_URL = "http://127.0.0.1:31337"
DEFAULT_DOCLING_URL = "http://127.0.0.1:5001"
DEFAULT_TIMEOUT_SECONDS = 2.0
RECEIPT_SCHEMA_VERSION = "firecrawl-operator-handoff-v1"
RECEIPT_DIR_RELATIVE = Path("tasks/tmp/firecrawl-operator-handoff")
MAX_RESPONSE_BYTES = 65_536
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9._:/#-]{1,160}$")
RECEIPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

MODEL_KEYS = (
    "OPENAI_BASE_URL",
    "MODEL_NAME",
    "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK",
)
OCR_ROUTING_KEYS = (
    "FIRE_PDF_ENABLE",
    "FIRE_PDF_PERCENT",
    "FIRE_PDF_BASE_URL",
    "PDF_RUST_EXTRACT_ENABLE",
    "MINERU_PERCENT",
)
TRANSITION_KEY_SETS = (MODEL_KEYS, OCR_ROUTING_KEYS)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_FINAL_STATES = {
    "planned",
    "retained",
    "restored",
    "restored-after-failure",
    "manual-handoff-required",
}
ALLOWED_OPERATORS = {"cayman", "stace"}
LIFECYCLE_ACTIONS = {"ensure", "restart", "stop"}
DOCLING_CONTAINER = "firecrawl-docling-serve"
ADAPTER_CONTAINER = "firecrawl-local-firepdf-adapter"
DOCLING_IMAGE = (
    "quay.io/docling-project/docling-serve-cpu@sha256:"
    "528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0"
)
MODEL_PROFILES: dict[str, dict[str, str]] = {
    "gateway": {
        "OPENAI_BASE_URL": "https://ai-gateway.vercel.sh/v1",
        "MODEL_NAME": "deepseek/deepseek-v4-flash-0731",
        "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK": "deepseek/deepseek-v4-pro-0813",
    },
    "gateway-pro": {
        "OPENAI_BASE_URL": "https://ai-gateway.vercel.sh/v1",
        "MODEL_NAME": "deepseek/deepseek-v4-pro-0813",
        "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK": "",
    },
    "gateway-codex": {
        "OPENAI_BASE_URL": "https://ai-gateway.vercel.sh/v1",
        "MODEL_NAME": "openai/gpt-5.4-mini",
        "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK": "",
    },
    "openai-direct": {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "MODEL_NAME": "gpt-5.4-mini",
        "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK": "",
    },
    "budget": {
        "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
        "MODEL_NAME": "deepseek/deepseek-v4-flash",
        "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK": "",
    },
    "escalated": {
        "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
        "MODEL_NAME": "deepseek/deepseek-v4-pro",
        "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK": "",
    },
}
OCR_ROUTING_MODES: dict[str, dict[str, str]] = {
    "local": {
        "FIRE_PDF_ENABLE": "true",
        "FIRE_PDF_PERCENT": "100",
        "FIRE_PDF_BASE_URL": "http://host.docker.internal:31337",
        "PDF_RUST_EXTRACT_ENABLE": "true",
        "MINERU_PERCENT": "0",
    },
    "off": {
        "FIRE_PDF_ENABLE": "false",
        "FIRE_PDF_PERCENT": "0",
        "FIRE_PDF_BASE_URL": "",
        "PDF_RUST_EXTRACT_ENABLE": "true",
        "MINERU_PERCENT": "0",
    },
}


class HandoffError(RuntimeError):
    """A fail-closed operator handoff refusal."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Fail redirects instead of allowing a loopback check to leave localhost."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def non_secret_digest(values: dict[str, str]) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def canonical_loopback_url(value: str, *, expected_port: int) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise HandoffError("api or adapter URL is malformed") from exc
    if (
        parts.scheme != "http"
        or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or port != expected_port
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise HandoffError(
            f"URL must be an http loopback origin on port {expected_port}"
        )
    host = "[::1]" if parts.hostname == "::1" else parts.hostname
    return f"http://{host}:{expected_port}"


def positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and between 0 and 10 seconds"
        ) from exc
    if not 0 < parsed <= 10:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and between 0 and 10 seconds"
        )
    return parsed


def checked_reference(value: str | None, *, label: str, required: bool) -> str | None:
    if value is None or not value:
        if required:
            raise HandoffError(f"--{label} is required for --apply")
        return None
    if not REFERENCE_PATTERN.fullmatch(value):
        raise HandoffError(
            f"--{label} must be a compact external reference, not a secret or free-form note"
        )
    return value


def ensure_no_secret_reference(value: str | None, *, label: str) -> str | None:
    value = checked_reference(value, label=label, required=False)
    if value is None:
        return None
    if any(
        fragment in value.lower()
        for fragment in ("openai_api_key", "fire_pdf_api_key", "bearer", "sk-")
    ):
        raise HandoffError(f"--{label} must not contain credential material")
    return value


class ReadOnlyLoopbackClient:
    """Small GET-only client with ambient proxies and redirects disabled."""

    def __init__(
        self, api_url: str, *, timeout_seconds: float, expected_port: int = 3002
    ) -> None:
        self.api_url = canonical_loopback_url(api_url, expected_port=expected_port)
        self.timeout_seconds = timeout_seconds
        self.proxy_handler = ProxyHandler({})
        self.opener = build_opener(self.proxy_handler, NoRedirectHandler())

    def get_json(self, path: str) -> dict[str, Any]:
        request = Request(
            f"{self.api_url}{path}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise HandoffError("loopback read returned an HTTP error")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise HandoffError(f"loopback read returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise HandoffError("loopback read is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise HandoffError("loopback read exceeded its body-free response limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffError("loopback read returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HandoffError("loopback read returned an invalid JSON shape")
        return payload

    def require_success_status(self, path: str) -> None:
        """Confirm a bounded loopback GET succeeds without retaining its body."""
        request = Request(f"{self.api_url}{path}", method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise HandoffError("loopback read returned an HTTP error")
        except HTTPError as exc:
            raise HandoffError(f"loopback read returned HTTP {exc.code}") from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise HandoffError("loopback read is unavailable") from exc


def nonnegative_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HandoffError(f"{label} is missing or invalid")
    return value


def idle_snapshot(client: ReadOnlyLoopbackClient) -> dict[str, int]:
    """Check identity, queue arithmetic, and active-crawl count without retaining bodies."""
    root = client.get_json("/")
    if root.get("message") != "Firecrawl API":
        raise HandoffError("loopback root identity did not match Firecrawl API")

    queue = client.get_json("/v2/team/queue-status")
    if queue.get("success") is not True:
        raise HandoffError("queue status was not successful")
    jobs = nonnegative_int(queue.get("jobsInQueue"), label="jobsInQueue")
    active = nonnegative_int(queue.get("activeJobsInQueue"), label="activeJobsInQueue")
    waiting = nonnegative_int(
        queue.get("waitingJobsInQueue"), label="waitingJobsInQueue"
    )
    if jobs != active + waiting:
        raise HandoffError("queue status arithmetic is inconsistent")
    if jobs != 0 or active != 0 or waiting != 0:
        raise HandoffError("queue is not idle")

    crawls = client.get_json("/v2/crawl/active")
    if crawls.get("success") is not True or not isinstance(crawls.get("crawls"), list):
        raise HandoffError("active crawl status was invalid")
    if crawls["crawls"]:
        raise HandoffError("active crawls are not idle")
    return {
        "jobs_in_queue": jobs,
        "active_jobs_in_queue": active,
        "waiting_jobs_in_queue": waiting,
        "active_crawl_count": 0,
    }


def adapter_snapshot(adapter: ReadOnlyLoopbackClient) -> dict[str, int | bool | str]:
    """Project only safe idle/capacity facts from the fixed-port OCR adapter."""
    settings = adapter.get_json("/settings")
    if settings.get("ok") is not True:
        raise HandoffError("OCR adapter settings were not successful")
    adapter_values = settings.get("adapter")
    profile = settings.get("profile")
    if not isinstance(adapter_values, dict) or not isinstance(profile, dict):
        raise HandoffError("OCR adapter settings were invalid")
    active = nonnegative_int(
        adapter_values.get("active_ocr"), label="adapter.active_ocr"
    )
    maximum = nonnegative_int(
        adapter_values.get("max_concurrent_ocr"), label="adapter.max_concurrent_ocr"
    )
    capture = profile.get("capture_docling_json")
    if active != 0 or maximum <= 0 or capture is not False:
        raise HandoffError("OCR adapter is not idle or permits raw capture")
    fingerprint = settings.get("settings_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", fingerprint
    ):
        raise HandoffError("OCR adapter settings fingerprint was invalid")
    return {
        "active_ocr": active,
        "max_concurrent_ocr": maximum,
        "settings_fingerprint": fingerprint,
    }


def read_env(path: Path) -> tuple[bytes, dict[str, str], dict[str, int]]:
    if not path.is_file() or path.is_symlink():
        raise HandoffError(f"local env file is missing or unsafe: {path}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError(
            "local env must be UTF-8 for a reversible transition"
        ) from exc
    values: dict[str, str] = {}
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        values[key] = value
        counts[key] = counts.get(key, 0) + 1
    return raw, values, counts


def require_reversible_keys(
    values: dict[str, str], counts: dict[str, int], keys: tuple[str, ...]
) -> dict[str, str]:
    missing = [key for key in keys if key not in values]
    duplicate = [key for key in keys if counts.get(key) != 1]
    if missing or duplicate:
        raise HandoffError(
            "the transition keys must each exist exactly once for a reversible handoff"
        )
    return {key: values[key] for key in keys}


def replace_env_values(raw: bytes, updates: dict[str, str]) -> bytes:
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise HandoffError("transition values must be single-line")
    text = raw.decode("utf-8")
    changed: set[str] = set()
    rendered: list[str] = []
    for line in text.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        key = body.split("=", 1)[0] if "=" in body else ""
        if key in updates:
            rendered.append(f"{key}={updates[key]}{ending}")
            changed.add(key)
        else:
            rendered.append(line)
    if changed != set(updates):
        raise HandoffError("refusing to append a missing transition key")
    return "".join(rendered).encode("utf-8")


def write_env_exact(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f".{path.name}.operator-handoff-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(raw)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def resolve_profile_capture(profile_name: str, profile_path: Path) -> bool:
    try:
        profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("OCR profile manifest is unavailable") from exc
    if not isinstance(profiles, dict):
        raise HandoffError("OCR profile manifest is invalid")

    def resolve(name: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in stack or not isinstance(profiles.get(name), dict):
            raise HandoffError("OCR profile is unknown or cyclic")
        current = dict(profiles[name])
        parent = current.get("extends")
        inherited = resolve(parent, (*stack, name)) if isinstance(parent, str) else {}
        inherited.update(current)
        return inherited

    profile = resolve(profile_name)
    return bool(profile.get("capture_docling_json"))


def receipt_path(receipt_dir: Path, receipt_id: str) -> Path:
    if not RECEIPT_ID_PATTERN.fullmatch(receipt_id):
        raise HandoffError("receipt id is invalid")
    return receipt_dir / f"{receipt_id}.json"


@dataclass(frozen=True)
class TransitionPaths:
    """Filesystem paths injected only by unit tests; the CLI always uses canonical paths."""

    repo_root: Path
    env_path: Path
    receipt_dir: Path


def resolve_scoped_transition_path(path: Path, *, scope_root: Path, label: str) -> Path:
    """Resolve a transition path only after rejecting links within its scope."""
    lexical_root = scope_root.absolute()
    lexical_path = path.absolute()
    try:
        relative_path = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise HandoffError(f"{label} must stay within its transition scope") from exc

    current = lexical_root
    for component in relative_path.parts:
        current /= component
        if current.is_symlink():
            raise HandoffError(f"{label} contains a symbolic link")

    canonical_root = lexical_root.resolve()
    canonical_path = lexical_path.resolve()
    try:
        canonical_path.relative_to(canonical_root)
    except ValueError as exc:  # pragma: no cover - guarded above, retained fail-closed
        raise HandoffError(f"{label} must stay within its transition scope") from exc
    return canonical_path


def canonical_transition_paths() -> TransitionPaths:
    return TransitionPaths(
        repo_root=REPO_ROOT,
        env_path=REPO_ROOT / ".env",
        receipt_dir=REPO_ROOT / RECEIPT_DIR_RELATIVE,
    )


def require_transition_key_set(keys: object) -> tuple[str, ...]:
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise HandoffError("receipt changed keys are invalid")
    sorted_keys = tuple(sorted(keys))
    for allowed in TRANSITION_KEY_SETS:
        if sorted_keys == tuple(sorted(allowed)):
            return allowed
    raise HandoffError("receipt changed keys are not an allowlisted transition set")


def require_nonsecret_value_map(
    value: object, keys: tuple[str, ...], *, label: str
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise HandoffError(f"receipt {label} is not an exact allowlisted value map")
    if not all(isinstance(item, str) for item in value.values()):
        raise HandoffError(f"receipt {label} contains a non-string value")
    return {key: value[key] for key in keys}


def require_digest(
    value: object, *, label: str, allow_none: bool = False
) -> str | None:
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise HandoffError(f"receipt {label} is invalid")
    return value


def require_snapshot(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {
        "jobs_in_queue",
        "active_jobs_in_queue",
        "waiting_jobs_in_queue",
        "active_crawl_count",
    }:
        raise HandoffError("receipt queue snapshot is invalid")
    return {key: nonnegative_int(value[key], label=f"receipt.{key}") for key in value}


def require_adapter_receipt(value: object) -> dict[str, int | str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "active_ocr",
        "max_concurrent_ocr",
        "settings_fingerprint",
    }:
        raise HandoffError("receipt OCR adapter state is invalid")
    active = nonnegative_int(value["active_ocr"], label="receipt.active_ocr")
    maximum = nonnegative_int(
        value["max_concurrent_ocr"], label="receipt.max_concurrent_ocr"
    )
    fingerprint = value["settings_fingerprint"]
    if (
        maximum <= 0
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    ):
        raise HandoffError("receipt OCR adapter state is unsafe")
    return {
        "active_ocr": active,
        "max_concurrent_ocr": maximum,
        "settings_fingerprint": fingerprint,
    }


def validate_receipt_schema(receipt: object) -> dict[str, Any]:
    """Validate only the body-free, non-secret receipt schema before use or write."""
    expected_keys = {
        "schema_version",
        "receipt_id",
        "observed_at",
        "operation",
        "target",
        "mode",
        "operator",
        "approval_ref",
        "handoff_ref",
        "provider_cost_approved",
        "queue_snapshots",
        "ocr_adapter",
        "changed_keys",
        "old_values",
        "new_values",
        "config_fingerprint",
        "env_sha256_before",
        "env_sha256_transition",
        "env_sha256_after",
        "compose_or_adapter_status",
        "canary_status",
        "final_state",
        "source_receipt_id",
        "body_retained_bytes",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise HandoffError("receipt schema has unknown or missing fields")
    if receipt["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise HandoffError("recorded receipt uses an unsupported schema")
    if not isinstance(receipt["receipt_id"], str) or not RECEIPT_ID_PATTERN.fullmatch(
        receipt["receipt_id"]
    ):
        raise HandoffError("receipt id is invalid")
    operation = receipt["operation"]
    target = receipt["target"]
    if operation not in {
        "model",
        "ocr-routing",
        "ocr-adapter",
        "ocr-lifecycle",
        "restore",
    } or not isinstance(target, str):
        raise HandoffError("receipt operation or target is invalid")
    if operation == "ocr-lifecycle" and target not in LIFECYCLE_ACTIONS:
        raise HandoffError("receipt lifecycle action is invalid")
    if receipt["mode"] not in {"dry_run", "apply"}:
        raise HandoffError("receipt mode is invalid")
    if receipt["final_state"] not in SAFE_FINAL_STATES:
        raise HandoffError("receipt final state is invalid")
    if receipt["body_retained_bytes"] != 0:
        raise HandoffError("receipt must not retain bodies")
    if not isinstance(receipt["provider_cost_approved"], bool):
        raise HandoffError("receipt provider cost status is invalid")
    if receipt["operator"] is not None and receipt["operator"] not in ALLOWED_OPERATORS:
        raise HandoffError("receipt operator is invalid")
    for key in ("approval_ref", "handoff_ref", "source_receipt_id"):
        if receipt[key] is not None and not isinstance(receipt[key], str):
            raise HandoffError(f"receipt {key} is invalid")
    for key in ("approval_ref", "handoff_ref"):
        ensure_no_secret_reference(receipt[key], label=key.replace("_", "-"))
    if receipt["source_receipt_id"] is not None and not RECEIPT_ID_PATTERN.fullmatch(
        receipt["source_receipt_id"]
    ):
        raise HandoffError("receipt source id is invalid")
    if (
        receipt["compose_or_adapter_status"]
        not in {
            "not_run",
            "completed",
            "failed",
        }
        or receipt["canary_status"] != "not_run_no_automatic_canary"
    ):
        raise HandoffError("receipt execution status is invalid")
    if not isinstance(receipt["queue_snapshots"], list):
        raise HandoffError("receipt queue snapshots are invalid")
    receipt["queue_snapshots"] = [
        require_snapshot(snapshot) for snapshot in receipt["queue_snapshots"]
    ]
    receipt["ocr_adapter"] = require_adapter_receipt(receipt["ocr_adapter"])
    for key in (
        "env_sha256_before",
        "env_sha256_transition",
        "env_sha256_after",
    ):
        require_digest(receipt[key], label=key, allow_none=True)

    changed_keys = receipt["changed_keys"]
    if operation in {"model", "ocr-routing", "restore"}:
        keys = require_transition_key_set(changed_keys)
        old_values = require_nonsecret_value_map(
            receipt["old_values"], keys, label="old_values"
        )
        new_values = require_nonsecret_value_map(
            receipt["new_values"], keys, label="new_values"
        )
        if receipt["config_fingerprint"] != non_secret_digest(new_values):
            raise HandoffError("receipt config fingerprint is invalid")
        receipt["changed_keys"] = sorted(keys)
        receipt["old_values"] = old_values
        receipt["new_values"] = new_values
    elif (
        changed_keys != []
        or receipt["old_values"] is not None
        or receipt["new_values"] is not None
        or receipt["config_fingerprint"] != non_secret_digest({})
    ):
        raise HandoffError("adapter receipt must not include environment values")
    return receipt


def require_restorable_receipt(
    receipt: object,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    source = validate_receipt_schema(receipt)
    if (
        source["operation"] not in {"model", "ocr-routing"}
        or source["mode"] != "apply"
        or source["final_state"] != "retained"
        or source["compose_or_adapter_status"] != "completed"
        or source["source_receipt_id"] is not None
    ):
        raise HandoffError("recorded receipt is not a retained operator transition")
    keys = require_transition_key_set(source["changed_keys"])
    expected = (
        MODEL_PROFILES.get(source["target"])
        if source["operation"] == "model"
        else OCR_ROUTING_MODES.get(source["target"])
    )
    if expected is None or source["new_values"] != expected:
        raise HandoffError(
            "recorded receipt target does not match its transition values"
        )
    require_digest(source["env_sha256_after"], label="env_sha256_after")
    return source, keys


def write_receipt(receipt_dir: Path, receipt: dict[str, Any]) -> Path:
    receipt = validate_receipt_schema(receipt)
    receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(receipt_dir, 0o700)
    path = receipt_path(receipt_dir, receipt["receipt_id"])
    encoded = json.dumps(receipt, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if path.exists() or path.is_symlink():
        raise HandoffError("receipt destination already exists or is unsafe")
    temporary = path.with_name(f".{path.name}.operator-handoff-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except FileExistsError as exc:
            raise HandoffError("receipt staging path already exists") from exc
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise HandoffError(
                "receipt destination already exists or is unsafe"
            ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def parse_receipt(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HandoffError("recorded receipt is unavailable or unsafe")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("recorded receipt is invalid") from exc
    return validate_receipt_schema(receipt)


def command_runner(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def validate_apply_attestation(
    args: argparse.Namespace, operation: str, target: str
) -> tuple[str, str, str | None]:
    operator = ensure_no_secret_reference(args.operator, label="operator")
    if operator is None or operator not in ALLOWED_OPERATORS:
        raise HandoffError(
            "--operator must be one of the configured operator identities"
        )
    approval = ensure_no_secret_reference(args.approval_ref, label="approval-ref")
    if approval is None or not args.approve_provider_cost:
        raise HandoffError(
            "--approval-ref and --approve-provider-cost are required for --apply"
        )
    if args.confirm != f"APPLY {operation} {target}":
        raise HandoffError(
            "--confirm must exactly acknowledge the requested apply transition"
        )
    handoff: str | None = None
    if args.retain:
        handoff = ensure_no_secret_reference(args.handoff_ref, label="handoff-ref")
        if handoff is None or args.retain_confirm != f"RETAIN {operation} {target}":
            raise HandoffError(
                "--retain needs --handoff-ref and an exact --retain-confirm"
            )
    return operator, approval, handoff


def make_receipt(
    *,
    operation: str,
    target: str,
    mode: str,
    operator: str | None,
    approval_ref: str | None,
    handoff_ref: str | None,
    snapshots: list[dict[str, int]],
    adapter: dict[str, int | bool | str] | None,
    old_values: dict[str, str] | None,
    new_values: dict[str, str] | None,
    env_before: bytes | None,
    env_transition: bytes | None,
    env_after: bytes | None,
    final_state: str,
    execution_status: str | None = None,
    source_receipt_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": uuid.uuid4().hex,
        "observed_at": now_utc(),
        "operation": operation,
        "target": target,
        "mode": mode,
        "operator": operator,
        "approval_ref": approval_ref,
        "handoff_ref": handoff_ref,
        "provider_cost_approved": mode == "apply",
        "queue_snapshots": snapshots,
        "ocr_adapter": adapter,
        "changed_keys": sorted((new_values or {}).keys()),
        "old_values": old_values,
        "new_values": new_values,
        "config_fingerprint": non_secret_digest(new_values or {}),
        "env_sha256_before": sha256_bytes(env_before)
        if env_before is not None
        else None,
        "env_sha256_transition": sha256_bytes(env_transition)
        if env_transition is not None
        else None,
        "env_sha256_after": sha256_bytes(env_after) if env_after is not None else None,
        "compose_or_adapter_status": execution_status
        if execution_status is not None
        else ("not_run" if mode == "dry_run" else "completed"),
        "canary_status": "not_run_no_automatic_canary",
        "final_state": final_state,
        "source_receipt_id": source_receipt_id,
        "body_retained_bytes": 0,
    }


def recreate_api(runner: Callable[..., None], repo_root: Path) -> None:
    runner(
        [
            "docker",
            "compose",
            "--project-directory",
            str(repo_root),
            "up",
            "-d",
            "--force-recreate",
            "api",
        ],
        cwd=repo_root,
    )
    runner(
        [str(repo_root / "scripts" / "firecrawl-ops" / "firecrawl_healthcheck.sh")],
        cwd=repo_root,
    )


def docker_container_exists(name: str, repo_root: Path) -> bool:
    """Read only exact-container existence without retaining Docker output."""
    result = subprocess.run(
        ["docker", "container", "inspect", name],
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def ensure_safe_docling(
    runner: Callable[..., None],
    repo_root: Path,
    container_exists: Callable[[str, Path], bool],
) -> None:
    if container_exists(DOCLING_CONTAINER, repo_root):
        return
    runner(
        [
            "docker",
            "run",
            "-d",
            "--name",
            DOCLING_CONTAINER,
            "-p",
            "127.0.0.1:5001:5001",
            "-e",
            "DOCLING_SERVE_ENABLE_UI=1",
            "-e",
            "DOCLING_SERVE_MAX_SYNC_WAIT=900",
            DOCLING_IMAGE,
        ],
        cwd=repo_root,
    )


def verify_safe_docling(repo_root: Path) -> None:
    """Read back only the fixed Docling image and loopback port binding."""
    result = subprocess.run(
        [
            "docker",
            "container",
            "inspect",
            "--format",
            "{{.Config.Image}}|{{json .HostConfig.PortBindings}}",
            DOCLING_CONTAINER,
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise HandoffError("fixed Docling container could not be inspected")
    try:
        image, bindings_text = result.stdout.decode("utf-8").strip().split("|", 1)
        bindings = json.loads(bindings_text)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HandoffError("fixed Docling readback was malformed") from exc
    if image != DOCLING_IMAGE or bindings != {
        "5001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5001"}]
    }:
        raise HandoffError("fixed Docling image or loopback binding did not match")


def remove_container_if_present(
    runner: Callable[..., None],
    repo_root: Path,
    name: str,
    container_exists: Callable[[str, Path], bool],
) -> None:
    if container_exists(name, repo_root):
        runner(["docker", "rm", "-f", name], cwd=repo_root)


def restart_safe_ocr_adapter(
    runner: Callable[..., None],
    repo_root: Path,
    profile: str,
    container_exists: Callable[[str, Path], bool],
) -> None:
    """Bootstrap or restart an adapter with a fixed no-capture contract."""
    script_dir = repo_root / "scripts" / "firecrawl-ops"
    remove_container_if_present(runner, repo_root, ADAPTER_CONTAINER, container_exists)
    runner(
        [
            "docker",
            "build",
            "-t",
            "firecrawl-local-firepdf-adapter:latest",
            "-f",
            str(script_dir / "local-firepdf-adapter.Dockerfile"),
            str(repo_root),
        ],
        cwd=repo_root,
    )
    runner(
        [
            "docker",
            "run",
            "-d",
            "--name",
            ADAPTER_CONTAINER,
            "-p",
            "127.0.0.1:31337:31337",
            "-e",
            "LOCAL_FIREPDF_HOST=0.0.0.0",
            "-e",
            "LOCAL_FIREPDF_PORT=31337",
            "-e",
            "LOCAL_FIREPDF_ENGINE=docling",
            "-e",
            "LOCAL_FIREPDF_DOCLING_URL=http://host.docker.internal:5001",
            "-e",
            f"LOCAL_FIREPDF_PROFILE={profile}",
            "-e",
            "LOCAL_FIREPDF_PROFILES_PATH=/app/pdf_ocr_profiles.json",
            "-e",
            "LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=false",
            "-e",
            "LOCAL_FIREPDF_TIMEOUT_SECONDS=600",
            "-e",
            "LOCAL_FIREPDF_MAX_CONCURRENT_OCR=2",
            "-e",
            "LOCAL_FIREPDF_FAIL_LOW_QUALITY=true",
            "firecrawl-local-firepdf-adapter:latest",
        ],
        cwd=repo_root,
    )


def failure_receipt(
    *,
    operation: str,
    target: str,
    operator: str,
    approval_ref: str,
    handoff_ref: str | None,
    snapshots: list[dict[str, int]],
    adapter: dict[str, int | bool | str] | None,
    old_values: dict[str, str] | None,
    updates: dict[str, str] | None,
    raw_before: bytes | None,
    raw_transition: bytes | None,
    env_path: Path,
    receipt_dir: Path,
    source_receipt_id: str | None,
) -> Path:
    """Restore only an unchanged transition image, then durably record final state."""
    final_raw = raw_transition
    final_state = "manual-handoff-required"
    if raw_before is not None:
        try:
            observed, _, _ = read_env(env_path)
            final_raw = observed
            if raw_transition is not None and observed == raw_transition:
                write_env_exact(env_path, raw_before)
                final_raw, _, _ = read_env(env_path)
                if final_raw == raw_before:
                    final_state = "restored-after-failure"
        except (HandoffError, OSError):
            final_state = "manual-handoff-required"
    receipt = make_receipt(
        operation=operation,
        target=target,
        mode="apply",
        operator=operator,
        approval_ref=approval_ref,
        handoff_ref=handoff_ref,
        snapshots=snapshots,
        adapter=adapter,
        old_values=old_values,
        new_values=updates,
        env_before=raw_before,
        env_transition=raw_transition,
        env_after=final_raw,
        final_state=final_state,
        execution_status="failed",
        source_receipt_id=source_receipt_id,
    )
    return write_receipt(receipt_dir, receipt)


def run_transition(
    args: argparse.Namespace,
    *,
    client: ReadOnlyLoopbackClient | None = None,
    client_factory: Callable[..., ReadOnlyLoopbackClient] = ReadOnlyLoopbackClient,
    sleeper: Callable[[float], None] = time.sleep,
    runner: Callable[..., None] = command_runner,
    container_exists: Callable[[str, Path], bool] = docker_container_exists,
    docling_inspector: Callable[[Path], None] = verify_safe_docling,
    paths: TransitionPaths | None = None,
) -> tuple[dict[str, Any], Path]:
    injected_paths = paths is not None
    paths = paths or canonical_transition_paths()
    repo_root = paths.repo_root.resolve()
    if not (repo_root / "scripts" / "firecrawl-ops").is_dir():
        raise HandoffError("repo root does not contain local Firecrawl ops scripts")
    scope_root = (
        Path(
            os.path.commonpath(
                (str(paths.env_path.absolute()), str(paths.receipt_dir.absolute()))
            )
        )
        if injected_paths
        else paths.repo_root
    )
    env_path = resolve_scoped_transition_path(
        paths.env_path,
        scope_root=scope_root,
        label="local env file",
    )
    receipt_dir = resolve_scoped_transition_path(
        paths.receipt_dir,
        scope_root=scope_root,
        label="receipt directory",
    )
    client = client or client_factory(args.api_url, timeout_seconds=args.timeout)

    operation = args.operation
    target = ""
    updates: dict[str, str] | None = None
    keys: tuple[str, ...] = ()
    adapter: dict[str, int | bool | str] | None = None
    source_receipt_id: str | None = None
    source: dict[str, Any] | None = None

    if operation == "model":
        target = args.profile
        updates = MODEL_PROFILES[target]
        keys = MODEL_KEYS
    elif operation == "ocr-routing":
        target = args.mode
        updates = OCR_ROUTING_MODES[target]
        keys = OCR_ROUTING_KEYS
    elif operation == "ocr-adapter":
        target = args.profile
        profiles = repo_root / "scripts" / "firecrawl-ops" / "pdf_ocr_profiles.json"
        if resolve_profile_capture(target, profiles):
            raise HandoffError("OCR adapter profile enables raw Docling JSON capture")
    elif operation == "ocr-lifecycle":
        target = args.action
    elif operation == "restore":
        target = args.receipt
        source, keys = require_restorable_receipt(
            parse_receipt(receipt_path(receipt_dir, args.receipt))
        )
        source_receipt_id = source["receipt_id"]
        updates = dict(source["old_values"])
    else:  # pragma: no cover - argparse choices protect this branch
        raise HandoffError("unknown handoff operation")

    raw_before: bytes | None = None
    old_values: dict[str, str] | None = None
    if keys:
        raw_before, values, counts = read_env(env_path)
        old_values = require_reversible_keys(values, counts, keys)
        if operation == "restore":
            assert source is not None
            if sha256_bytes(raw_before) != source["env_sha256_after"]:
                raise HandoffError(
                    "env digest no longer matches the recorded post-transition state"
                )
        if operation == "ocr-routing" and values.get("FIRE_PDF_API_KEY"):
            raise HandoffError(
                "refusing to replace routing while a nonempty external FirePDF key exists"
            )

    snapshot_a = idle_snapshot(client)
    sleeper(args.snapshot_delay)
    snapshot_b = idle_snapshot(client)
    snapshots = [snapshot_a, snapshot_b]
    adapter_client: ReadOnlyLoopbackClient | None = None
    adapter_missing = False
    if operation == "ocr-adapter":
        adapter_client = client_factory(
            args.adapter_url, timeout_seconds=args.timeout, expected_port=31337
        )
        try:
            adapter = adapter_snapshot(adapter_client)
        except HandoffError as exc:
            if str(exc) != "loopback read is unavailable":
                raise
            adapter_missing = True

    if not args.apply:
        receipt = make_receipt(
            operation=operation,
            target=target,
            mode="dry_run",
            operator=None,
            approval_ref=None,
            handoff_ref=None,
            snapshots=snapshots,
            adapter=adapter,
            old_values=old_values,
            new_values=updates,
            env_before=raw_before,
            env_transition=raw_before,
            env_after=raw_before,
            final_state="planned",
            source_receipt_id=source_receipt_id,
        )
        return receipt, write_receipt(receipt_dir, receipt)

    operator, approval_ref, handoff_ref = validate_apply_attestation(
        args, operation, target
    )
    if operation in {"ocr-adapter", "ocr-lifecycle", "restore"} and not args.retain:
        raise HandoffError(
            f"{operation} apply requires --retain and a handoff reference"
        )
    snapshots.append(idle_snapshot(client))
    if adapter_client is not None and not adapter_missing:
        adapter = adapter_snapshot(adapter_client)

    raw_transition = raw_before
    mutation_started = False
    try:
        if operation in {"model", "ocr-routing", "restore"}:
            raw_transition = replace_env_values(raw_before or b"", updates or {})
            write_env_exact(env_path, raw_transition)
            mutation_started = True
        elif operation == "ocr-adapter":
            mutation_started = True
            restart_safe_ocr_adapter(runner, repo_root, target, container_exists)
            if adapter_missing:
                adapter_client = client_factory(
                    args.adapter_url, timeout_seconds=args.timeout, expected_port=31337
                )
            assert adapter_client is not None
            adapter = adapter_snapshot(adapter_client)
        elif operation == "ocr-lifecycle":
            mutation_started = True
            if target == "restart":
                remove_container_if_present(
                    runner, repo_root, ADAPTER_CONTAINER, container_exists
                )
                remove_container_if_present(
                    runner, repo_root, DOCLING_CONTAINER, container_exists
                )
            if target in {"ensure", "restart"}:
                ensure_safe_docling(runner, repo_root, container_exists)
                docling_inspector(repo_root)
                docling_client = client_factory(
                    DEFAULT_DOCLING_URL,
                    timeout_seconds=args.timeout,
                    expected_port=5001,
                )
                docling_client.require_success_status("/docs")
            if target == "restart":
                restart_safe_ocr_adapter(runner, repo_root, "default", container_exists)
                adapter_client = client_factory(
                    args.adapter_url, timeout_seconds=args.timeout, expected_port=31337
                )
                adapter = adapter_snapshot(adapter_client)
            elif target == "stop":
                remove_container_if_present(
                    runner, repo_root, ADAPTER_CONTAINER, container_exists
                )
                remove_container_if_present(
                    runner, repo_root, DOCLING_CONTAINER, container_exists
                )

        if operation in {"model", "ocr-routing", "restore"}:
            recreate_api(runner, repo_root)

        snapshots.append(idle_snapshot(client))
        if operation in {"model", "ocr-routing"} and not args.retain:
            current_raw, _, _ = read_env(env_path)
            if current_raw != raw_transition:
                raise HandoffError("env diverged before the automatic restore")
            write_env_exact(env_path, raw_before or b"")
            recreate_api(runner, repo_root)
            snapshots.append(idle_snapshot(client))
            final_raw, _, _ = read_env(env_path)
            if final_raw != raw_before:
                raise HandoffError("automatic restore did not produce the recorded env")
            final_state = "restored"
        elif raw_before is None:
            final_raw = None
            final_state = "retained"
        else:
            final_raw, _, _ = read_env(env_path)
            if final_raw != raw_transition:
                raise HandoffError("env diverged after the guarded transition")
            final_state = "retained"
    except Exception as exc:
        if not mutation_started:
            raise
        receipt_path_value = failure_receipt(
            operation=operation,
            target=target,
            operator=operator,
            approval_ref=approval_ref,
            handoff_ref=handoff_ref,
            snapshots=snapshots,
            adapter=adapter,
            old_values=old_values,
            updates=updates,
            raw_before=raw_before,
            raw_transition=raw_transition,
            env_path=env_path,
            receipt_dir=receipt_dir,
            source_receipt_id=source_receipt_id,
        )
        raise HandoffError(
            "apply failed after guarded mutation; inspect the redacted receipt "
            f"{receipt_path_value.name}"
        ) from exc

    receipt = make_receipt(
        operation=operation,
        target=target,
        mode="apply",
        operator=operator,
        approval_ref=approval_ref,
        handoff_ref=handoff_ref,
        snapshots=snapshots,
        adapter=adapter,
        old_values=old_values,
        new_values=updates,
        env_before=raw_before,
        env_transition=raw_transition,
        env_after=final_raw,
        final_state=final_state,
        source_receipt_id=source_receipt_id,
    )
    return receipt, write_receipt(receipt_dir, receipt)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--adapter-url", default=DEFAULT_ADAPTER_URL)
    parser.add_argument(
        "--timeout", type=positive_timeout, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument("--snapshot-delay", type=positive_timeout, default=0.2)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="perform a guarded transition after explicit attestation",
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="emit the default read-only plan receipt"
    )
    parser.add_argument("--operator")
    parser.add_argument("--approval-ref")
    parser.add_argument("--approve-provider-cost", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--retain", action="store_true")
    parser.add_argument("--handoff-ref")
    parser.add_argument("--retain-confirm")

    operations = parser.add_subparsers(dest="operation", required=True)
    model = operations.add_parser(
        "model", help="plan or change only model-routing values"
    )
    model.add_argument("--profile", choices=sorted(MODEL_PROFILES), required=True)
    routing = operations.add_parser(
        "ocr-routing", help="plan or change only local OCR routing values"
    )
    routing.add_argument("--mode", choices=sorted(OCR_ROUTING_MODES), required=True)
    adapter = operations.add_parser(
        "ocr-adapter",
        help="plan or restart the local adapter with a safe named profile",
    )
    adapter.add_argument("--profile", required=True)
    lifecycle = operations.add_parser(
        "ocr-lifecycle",
        help="plan or apply one fixed Docling/adapter lifecycle action",
    )
    lifecycle.add_argument("--action", choices=sorted(LIFECYCLE_ACTIONS), required=True)
    restore = operations.add_parser(
        "restore", help="restore non-secret values from a retained receipt"
    )
    restore.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        receipt, path = run_transition(args)
    except HandoffError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "receipt_id": receipt["receipt_id"],
                "receipt_path": str(path),
                "operation": receipt["operation"],
                "target": receipt["target"],
                "mode": receipt["mode"],
                "final_state": receipt["final_state"],
                "body_retained_bytes": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
