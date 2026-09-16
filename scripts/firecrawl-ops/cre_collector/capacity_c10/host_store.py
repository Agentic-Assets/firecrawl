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
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from .contracts import (
    C10Error,
    canonical_bytes,
    claim_next_arm,
    sha256,
    validate_plan,
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


class PrivateReceiptStore:
    """Linux-only descriptor-relative 0700/0600 immutable receipt store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._fd = -1
        self._identity: tuple[int, int] | None = None

    @classmethod
    def create(cls, root: Path) -> PrivateReceiptStore:
        if (
            sys.platform != "linux"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
        ):
            raise C10Error(
                "C10 private receipt storage requires Linux fd-relative primitives"
            )
        if not root.is_absolute():
            raise C10Error("C10 private receipt root must be absolute")
        root.mkdir(mode=_ROOT_MODE, parents=True, exist_ok=True)
        store = cls(root)
        try:
            store._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fchmod(store._fd, _ROOT_MODE)
            store._assert_root()
            return store
        except BaseException:
            store.close()
            raise

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def seal_json(self, stem: str, value: Any) -> Mapping[str, Any]:
        return self.seal_bytes(stem, canonical_bytes(value))

    def seal_bytes(self, stem: str, body: bytes) -> Mapping[str, Any]:
        if not stem or not all(
            char.islower() or char.isdigit() or char == "-" for char in stem
        ):
            raise C10Error("C10 private receipt stem is invalid")
        if len(body) > _MAX_PRIVATE_ARTIFACT:
            raise C10Error("C10 private receipt exceeds its body bound")
        self._assert_root()
        digest = hashlib.sha256(body).hexdigest()
        final = f"{stem}-{digest}.sealed"
        temporary = f".{final}.tmp-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, _FILE_MODE, dir_fd=self._fd)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(
                temporary,
                final,
                src_dir_fd=self._fd,
                dst_dir_fd=self._fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=self._fd)
            os.fsync(self._fd)
            self._assert_artifact(final, digest, len(body))
            return {"name": final, "sha256": digest, "bytes": len(body)}
        except BaseException as exc:
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            if isinstance(exc, C10Error):
                raise
            raise C10Error("C10 private receipt could not be sealed") from exc

    def _assert_root(self) -> None:
        if self._fd < 0:
            raise C10Error("C10 private receipt store is closed")
        opened, named = os.fstat(self._fd), os.lstat(self.root)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != _ROOT_MODE
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_IMODE(named.st_mode) != _ROOT_MODE
            or (named.st_dev, named.st_ino) != identity
            or (self._identity is not None and self._identity != identity)
        ):
            raise C10Error("C10 private receipt root identity changed")
        self._identity = identity

    def _assert_artifact(self, name: str, digest: str, expected: int) -> None:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            body = handle.read()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
            or metadata.st_nlink != 1
            or len(body) != expected
            or hashlib.sha256(body).hexdigest() != digest
        ):
            raise C10Error("C10 sealed receipt readback is invalid")


class C10SessionStore:
    """A durable, plan-bound one-arm claim ledger owned by the Python host."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise C10Error("C10 durable session path must be absolute")
        self.path = path

    def claim(
        self, plan: Mapping[str, Any], session: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        validate_plan(plan)
        proposed = claim_next_arm(plan, session)
        source_session_digest = sha256(session)
        self.path.parent.mkdir(mode=_ROOT_MODE, parents=True, exist_ok=True)
        if self.path.exists():
            raise C10Error("C10 durable session already has a claimed arm")
        record = {
            "kind": "cre_capacity_c10_v3_host_claim",
            "plan_sha256": plan["plan_sha256"],
            "source_session_sha256": source_session_digest,
            "arm": proposed["arm"],
            "claimed_session": proposed["session"],
            "claim_id": secrets.token_hex(32),
        }
        record["session_sha256"] = sha256(record)
        descriptor = -1
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(canonical_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except FileExistsError as exc:
            raise C10Error("C10 durable session already has a claimed arm") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return record

    def assert_available(
        self, plan: Mapping[str, Any], session: Mapping[str, Any]
    ) -> None:
        """Reject an in-progress or terminal replay of this exact arm identity."""
        if not self.path.exists():
            return
        record = self._read_record()
        if record.get("plan_sha256") != plan.get("plan_sha256") or record.get(
            "source_session_sha256"
        ) != sha256(session):
            raise C10Error("C10 canonical ledger identity is inconsistent")
        raise C10Error("C10 durable session is already claimed or terminalized")

    def _read_record(self) -> Mapping[str, Any]:
        try:
            metadata = os.lstat(self.path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
                or metadata.st_nlink != 1
            ):
                raise C10Error("C10 durable session ledger is unsafe")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise C10Error("C10 durable session ledger cannot be read") from exc
        if not isinstance(payload, Mapping):
            raise C10Error("C10 durable session ledger is invalid")
        return payload

    def read_bound(
        self, plan: Mapping[str, Any], session: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        payload = self._read_record()
        if (
            not isinstance(payload, Mapping)
            or payload.get("plan_sha256") != plan.get("plan_sha256")
            or payload.get("source_session_sha256") != sha256(session)
        ):
            raise C10Error("C10 durable session rejects an alternate plan or ledger")
        return payload

    def record_quarantine(self, claim: Mapping[str, Any] | None, reason: str) -> None:
        """Durably preserve the failure before the canonical lock can release."""
        record = {
            "kind": "cre_capacity_c10_v3_host_quarantine",
            "claim_id": claim.get("claim_id") if isinstance(claim, Mapping) else None,
            "session_sha256": claim.get("session_sha256")
            if isinstance(claim, Mapping)
            else None,
            "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            "state": "quarantined",
        }
        target = self.path.with_name(f"{self.path.name}.quarantine")
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except FileExistsError:
            return
        except OSError as exc:
            raise C10Error("C10 quarantine evidence could not be persisted") from exc

    def record_terminal(
        self, claim: Mapping[str, Any], authenticated_arm: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Durably bind one authenticated host arm before lock release.

        This intentionally stores only hashes and the comparator-facing
        envelope. Browser bodies remain in the private sealed receipt root.
        ``O_EXCL`` makes a second terminalization of the same claim a stop.
        """
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise C10Error("C10 terminalization requires a durable claim")
        record = {
            "kind": "cre_capacity_c10_v3_host_terminal",
            "claim_id": claim_id,
            "plan_sha256": claim.get("plan_sha256"),
            "source_session_sha256": claim.get("source_session_sha256"),
            "session_sha256": claim.get("session_sha256"),
            "arm": claim.get("arm"),
            "claimed_session": claim.get("claimed_session"),
            "authenticated_arm": dict(authenticated_arm),
            "authenticated_arm_sha256": sha256(authenticated_arm),
            "state": "terminal",
        }
        expected = self._read_record()
        if dict(expected) != dict(claim):
            raise C10Error("C10 terminalization claim no longer matches the ledger")
        target = self.path.with_name(
            f".{self.path.name}.terminal-{secrets.token_hex(16)}"
        )
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.replace(target, self.path)
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise C10Error("C10 terminal evidence could not be persisted") from exc
        return record

    def load_terminal(
        self, plan: Mapping[str, Any], session: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Reload the safe comparator envelope after process/stdout loss."""
        record = self._read_record()
        envelope = record.get("authenticated_arm")
        if (
            record.get("kind") != "cre_capacity_c10_v3_host_terminal"
            or record.get("plan_sha256") != plan.get("plan_sha256")
            or record.get("source_session_sha256") != sha256(session)
            or not isinstance(envelope, Mapping)
            or record.get("authenticated_arm_sha256") != sha256(envelope)
        ):
            raise C10Error("C10 terminal ledger cannot be reverified")
        return envelope
