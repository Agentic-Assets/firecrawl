"""Fail-closed host coordinator for the C10 v3 browser sidecar.

This module is deliberately the only Python surface which owns the C10 lock,
durable arm claim, private receipt directory, lifecycle keys, compose overlay,
and deadline.  The TypeScript child is an untrusted narrow transport: it is
given one signed capability and can return only the sidecar's signed evidence.
It cannot mint a capability, create a receipt store, or acquire a CRE lock.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


class _OpenSsl:
    """Small Ed25519 adapter using the host OpenSSL, never a stored key."""

    @staticmethod
    def pair(deadline: float) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="c10-ed25519-") as root:
            private = Path(root) / "private.pem"
            public = Path(root) / "public.pem"
            _OpenSsl._run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                deadline,
            )
            _OpenSsl._run(
                [
                    "openssl",
                    "pkey",
                    "-in",
                    str(private),
                    "-pubout",
                    "-out",
                    str(public),
                ],
                deadline,
            )
            return private.read_text(encoding="utf-8"), public.read_text(
                encoding="utf-8"
            )

    @staticmethod
    def sign(private_pem: str, payload: bytes, deadline: float) -> str:
        with tempfile.TemporaryDirectory(prefix="c10-sign-") as root:
            private, body, signature = (
                Path(root) / "private.pem",
                Path(root) / "body",
                Path(root) / "sig",
            )
            private.write_text(private_pem, encoding="utf-8")
            private.chmod(_FILE_MODE)
            body.write_bytes(payload)
            _OpenSsl._run(
                [
                    "openssl",
                    "pkeyutl",
                    "-sign",
                    "-inkey",
                    str(private),
                    "-rawin",
                    "-in",
                    str(body),
                    "-out",
                    str(signature),
                ],
                deadline,
            )
            return (
                base64.urlsafe_b64encode(signature.read_bytes())
                .decode("ascii")
                .rstrip("=")
            )

    @staticmethod
    def verify(
        public_pem: str, payload: bytes, signature: str, deadline: float
    ) -> bool:
        try:
            with tempfile.TemporaryDirectory(prefix="c10-verify-") as root:
                public, body, sig = (
                    Path(root) / "public.pem",
                    Path(root) / "body",
                    Path(root) / "sig",
                )
                public.write_text(public_pem, encoding="utf-8")
                public.chmod(_FILE_MODE)
                body.write_bytes(payload)
                sig.write_bytes(
                    base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
                )
                _OpenSsl._run(
                    [
                        "openssl",
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-inkey",
                        str(public),
                        "-rawin",
                        "-in",
                        str(body),
                        "-sigfile",
                        str(sig),
                    ],
                    deadline,
                )
                return True
        except (C10Error, ValueError):
            return False

    @staticmethod
    def _run(command: list[str], deadline: float) -> None:
        try:
            result = subprocess.run(
                command, capture_output=True, check=False, timeout=_remaining(deadline)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise C10Error("C10 OpenSSL operation failed") from exc
        if result.returncode != 0:
            raise C10Error("C10 OpenSSL operation failed")
