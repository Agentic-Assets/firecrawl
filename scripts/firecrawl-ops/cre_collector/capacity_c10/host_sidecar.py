"""Fail-closed host coordinator for the C10 v3 browser sidecar.

This module is deliberately the only Python surface which owns the C10 lock,
durable arm claim, private receipt directory, lifecycle keys, compose overlay,
and deadline.  The TypeScript child is an untrusted narrow transport: it is
given one signed capability and can return only the sidecar's signed evidence.
It cannot mint a capability, create a receipt store, or acquire a CRE lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    C10Error,
    canonical_bytes,
)

_ENV_MODE = 0o600
_ROOT_MODE = 0o700
_FILE_MODE = 0o600
_MAX_RAW_BODY_BYTES = 2 * 1024 * 1024
# Signed JSON contains base64 data. Keep the response cap independent from the
# envelope cap so a valid two-MiB browser response can still be sealed.
_MAX_PRIVATE_ARTIFACT = 8 * 1024 * 1024
_MAX_CHILD_FRAME_BYTES = 64 * 1024
_MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024
_MAX_CHILD_STDERR_BYTES = 64 * 1024
_MAX_CARD_TIMEOUT_MS = 30_000
_C10_COMPOSE_SERVICE = "playwright-service-c10"
_JLL_HOST = "property.jll.com"
_JLL_BOOTSTRAP_URL = "https://property.jll.com/"
# This is deliberately a fixed recipe rather than a caller supplied request
# graph.  The query is the reviewed public property search operation.  The
# immutable cohort supplies only the sixteen selected canonical member routes.
_JLL_QUERY = """query SearchResults($market: String! $language: String! $propertyTypes: [String!] $tenureTypes: [String!] $skip: Int $take: IntString = 50 $orderBy: PropertiesOrderInput) { properties(market: $market language: $language propertyTypes: $propertyTypes tenureTypes: $tenureTypes skip: $skip take: $take orderBy: $orderBy) { count items { id title images address propertyTypes tenureTypes rentPrice { amount currency unit } salePrice { amount currency unit } hidePrice pageUrl latitude longitude city state postcode surfaceAreas { value unit label alternativeUnit showEstimateDesks metrics { value unit } } } } }"""
_JLL_RECIPE = {"transaction": "sale", "property_type": "office", "page": 1}
_COHORT_HASH_FIELDS = (
    "schema_version",
    "config_sha256",
    "sampling",
    "sources",
    "planes",
    "aggregate",
)
_HEALTH_FIELDS = {
    "activePages",
    "configuredCapacity",
    "coordinatorKeyId",
    "evidenceKeyId",
    "healthSignature",
    "protocolVersion",
    "replayEntries",
    "status",
    "transport",
}
_EVIDENCE_FIELDS = {
    "binding",
    "bodyBase64",
    "cacheRead",
    "cacheWrite",
    "challengeDetected",
    "configuredCapacity",
    "contentType",
    "context",
    "elapsedMs",
    "engineAttempt",
    "evidenceSignature",
    "finalUrl",
    "jobId",
    "leaseEndMonotonicNs",
    "leaseStartMonotonicNs",
    "observedActivePages",
    "pageLease",
    "protocolVersion",
    "proxy",
    "queueMs",
    "redirectCount",
    "status",
}


def _canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _key_id(public_key_pem: str) -> str:
    return hashlib.sha256(public_key_pem.encode("utf-8")).hexdigest()


def _safe_json(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise C10Error(f"{label} must be an object")
    # Re-encoding rejects non-finite and unsupported values through contracts.
    json.loads(_canonical_text(value))
    return value


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise C10Error("C10 lifecycle deadline expired")
    return value


@dataclass(frozen=True)
class C10EphemeralKeys:
    coordinator_private_pem: str
    coordinator_public_pem: str
    sidecar_private_pem: str
    sidecar_public_pem: str
    transport_key: str


class SidecarLifecycle(Protocol):
    def start(
        self, environment: Mapping[str, str], port: int, deadline: float
    ) -> None: ...
    def stop(self, deadline: float) -> None: ...


class DockerComposeSidecar:
    """The one supported compose overlay, with a temporary owner-only env file."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._env_file: Path | None = None
        self._compose_env: dict[str, str] | None = None

    def start(self, environment: Mapping[str, str], port: int, deadline: float) -> None:
        descriptor, raw = tempfile.mkstemp(prefix="c10-sidecar-", suffix=".env")
        self._env_file = Path(raw)
        os.fchmod(descriptor, _ENV_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key, value in environment.items():
                if "\n" in value or "\r" in value:
                    raise C10Error("C10 sidecar environment is invalid")
                handle.write(f"{key}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        command = [
            "docker",
            "compose",
            "-f",
            "docker-compose.yaml",
            "-f",
            "docker-compose.c10.yaml",
            "up",
            "-d",
            "--no-deps",
            _C10_COMPOSE_SERVICE,
        ]
        env = {
            **os.environ,
            **environment,
            "C10_BROWSER_PRIVATE_ENV_FILE": str(self._env_file),
            "C10_BROWSER_HOST_PORT": str(port),
            "C10_COMPOSE_PROJECT": f"c10-{secrets.token_hex(12)}",
        }
        env["COMPOSE_PROJECT_NAME"] = env["C10_COMPOSE_PROJECT"]
        self._compose_env = env
        try:
            rendered = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yaml",
                    "-f",
                    "docker-compose.c10.yaml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=_remaining(deadline),
                check=False,
            )
            if rendered.returncode != 0 or not self._rendered_identity(
                rendered.stdout, port, environment
            ):
                raise C10Error(
                    "C10 compose overlay did not render the exact loopback sidecar"
                )
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                timeout=_remaining(deadline),
                check=False,
            )
            if result.returncode != 0:
                raise C10Error("C10 sidecar compose startup failed")
        except BaseException:
            # A compose validation timeout or `up` failure may have created a
            # container. Always use this exact env/project identity to remove
            # it before the exception escapes the lifecycle owner.
            try:
                self.stop(deadline)
            except BaseException as cleanup_error:
                raise C10Error(
                    "C10 sidecar partial-start cleanup failed"
                ) from cleanup_error
            raise

    def stop(self, deadline: float) -> None:
        try:
            if self._compose_env is None:
                # Start owns failed-start cleanup locally. A host finally block
                # may race only that already-confirmed cleanup, never another
                # project or ordinary sidecar.
                return
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yaml",
                    "-f",
                    "docker-compose.c10.yaml",
                    "rm",
                    "--force",
                    "--stop",
                    _C10_COMPOSE_SERVICE,
                ],
                cwd=self.repo_root,
                env=self._compose_env,
                capture_output=True,
                timeout=_remaining(deadline),
                check=False,
            )
            if result.returncode != 0:
                raise C10Error("C10 sidecar removal was not confirmed")
            quiescence = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yaml",
                    "-f",
                    "docker-compose.c10.yaml",
                    "ps",
                    "--all",
                    "--format",
                    "json",
                    _C10_COMPOSE_SERVICE,
                ],
                cwd=self.repo_root,
                env=self._compose_env,
                capture_output=True,
                text=True,
                timeout=_remaining(deadline),
                check=False,
            )
            if quiescence.returncode != 0 or not self._compose_is_absent(
                quiescence.stdout
            ):
                # Absence is stronger than stopped: it removes the container
                # Config.Env which held the per-session sidecar secrets.
                raise C10Error(
                    "C10 sidecar removal/Config.Env absence was not confirmed"
                )
        finally:
            self._remove_env()
            self._compose_env = None

    @staticmethod
    def _rendered_identity(
        rendered: str, port: int, expected_environment: Mapping[str, str]
    ) -> bool:
        try:
            service = json.loads(rendered)["services"][_C10_COMPOSE_SERVICE]
            ports = service["ports"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        loopback = any(
            isinstance(item, Mapping)
            and item.get("host_ip") == "127.0.0.1"
            and str(item.get("published")) == str(port)
            and int(item.get("target", 0)) == 3004
            for item in ports
        )
        actual_environment = service.get("environment")
        if isinstance(actual_environment, list):
            actual_environment = dict(
                item.split("=", 1) for item in actual_environment if "=" in item
            )
        return (
            loopback
            and str(service.get("cpus")) == expected_environment["C10_BROWSER_CPUS"]
            and isinstance(actual_environment, Mapping)
            and all(
                actual_environment.get(key) == expected_environment[key]
                for key in ("MAX_CONCURRENT_PAGES", "C10_PROFILE_SHA256")
            )
            and (actual_environment.get("C10_ADMISSION_LANE") or "")
            == (expected_environment.get("C10_ADMISSION_LANE") or "")
        )

    def _remove_env(self) -> None:
        if self._env_file is not None:
            self._env_file.unlink(missing_ok=True)
            self._env_file = None

    @staticmethod
    def _compose_is_absent(output: str) -> bool:
        if not output.strip():
            return True
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return False
            # Compose may return a JSON array or a JSON object. Either form
            # represents a retained container and therefore retained Config.Env.
            if parsed not in (None, []):
                return False
        return True
