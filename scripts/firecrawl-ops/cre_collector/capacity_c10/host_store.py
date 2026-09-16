"""Fail-closed host coordinator for the C10 v3 browser sidecar.

This module is deliberately the only Python surface which owns the C10 lock,
durable arm claim, private receipt directory, lifecycle keys, compose overlay,
and deadline.  The TypeScript child is an untrusted narrow transport: it is
given one signed capability and can return only the sidecar's signed evidence.
It cannot mint a capability, create a receipt store, or acquire a CRE lock.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from .contracts import (
    C10Error,
    canonical_bytes,
    claim_next_arm,
    sha256,
    validate_plan,
)
from .host_crypto import _OpenSsl

_ENV_MODE = 0o600
_ROOT_MODE = 0o700
_FILE_MODE = 0o600
_CONTROLLER_AUTH_SECRET = secrets.token_bytes(32)
_ACTIVE_CONTROLLER_AUTH: contextvars.ContextVar[Mapping[str, Any] | None] = (
    contextvars.ContextVar("c10_active_controller_ledger_authorization", default=None)
)


def _controller_authorization_payload(
    kind: str,
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    attempt: str | None,
    deadline: float,
) -> dict[str, Any]:
    if kind not in {"claim", "terminal"} or type(deadline) not in {int, float}:
        raise C10Error("C10 controller ledger authorization is invalid")
    index, variant = arm.get("index"), arm.get("variant")
    if type(index) is not int or variant not in {"p0", "p1"}:
        raise C10Error("C10 controller ledger arm is invalid")
    return {
        "kind": kind,
        "plan_sha256": plan.get("plan_sha256"),
        "arm": {"index": index, "variant": variant},
        "attempt": attempt,
        "deadline_ns": int(deadline * 1_000_000_000),
    }


@contextmanager
def _controller_ledger_authorization(
    kind: str,
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    *,
    attempt: str | None,
    deadline: float,
) -> Any:
    """Install one controller-only, HMAC-bound ledger mutation authorization."""
    payload = _controller_authorization_payload(kind, plan, arm, attempt, deadline)
    authorization = {
        "payload": payload,
        "mac": hmac.new(
            _CONTROLLER_AUTH_SECRET, canonical_bytes(payload), hashlib.sha256
        ).hexdigest(),
    }
    token = _ACTIVE_CONTROLLER_AUTH.set(authorization)
    try:
        yield
    finally:
        _ACTIVE_CONTROLLER_AUTH.reset(token)


def _require_controller_ledger_authorization(
    kind: str,
    plan: Mapping[str, Any],
    arm: Mapping[str, Any],
    *,
    attempt: str | None,
    deadline: float | None,
) -> None:
    authorization = _ACTIVE_CONTROLLER_AUTH.get()
    if (
        deadline is None
        or time.monotonic() >= deadline
        or not isinstance(authorization, Mapping)
    ):
        raise C10Error("C10 ledger mutation requires active controller provenance")
    payload = authorization.get("payload")
    mac = authorization.get("mac")
    if not isinstance(payload, Mapping) or not isinstance(mac, str):
        raise C10Error("C10 ledger controller provenance is invalid")
    expected = _controller_authorization_payload(kind, plan, arm, attempt, deadline)
    expected_mac = hmac.new(
        _CONTROLLER_AUTH_SECRET, canonical_bytes(expected), hashlib.sha256
    ).hexdigest()
    if dict(payload) != expected or not hmac.compare_digest(mac, expected_mac):
        raise C10Error("C10 ledger controller provenance does not bind this mutation")


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

    def descriptor(self) -> Mapping[str, str]:
        """Return a non-secret, inode-bound identity for a reopened receipt root."""
        self._assert_root()
        metadata = os.fstat(self._fd)
        path = str(self.root.resolve())
        return {
            "path": path,
            "id": hashlib.sha256(
                canonical_bytes(
                    {"path": path, "dev": metadata.st_dev, "ino": metadata.st_ino}
                )
            ).hexdigest(),
        }

    def read_sealed(self, artifact: Mapping[str, Any]) -> bytes:
        """Re-open and hash one exact manifest artifact without path traversal."""
        if set(artifact) != {"name", "sha256", "bytes"}:
            raise C10Error("C10 sealed receipt manifest entry is invalid")
        name, digest, expected = (
            artifact.get("name"),
            artifact.get("sha256"),
            artifact.get("bytes"),
        )
        if (
            not isinstance(name, str)
            or not name.endswith(".sealed")
            or Path(name).name != name
            or not isinstance(digest, str)
            or type(expected) is not int
            or expected <= 0
        ):
            raise C10Error("C10 sealed receipt manifest entry is unsafe")
        self._assert_root()
        self._assert_artifact(name, digest, expected)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()

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
    """A durable, plan-bound one-arm ledger rooted at one verified directory.

    The ledger root is deliberately opened once and every record operation is
    relative to that descriptor.  The name is re-checked against the descriptor
    before and after each operation: replacing the root can therefore only stop
    the protocol, not redirect a read or durable write into an attacker root.
    """

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise C10Error("C10 durable session path must be absolute")
        self.path = path
        self._root = path.parent
        self._root_fd = -1
        self._root_identity: tuple[int, int] | None = None

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __del__(self) -> None:
        self.close()

    def _arm_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.stem}.arm-{index}.json")

    def _arm_name(self, index: int) -> str:
        return self._arm_path(index).name

    def _root_descriptor(self, *, create: bool) -> int | None:
        """Return the checked ledger-root descriptor without following its name."""
        if self._root_fd >= 0:
            self._assert_root()
            return self._root_fd
        try:
            if create:
                self._root.mkdir(mode=_ROOT_MODE, parents=True, exist_ok=True)
            else:
                try:
                    os.lstat(self._root)
                except FileNotFoundError:
                    return None
            self._root_fd = os.open(
                self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            self._assert_root()
            return self._root_fd
        except FileNotFoundError:
            if not create:
                return None
            raise C10Error("C10 durable session ledger root is unavailable") from None
        except OSError as exc:
            self.close()
            raise C10Error("C10 durable session ledger root is unsafe") from exc
        except BaseException:
            self.close()
            raise

    def _assert_root(self) -> None:
        if self._root_fd < 0:
            raise C10Error("C10 durable session ledger root is closed")
        try:
            opened, named = os.fstat(self._root_fd), os.lstat(self._root)
        except OSError as exc:
            raise C10Error("C10 durable session ledger root identity changed") from exc
        identity = (opened.st_dev, opened.st_ino)
        owner = os.geteuid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != owner
            or stat.S_IMODE(opened.st_mode) != _ROOT_MODE
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or named.st_uid != owner
            or stat.S_IMODE(named.st_mode) != _ROOT_MODE
            or (named.st_dev, named.st_ino) != identity
            or (self._root_identity is not None and self._root_identity != identity)
        ):
            raise C10Error("C10 durable session ledger root identity changed")
        self._root_identity = identity

    def _record_exists(self, name: str, descriptor: int) -> bool:
        self._assert_root()
        try:
            metadata = os.lstat(name, dir_fd=descriptor)
        except FileNotFoundError:
            self._assert_root()
            return False
        except OSError as exc:
            raise C10Error("C10 durable session ledger cannot be read") from exc
        self._assert_root()
        if stat.S_ISLNK(metadata.st_mode):
            raise C10Error("C10 durable session ledger is unsafe")
        return True

    def _record_names(self, descriptor: int) -> list[str]:
        self._assert_root()
        try:
            names = os.listdir(descriptor)
        except OSError as exc:
            raise C10Error("C10 durable session ledger cannot be read") from exc
        self._assert_root()
        return names

    def _write_new_record(
        self, name: str, record: Mapping[str, Any], *, error: str
    ) -> None:
        descriptor = self._root_descriptor(create=True)
        assert descriptor is not None
        self._assert_root()
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
                dir_fd=descriptor,
            )
            with os.fdopen(file_descriptor, "wb") as handle:
                file_descriptor = -1
                handle.write(canonical_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_root()
            os.fsync(descriptor)
            self._assert_root()
        except FileExistsError:
            raise
        except OSError as exc:
            raise C10Error(error) from exc
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    def _next(self, plan: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Derive progress solely from immutable protocol/plan-owned arm records."""
        validate_plan(plan)
        descriptor = self._root_descriptor(create=False)
        if descriptor is None:
            return 0, {
                "schema_version": 1,
                "kind": "cre_capacity_c10_v1_session",
                "plan_sha256": plan["plan_sha256"],
                "consumed_arm_indexes": [],
            }
        if self._record_exists(self.path.with_suffix(".quarantine").name, descriptor):
            raise C10Error("C10 protocol ledger requires terminal recovery")
        expected = {self._arm_name(index) for index in range(len(plan["arm_sequence"]))}
        if any(
            candidate.name not in expected
            for candidate in map(Path, self._record_names(descriptor))
            if candidate.match(f"{self.path.stem}.arm-*.json")
        ):
            raise C10Error("C10 immutable protocol ledger has an extra arm record")
        consumed: list[int] = []
        for index in range(len(plan["arm_sequence"])):
            name = self._arm_name(index)
            if not self._record_exists(name, descriptor):
                if any(
                    self._record_exists(self._arm_name(later), descriptor)
                    for later in range(index + 1, len(plan["arm_sequence"]))
                ):
                    raise C10Error("C10 immutable protocol ledger has an arm gap")
                break
            record = self._read_record(name)
            arm = record.get("arm")
            if (
                record.get("plan_sha256") != plan["plan_sha256"]
                or not isinstance(arm, Mapping)
                or arm.get("index") != index
                or arm.get("variant") != plan["arm_sequence"][index]
                or record.get("state") not in {"claimed", "terminal", "quarantined"}
            ):
                raise C10Error("C10 immutable protocol ledger is inconsistent")
            if record.get("state") != "terminal":
                raise C10Error("C10 protocol ledger requires terminal recovery")
            consumed.append(index)
        if len(consumed) == len(plan["arm_sequence"]):
            raise C10Error("all C10 protocol arms are already consumed")
        # The session is reconstructed, never accepted from an execution caller.
        return len(consumed), {
            "schema_version": 1,
            "kind": "cre_capacity_c10_v1_session",
            "plan_sha256": plan["plan_sha256"],
            "consumed_arm_indexes": consumed,
        }

    def claim(
        self, plan: Mapping[str, Any], *, deadline: float | None = None
    ) -> Mapping[str, Any]:
        validate_plan(plan)
        self._root_descriptor(create=True)
        index, internal_session = self._next(plan)
        proposed = claim_next_arm(plan, internal_session)
        _require_controller_ledger_authorization(
            "claim", plan, proposed["arm"], attempt=None, deadline=deadline
        )
        source_session_digest = sha256(internal_session)
        descriptor = self._root_descriptor(create=True)
        assert descriptor is not None
        name = self._arm_name(index)
        if self._record_exists(name, descriptor):
            raise C10Error("C10 durable protocol arm already has a claim")
        record = {
            "kind": "cre_capacity_c10_v3_host_claim",
            "plan_sha256": plan["plan_sha256"],
            "protocol": "cre_capacity_c10_v3",
            "ledger_identity_sha256": sha256(
                {"protocol": "cre_capacity_c10_v3", "plan_sha256": plan["plan_sha256"]}
            ),
            "source_session_sha256": source_session_digest,
            "arm": proposed["arm"],
            "claimed_session": proposed["session"],
            "claim_id": secrets.token_hex(32),
            "state": "claimed",
        }
        record["session_sha256"] = sha256(record)
        try:
            self._write_new_record(
                name,
                record,
                error="C10 durable session ledger could not be persisted",
            )
        except FileExistsError as exc:
            raise C10Error("C10 durable protocol arm already has a claim") from exc
        return record

    def assert_available(self, plan: Mapping[str, Any]) -> None:
        """Reject an in-progress or terminal replay of this exact arm identity."""
        self._next(plan)

    def next_arm_index(self, plan: Mapping[str, Any]) -> int:
        """Expose the internally derived next arm without accepting a session."""
        index, _session = self._next(plan)
        return index

    def _read_record(self, name: str) -> Mapping[str, Any]:
        descriptor = self._root_descriptor(create=False)
        if descriptor is None:
            raise C10Error("C10 durable session ledger cannot be read")
        self._assert_root()
        try:
            file_descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            with os.fdopen(file_descriptor, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                payload = json.loads(handle.read().decode("utf-8"))
            self._assert_root()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
                or metadata.st_nlink != 1
            ):
                raise C10Error("C10 durable session ledger is unsafe")
        except (OSError, json.JSONDecodeError) as exc:
            raise C10Error("C10 durable session ledger cannot be read") from exc
        if not isinstance(payload, Mapping):
            raise C10Error("C10 durable session ledger is invalid")
        return payload

    def read_bound(
        self, plan: Mapping[str, Any], claim: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        arm = claim.get("arm")
        if not isinstance(arm, Mapping) or type(arm.get("index")) is not int:
            raise C10Error("C10 durable claim arm is invalid")
        payload = self._read_record(self._arm_name(arm["index"]))
        if (
            not isinstance(payload, Mapping)
            or payload.get("plan_sha256") != plan.get("plan_sha256")
            or dict(payload) != dict(claim)
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
        index = (
            claim.get("arm", {}).get("index") if isinstance(claim, Mapping) else None
        )
        name = (
            self._arm_path(index).with_suffix(".quarantine").name
            if type(index) is int
            else self.path.with_suffix(".quarantine").name
        )
        try:
            self._write_new_record(
                name,
                record,
                error="C10 quarantine evidence could not be persisted",
            )
        except FileExistsError:
            return

    def record_terminal(
        self,
        plan: Mapping[str, Any],
        claim: Mapping[str, Any],
        authenticated_arm: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> Mapping[str, Any]:
        """Durably bind one authenticated host arm before lock release.

        This intentionally stores only hashes and the comparator-facing
        envelope. Browser bodies remain in the private sealed receipt root.
        ``O_EXCL`` makes a second terminalization of the same claim a stop.
        """
        descriptor = self._root_descriptor(create=False)
        if descriptor is None:
            raise C10Error("C10 terminal evidence could not be persisted")
        self._assert_root()
        self._validate_authenticated_terminal(
            plan, authenticated_arm, deadline=deadline
        )
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise C10Error("C10 terminalization requires a durable claim")
        arm = claim.get("arm")
        if not isinstance(arm, Mapping) or type(arm.get("index")) is not int:
            raise C10Error("C10 terminalization requires an exact arm")
        _require_controller_ledger_authorization(
            "terminal", plan, arm, attempt=claim_id, deadline=deadline
        )
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
        arm_name = self._arm_name(arm["index"])
        expected = self._read_record(arm_name)
        if dict(expected) != dict(claim):
            raise C10Error("C10 terminalization claim no longer matches the ledger")
        descriptor = self._root_descriptor(create=False)
        assert descriptor is not None
        temporary = f".{arm_name}.terminal-{secrets.token_hex(16)}"
        self._assert_root()
        try:
            file_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
                dir_fd=descriptor,
            )
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(canonical_bytes(record))
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_root()
            os.replace(
                temporary,
                arm_name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            os.fsync(descriptor)
            self._assert_root()
        except OSError as exc:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except OSError:
                pass
            raise C10Error("C10 terminal evidence could not be persisted") from exc
        return record

    def load_terminal(
        self, plan: Mapping[str, Any], index: int, *, deadline: float | None = None
    ) -> Mapping[str, Any]:
        """Reload the safe comparator envelope after process/stdout loss."""
        verification_deadline = (
            deadline if deadline is not None else time.monotonic() + 30
        )
        if time.monotonic() >= verification_deadline:
            raise C10Error("C10 terminal revalidation deadline expired")
        if type(index) is not int or index < 0 or index >= len(plan["arm_sequence"]):
            raise C10Error("C10 terminal arm index is invalid")
        record = self._read_record(self._arm_name(index))
        envelope = record.get("authenticated_arm")
        if (
            record.get("kind") != "cre_capacity_c10_v3_host_terminal"
            or record.get("plan_sha256") != plan.get("plan_sha256")
            or not isinstance(envelope, Mapping)
            or record.get("authenticated_arm_sha256") != sha256(envelope)
        ):
            raise C10Error("C10 terminal ledger cannot be reverified")
        self._validate_authenticated_terminal(
            plan, envelope, deadline=verification_deadline
        )
        return envelope

    def _validate_authenticated_terminal(
        self,
        plan: Mapping[str, Any],
        envelope: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> None:
        """Verify an authenticated envelope before it can enter the ledger."""
        verification_deadline = (
            deadline if deadline is not None else time.monotonic() + 30
        )
        if time.monotonic() >= verification_deadline:
            raise C10Error("C10 terminal revalidation deadline expired")
        # Import locally to avoid a host-store/comparator import cycle.
        from . import compare

        compare.validate_authenticated_host_arm(plan, envelope)
        host = envelope["host_result"]
        root = host.get("receipt_root") if isinstance(host, Mapping) else None
        manifest = host.get("evidence_manifest") if isinstance(host, Mapping) else None
        public_key = (
            host.get("evidence_public_key") if isinstance(host, Mapping) else None
        )
        key_id = host.get("evidence_key_id") if isinstance(host, Mapping) else None
        if (
            not isinstance(root, Mapping)
            or set(root) != {"path", "id"}
            or not isinstance(root.get("path"), str)
            or not Path(root["path"]).is_absolute()
            or not isinstance(manifest, list)
            or len(manifest) != 17
            or not isinstance(public_key, str)
            or hashlib.sha256(public_key.encode("utf-8")).hexdigest() != key_id
        ):
            raise C10Error("C10 terminal receipt authority is invalid")
        with PrivateReceiptStore.create(Path(root["path"])) as receipts:
            if dict(receipts.descriptor()) != dict(root):
                raise C10Error("C10 terminal receipt root changed")
            evidence: list[Mapping[str, Any]] = []
            for position, artifact in enumerate(manifest):
                if time.monotonic() >= verification_deadline:
                    raise C10Error("C10 terminal revalidation deadline expired")
                if not isinstance(artifact, Mapping):
                    raise C10Error("C10 terminal receipt manifest is invalid")
                if not str(artifact.get("name", "")).startswith(
                    f"browser-evidence-{position}-"
                ):
                    raise C10Error("C10 terminal receipt manifest is unordered")
                try:
                    item = json.loads(receipts.read_sealed(artifact))
                except (OSError, json.JSONDecodeError) as exc:
                    raise C10Error(
                        "C10 terminal receipt artifact is unreadable"
                    ) from exc
                if not isinstance(item, Mapping):
                    raise C10Error("C10 terminal evidence is invalid")
                signature = item.get("evidenceSignature")
                unsigned = {
                    key: value
                    for key, value in item.items()
                    if key != "evidenceSignature"
                }
                verified = isinstance(signature, str) and _OpenSsl.verify(
                    public_key,
                    canonical_bytes(unsigned),
                    signature,
                    verification_deadline,
                )
                if time.monotonic() >= verification_deadline:
                    raise C10Error("C10 terminal revalidation deadline expired")
                if not verified:
                    raise C10Error("C10 terminal evidence signature is invalid")
                binding = unsigned.get("binding")
                if not isinstance(binding, Mapping) or any(
                    binding.get(key) != host["binding"].get(key)
                    for key in (
                        "planSha256",
                        "cohortSha256",
                        "sessionSha256",
                        "armSha256",
                        "profileSha256",
                    )
                ):
                    raise C10Error("C10 terminal evidence binding is invalid")
                evidence.append(item)
        if host.get("evidence_manifest_sha256") != sha256(evidence):
            raise C10Error("C10 terminal receipt manifest hash is invalid")
