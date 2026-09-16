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
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.request import Request, urlopen

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir

from .contracts import (
    C10Error,
    canonical_bytes,
    claim_next_arm,
    require_sha256,
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

    def read_bound(
        self, plan: Mapping[str, Any], session: Mapping[str, Any]
    ) -> Mapping[str, Any]:
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


class C10SealedCardRegistry:
    """Immutable host-owned JLL cards. C10 never accepts a caller-provided URL."""

    def __init__(
        self, plan: Mapping[str, Any], cards: Mapping[str, Mapping[str, Any]]
    ) -> None:
        validate_plan(plan)
        if not cards:
            raise C10Error("C10 sealed registry cannot be empty")
        frozen: dict[str, Mapping[str, Any]] = {}
        for card_id, raw in cards.items():
            card = _safe_json(raw, "C10 sealed card")
            if card_id != card.get("id") or card.get("sourceKey") != "jll":
                raise C10Error("C10 registry permits only exact sealed JLL cards")
            if (
                card.get("cacheMode") != "no-store"
                or not isinstance(card.get("maxBytes"), int)
                or not 0 < card["maxBytes"] <= _MAX_RAW_BODY_BYTES
            ):
                raise C10Error(
                    "C10 sealed card violates its reviewed body/cache bounds"
                )
            frozen[card_id] = json.loads(_canonical_text(card))
        self._cards = frozen
        self.manifest_sha256 = sha256(frozen)

    def resolve(self, card_id: str) -> Mapping[str, Any]:
        if not isinstance(card_id, str) or card_id not in self._cards:
            raise C10Error("C10 rejects a card outside the sealed registry")
        return self._cards[card_id]


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
            "playwright-service",
        ]
        env = {
            **os.environ,
            **environment,
            "C10_BROWSER_PRIVATE_ENV_FILE": str(self._env_file),
            "C10_BROWSER_HOST_PORT": str(port),
        }
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
        if rendered.returncode != 0 or not self._rendered_loopback_only(
            rendered.stdout, port
        ):
            self._remove_env()
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

    def stop(self, deadline: float) -> None:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yaml",
                    "-f",
                    "docker-compose.c10.yaml",
                    "stop",
                    "playwright-service",
                ],
                cwd=self.repo_root,
                capture_output=True,
                timeout=_remaining(deadline),
                check=False,
            )
            if result.returncode != 0:
                raise C10Error("C10 sidecar stop was not confirmed")
        finally:
            self._remove_env()

    @staticmethod
    def _rendered_loopback_only(rendered: str, port: int) -> bool:
        try:
            ports = json.loads(rendered)["services"]["playwright-service"]["ports"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        return any(
            isinstance(item, Mapping)
            and item.get("host_ip") == "127.0.0.1"
            and str(item.get("published")) == str(port)
            and int(item.get("target", 0)) == 3004
            for item in ports
        )

    def _remove_env(self) -> None:
        if self._env_file is not None:
            self._env_file.unlink(missing_ok=True)
            self._env_file = None


class C10HostExecutionSession:
    """One lock-held v3 lifecycle; failures quarantine before the lock releases."""

    def __init__(
        self,
        *,
        repo_root: Path,
        session_store: C10SessionStore,
        private_root: Path,
        cards: C10SealedCardRegistry,
        sidecar: SidecarLifecycle | None = None,
        quarantine: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.lock_path = canonical_shared_lock_dir(self.repo_root).resolve()
        self.session_store, self.private_root = session_store, private_root
        self.cards = cards
        self.sidecar = sidecar or DockerComposeSidecar(repo_root)
        self.quarantine = quarantine or (lambda _reason: None)

    def execute(
        self,
        plan: Mapping[str, Any],
        session: Mapping[str, Any],
        card_id: str,
        *,
        timeout_seconds: float = 120,
        child: Callable[[Mapping[str, Any], float], Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise C10Error("C10 lifecycle timeout is outside its reviewed bound")
        validate_plan(plan)
        card = self.cards.resolve(card_id)
        deadline = time.monotonic() + timeout_seconds
        lock = SharedLock(self.lock_path)
        if lock.path.resolve() != canonical_shared_lock_dir(self.repo_root).resolve():
            raise C10Error("C10 host rejected a noncanonical SharedLock identity")
        lock.acquire()
        sidecar_attempted = False
        claim: Mapping[str, Any] | None = None
        try:
            claim = self.session_store.claim(plan, session)
            with PrivateReceiptStore.create(self.private_root) as store:
                keys = self._keys(deadline)
                port = self._free_loopback_port()
                arm = _safe_json(claim["arm"], "C10 arm")
                profile = _safe_json(plan["profiles"][arm["variant"]], "C10 profile")
                sidecar_attempted = True
                self.sidecar.start(
                    {
                        "C10_COORDINATOR_PUBLIC_KEY_PEM": keys.coordinator_public_pem,
                        "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM": keys.sidecar_private_pem,
                        "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY": keys.transport_key,
                        "MAX_CONCURRENT_PAGES": str(
                            profile["requested"]["global_pages"]
                        ),
                        "C10_PROFILE_SHA256": sha256(profile["requested"]),
                        "C10_BROWSER_CPUS": str(profile["requested"]["browser_cpus"]),
                    },
                    port,
                    deadline,
                )
                endpoint = f"http://127.0.0.1:{port}"
                self._verify_health(endpoint, keys, profile, deadline)
                issued = self._issue(endpoint, claim, plan, card, keys, deadline)
                evidence = (child or self._run_child)(issued, deadline)
                self._verify_evidence(evidence, issued, keys, deadline)
                private = store.seal_json("browser-evidence", evidence)
                return {
                    "claim": claim,
                    "private_artifact": private,
                    "evidence_sha256": sha256(evidence),
                    "binding": issued["capability"]["binding"],
                }
        except BaseException as exc:
            lock.retain_on_exit = True
            self.session_store.record_quarantine(claim, str(exc))
            self.quarantine(str(exc))
            raise
        finally:
            try:
                if sidecar_attempted:
                    self.sidecar.stop(deadline)
            except BaseException as cleanup_error:
                lock.retain_on_exit = True
                self.session_store.record_quarantine(claim, str(cleanup_error))
                self.quarantine(f"C10 sidecar cleanup failed: {cleanup_error}")
                raise
            finally:
                lock.release()

    def _keys(self, deadline: float) -> C10EphemeralKeys:
        coordinator_private, coordinator_public = _OpenSsl.pair(deadline)
        sidecar_private, sidecar_public = _OpenSsl.pair(deadline)
        return C10EphemeralKeys(
            coordinator_private,
            coordinator_public,
            sidecar_private,
            sidecar_public,
            secrets.token_urlsafe(32),
        )

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _verify_health(
        self,
        endpoint: str,
        keys: C10EphemeralKeys,
        profile: Mapping[str, Any],
        deadline: float,
    ) -> None:
        request = Request(
            f"{endpoint}/health",
            headers={"x-firecrawl-host-transport-key": keys.transport_key},
        )
        try:
            with urlopen(request, timeout=_remaining(deadline)) as response:
                raw = json.loads(response.read(_MAX_PRIVATE_ARTIFACT + 1))
        except Exception as exc:
            raise C10Error("C10 signed sidecar health is unavailable") from exc
        health = _safe_json(raw, "C10 signed health")
        if set(health) != _HEALTH_FIELDS | {"profileSha256"}:
            raise C10Error("C10 signed health schema is invalid")
        signature = health["healthSignature"]
        unsigned = {
            key: value for key, value in health.items() if key != "healthSignature"
        }
        if not isinstance(signature, str) or not _OpenSsl.verify(
            keys.sidecar_public_pem, canonical_bytes(unsigned), signature, deadline
        ):
            raise C10Error("C10 signed health attestation is invalid")
        if (
            unsigned.get("protocolVersion") != 3
            or unsigned.get("status") != "healthy"
            or unsigned.get("transport") != "docker-loopback-tcp"
            or unsigned.get("coordinatorKeyId") != _key_id(keys.coordinator_public_pem)
            or unsigned.get("evidenceKeyId") != _key_id(keys.sidecar_public_pem)
            or unsigned.get("configuredCapacity")
            != profile["requested"]["global_pages"]
            or unsigned.get("profileSha256") != sha256(profile["requested"])
        ):
            raise C10Error("C10 signed health binding is invalid")

    def _issue(
        self,
        endpoint: str,
        claim: Mapping[str, Any],
        plan: Mapping[str, Any],
        card: Mapping[str, Any],
        keys: C10EphemeralKeys,
        deadline: float,
    ) -> Mapping[str, Any]:
        card_digest = sha256(card)
        arm = _safe_json(claim["arm"], "C10 arm")
        profile = _safe_json(plan["profiles"][arm["variant"]], "C10 profile")
        session_digest = require_sha256(
            claim.get("session_sha256"), "C10 durable session"
        )
        source_key = card.get("sourceKey")
        if not isinstance(source_key, str) or not source_key:
            raise C10Error("C10 issued card lacks a source binding")
        binding = {
            "planSha256": plan["plan_sha256"],
            "cohortSha256": plan["cohort_sha256"],
            "cardSha256": card_digest,
            "manifestSha256": self.cards.manifest_sha256,
            "sessionSha256": session_digest,
            "armSha256": sha256(arm),
            "profileSha256": sha256(profile["requested"]),
        }
        capability = {
            "protocolVersion": 3,
            "coordinatorKeyId": _key_id(keys.coordinator_public_pem),
            "nonce": secrets.token_urlsafe(32),
            "expiresAtMs": int(
                time.time() * 1000 + min(_remaining(deadline) * 1000, 120_000)
            ),
            "sourceKey": source_key,
            "binding": binding,
        }
        payload = (
            base64.urlsafe_b64encode(canonical_bytes(capability))
            .decode("ascii")
            .rstrip("=")
        )
        authorization = f"{payload}.{_OpenSsl.sign(keys.coordinator_private_pem, payload.encode('utf-8'), deadline)}"
        return {
            "endpoint": endpoint,
            "capability": capability,
            "authorization": authorization,
            "card": card,
            "hostTransportKey": keys.transport_key,
        }

    def _run_child(
        self, issued: Mapping[str, Any], deadline: float
    ) -> Mapping[str, Any]:
        # The child receives no private key or mutable controller paths.
        payload = dict(issued)
        script = (
            self.repo_root
            / "scripts/firecrawl-ops/cre_collector/capacity_c10/receipts/issued_browser_child.ts"
        )
        try:
            process = subprocess.Popen(
                ["node", "--import", "tsx", str(script)],
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.repo_root,
            )
            assert process.stdin is not None and process.stdout is not None
            frame = _canonical_text(payload) + "\n"
            if len(frame.encode("utf-8")) > _MAX_CHILD_FRAME_BYTES:
                raise C10Error("C10 issued child frame exceeds its bound")
            process.stdin.write(frame)
            process.stdin.close()
            output = process.stdout.read(_MAX_CHILD_STDOUT_BYTES + 1)
            if len(output.encode("utf-8")) > _MAX_CHILD_STDOUT_BYTES:
                process.kill()
                raise C10Error("C10 issued child output exceeds its bound")
            if process.wait(timeout=_remaining(deadline)) != 0:
                raise C10Error("C10 issued browser child failed")
            return _safe_json(json.loads(output), "C10 issued browser child evidence")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise C10Error("C10 issued browser child failed") from exc

    def _verify_evidence(
        self,
        raw: Mapping[str, Any],
        issued: Mapping[str, Any],
        keys: C10EphemeralKeys,
        deadline: float,
    ) -> None:
        evidence = _safe_json(raw, "C10 sidecar evidence")
        if set(evidence) != _EVIDENCE_FIELDS:
            raise C10Error("C10 sidecar evidence schema is invalid")
        signature = evidence["evidenceSignature"]
        unsigned = {
            key: value for key, value in evidence.items() if key != "evidenceSignature"
        }
        if not isinstance(signature, str) or not _OpenSsl.verify(
            keys.sidecar_public_pem, canonical_bytes(unsigned), signature, deadline
        ):
            raise C10Error("C10 sidecar evidence signature is invalid")
        if (
            unsigned.get("protocolVersion") != 3
            or unsigned.get("binding") != issued["capability"]["binding"]
            or unsigned.get("cacheRead") is not False
            or unsigned.get("cacheWrite") is not False
        ):
            raise C10Error("C10 sidecar evidence binding is invalid")
        body = unsigned.get("bodyBase64")
        if not isinstance(body, str) or len(
            base64.b64decode(body, validate=True)
        ) > int(issued["card"].get("maxBytes", 0)):
            raise C10Error("C10 sidecar evidence body exceeds its issued card bound")
