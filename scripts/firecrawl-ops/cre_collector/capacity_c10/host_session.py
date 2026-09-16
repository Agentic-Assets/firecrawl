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
import selectors
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import urlsplit, urlunsplit
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
    """The only C10 request graph: one fixed JLL enumeration and 16 members.

    Construction intentionally takes a hash-bound v1 cohort, never an
    arbitrary card map.  The route bytes and GraphQL body are manufactured by
    the host from the immutable selected JLL membership.
    """

    def __init__(self, plan: Mapping[str, Any], cohort: Mapping[str, Any]) -> None:
        validate_plan(plan)
        if cohort.get("cohort_sha256") != plan["cohort_sha256"] or sha256(
            {key: cohort.get(key) for key in _COHORT_HASH_FIELDS}
        ) != cohort.get("cohort_sha256"):
            raise C10Error("C10 registry rejects a different plan or cohort")
        sources = cohort.get("sources")
        if not isinstance(sources, list) or len(sources) != 20:
            raise C10Error("C10 registry cohort is invalid")
        self.source_projection_sha256 = self._validate_source_projection(plan, sources)
        jll = next(
            (
                value
                for value in sources
                if isinstance(value, Mapping) and value.get("source_key") == "jll"
            ),
            None,
        )
        if not isinstance(jll, Mapping) or jll.get("core_state") != "ready":
            raise C10Error("C10 registry requires the sealed JLL cohort")
        members = jll.get("core")
        fresh = jll.get("fresh_enumeration")
        if (
            not isinstance(members, list)
            or len(members) != 16
            or jll.get("core_selected_rows") != 16
            or jll.get("core_target_rows") != 16
            or not isinstance(fresh, Mapping)
            or fresh.get("population_state") != "verified"
        ):
            raise C10Error("C10 registry requires exactly sixteen sealed JLL members")
        require_sha256(fresh.get("receipt_sha256"), "JLL enumeration receipt")
        frozen: dict[str, Mapping[str, Any]] = {
            "jll-enumeration": self._enumeration_card()
        }
        observed_routes: set[str] = set()
        observed_ids: set[str] = set()
        for index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise C10Error("C10 JLL member is invalid")
            provider_id = member.get("provider_id")
            route = self._member_route(member.get("canonical_url"))
            if (
                not isinstance(provider_id, str)
                or not provider_id.isdigit()
                or provider_id in observed_ids
                or route in observed_routes
            ):
                raise C10Error(
                    "C10 JLL membership does not have exact canonical identity"
                )
            observed_ids.add(provider_id)
            observed_routes.add(route)
            frozen[f"jll-member-{index}"] = self._member_card(index, route)
        # Keep canonical bytes, not caller-reachable mutable dictionaries. A
        # resolve always returns a fresh decoded projection for one capability.
        self._cards = {
            card_id: _canonical_text(card) for card_id, card in frozen.items()
        }
        self.cohort_sha256 = plan["cohort_sha256"]
        self.plan_sha256 = plan["plan_sha256"]
        self.manifest_sha256 = sha256(
            {card_id: json.loads(card) for card_id, card in self._cards.items()}
        )

    @staticmethod
    def _validate_source_projection(plan: Mapping[str, Any], sources: list[Any]) -> str:
        """Bind all twenty plan source projections to the sealed cohort.

        A plan hash alone is not enough at this boundary: a forged Plan B can
        retain the same cohort hash while changing its source projection.  The
        registry retains the exact admitted projection and execute compares it
        before it acquires a lock or creates any lifecycle material.
        """
        plan_sources = plan.get("sources")
        if not isinstance(plan_sources, list) or len(plan_sources) != 20:
            raise C10Error("C10 registry plan lacks the twenty-source contract")
        by_key: dict[str, Mapping[str, Any]] = {}
        for source in sources:
            if not isinstance(source, Mapping) or not isinstance(
                source.get("source_key"), str
            ):
                raise C10Error("C10 registry cohort source is invalid")
            key = source["source_key"]
            if key in by_key:
                raise C10Error("C10 registry cohort source keys are not unique")
            core, fresh = source.get("core"), source.get("fresh_enumeration")
            if not isinstance(core, list) or not isinstance(fresh, Mapping):
                raise C10Error("C10 registry cohort source projection is invalid")
            by_key[key] = source
        if len(by_key) != 20:
            raise C10Error("C10 registry cohort source count is invalid")
        seen: set[str] = set()
        for projection in plan_sources:
            if not isinstance(projection, Mapping):
                raise C10Error("C10 registry plan source projection is invalid")
            key = projection.get("key")
            source = by_key.get(key) if isinstance(key, str) else None
            if source is None or key in seen:
                raise C10Error("C10 registry plan source set differs from cohort")
            seen.add(key)
            core = source["core"]
            fresh = source["fresh_enumeration"]
            if (
                projection.get("plane") != source.get("plane")
                or projection.get("cohort_member_count") != len(core)
                or projection.get("cohort_member_sha256") != sha256(core)
                or projection.get("enumeration_receipt_sha256")
                != fresh.get("receipt_sha256")
            ):
                raise C10Error(
                    "C10 registry plan source projection differs from cohort"
                )
        if seen != set(by_key):
            raise C10Error("C10 registry plan source set differs from cohort")
        return sha256(plan_sources)

    @staticmethod
    def _member_route(value: Any) -> str:
        if not isinstance(value, str):
            raise C10Error("C10 JLL member lacks a canonical route")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != _JLL_HOST
            or not parsed.path.startswith("/listings/")
            or parsed.path.rstrip("/") == "/listings"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise C10Error("C10 JLL member route is outside the reviewed path")
        return urlunsplit(("https", _JLL_HOST, parsed.path.rstrip("/"), "", ""))

    @staticmethod
    def _card(
        *,
        card_id: str,
        stage: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content_type: str | None,
        body: str | None,
    ) -> Mapping[str, Any]:
        return {
            "id": card_id,
            "sourceKey": "jll",
            "stage": stage,
            "method": method,
            "url": url,
            "allowedHost": _JLL_HOST,
            "headers": dict(headers),
            "contentType": content_type,
            "body": body,
            "browserBootstrapUrl": _JLL_BOOTSTRAP_URL,
            "cacheMode": "no-store",
            "timeoutMs": _MAX_CARD_TIMEOUT_MS,
            "maxBytes": _MAX_RAW_BODY_BYTES,
            "bodySha256": hashlib.sha256(body.encode("utf-8")).hexdigest()
            if body is not None
            else None,
        }

    @classmethod
    def _enumeration_card(cls) -> Mapping[str, Any]:
        variables = {
            "market": "us",
            "language": "en",
            "propertyTypes": [_JLL_RECIPE["property_type"]],
            "tenureTypes": ["sale"],
            "skip": 0,
            "take": 50,
            "orderBy": {
                "field": "dateModified",
                "direction": "desc",
                "imagePriority": True,
            },
        }
        return cls._card(
            card_id="jll-enumeration",
            stage="enumeration",
            method="POST",
            url=f"https://{_JLL_HOST}/api/graphql",
            headers={
                "accept": "application/json",
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            content_type="application/json",
            body=_canonical_text(
                {
                    "operationName": "SearchResults",
                    "query": _JLL_QUERY,
                    "variables": variables,
                }
            ),
        )

    @classmethod
    def _member_card(cls, index: int, route: str) -> Mapping[str, Any]:
        return cls._card(
            card_id=f"jll-member-{index}",
            stage="member",
            method="GET",
            url=route,
            headers={"accept": "text/html,application/xhtml+xml"},
            content_type=None,
            body=None,
        )

    def resolve(self, card_id: str) -> Mapping[str, Any]:
        if not isinstance(card_id, str) or card_id not in self._cards:
            raise C10Error("C10 rejects a card outside the sealed registry")
        return json.loads(self._cards[card_id])

    def assert_plan_identity(self, plan: Mapping[str, Any]) -> None:
        if (
            self.plan_sha256 != plan.get("plan_sha256")
            or self.cohort_sha256 != plan.get("cohort_sha256")
            or self.source_projection_sha256 != sha256(plan.get("sources"))
        ):
            raise C10Error("C10 registry rejects an alternate plan/source projection")


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
        *,
        timeout_seconds: float = 120,
        child: Callable[[Mapping[str, Any], float], Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise C10Error("C10 lifecycle timeout is outside its reviewed bound")
        validate_plan(plan)
        # Reject an alternate but syntactically valid Plan B before lock
        # acquisition, durable claim, key generation, or Compose activity.
        self.cards.assert_plan_identity(plan)
        deadline = time.monotonic() + timeout_seconds
        lock = SharedLock(self.lock_path)
        if lock.path.resolve() != canonical_shared_lock_dir(self.repo_root).resolve():
            raise C10Error("C10 host rejected a noncanonical SharedLock identity")
        lock.acquire()
        sidecar_attempted = False
        claim: Mapping[str, Any] | None = None
        result: Mapping[str, Any] | None = None
        try:
            claim = self.session_store.claim(plan, session)
            with PrivateReceiptStore.create(self.private_root) as store:
                keys = self._keys(deadline)
                port = self._free_loopback_port()
                arm = _safe_json(claim["arm"], "C10 arm")
                profile = _safe_json(plan["profiles"][arm["variant"]], "C10 profile")
                # Cleanup responsibility begins before Compose is invoked: an
                # `up` timeout can leave a container even when start raises.
                sidecar_attempted = True
                self.sidecar.start(
                    {
                        "C10_COORDINATOR_PUBLIC_KEY_PEM_B64": base64.b64encode(
                            keys.coordinator_public_pem.encode("utf-8")
                        ).decode("ascii"),
                        "C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64": base64.b64encode(
                            keys.sidecar_private_pem.encode("utf-8")
                        ).decode("ascii"),
                        "PLAYWRIGHT_HOST_TRANSPORT_V3_KEY": keys.transport_key,
                        "MAX_CONCURRENT_PAGES": str(
                            profile["requested"]["global_pages"]
                        ),
                        "C10_PROFILE_SHA256": sha256(profile["requested"]),
                        "C10_BROWSER_CPUS": str(profile["requested"]["browser_cpus"]),
                        "C10_BROWSER_PIDS": str(profile["requested"]["browser_pids"]),
                    },
                    port,
                    deadline,
                )
                endpoint = f"http://127.0.0.1:{port}"
                self._verify_health(endpoint, keys, profile, deadline)
                evidence, artifacts = self._run_cohort(
                    endpoint, claim, plan, profile, keys, store, deadline, child
                )
                result = {
                    "claim": claim,
                    "private_artifacts": artifacts,
                    "evidence_manifest_sha256": sha256(evidence),
                    "binding": evidence[0]["binding"],
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
        if result is None:
            raise C10Error("C10 host did not produce a terminal cohort result")
        return result

    def _run_cohort(
        self,
        endpoint: str,
        claim: Mapping[str, Any],
        plan: Mapping[str, Any],
        profile: Mapping[str, Any],
        keys: C10EphemeralKeys,
        store: PrivateReceiptStore,
        deadline: float,
        child: Callable[[Mapping[str, Any], float], Mapping[str, Any]] | None,
    ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        """Run the one enumeration then all 16 immutable members at P0/P1.

        The sidecar supplies signed lease intervals.  The coordinator derives
        saturation itself, instead of accepting a caller's throughput scalar.
        """
        target = profile["requested"].get("global_pages")
        if target not in {4, 10} or target != profile["requested"].get(
            "jll_detail_concurrency"
        ):
            raise C10Error("C10 profile has no reviewed P0/P1 JLL capacity")
        execute_child = child or self._run_child
        issued_cards = [
            self._issue(
                endpoint,
                claim,
                plan,
                self.cards.resolve("jll-enumeration"),
                keys,
                deadline,
                0,
            )
        ]
        issued_cards.extend(
            self._issue(
                endpoint,
                claim,
                plan,
                self.cards.resolve(f"jll-member-{index}"),
                keys,
                deadline,
                index + 1,
            )
            for index in range(16)
        )
        # The native enumeration is a required predecessor of every selected
        # detail member. It is still host-issued and sealed like every card.
        enumeration = execute_child(issued_cards[0], deadline)
        self._verify_evidence(enumeration, issued_cards[0], keys, deadline)
        evidence: list[Mapping[str, Any]] = [enumeration]
        pool = ThreadPoolExecutor(max_workers=target, thread_name_prefix="c10-jll")
        try:
            futures = {
                pool.submit(execute_child, issued, deadline): issued
                for issued in issued_cards[1:]
            }
            for future in as_completed(futures, timeout=_remaining(deadline)):
                issued = futures[future]
                raw = future.result(timeout=_remaining(deadline))
                self._verify_evidence(raw, issued, keys, deadline)
                evidence.append(raw)
        finally:
            # An actual child is process-group-killed by _run_child. Do not let
            # an injected/hung test transport make the lifecycle wait past its
            # one deadline while unwinding this host authority scope.
            pool.shutdown(wait=False, cancel_futures=True)
        if len(evidence) != 17:
            raise C10Error(
                "C10 cohort did not execute its exact enumeration and members"
            )
        self._verify_saturation(evidence[1:], int(target))
        artifacts = [
            store.seal_json(f"browser-evidence-{index}", item)
            for index, item in enumerate(evidence)
        ]
        return evidence, artifacts

    @staticmethod
    def _verify_saturation(evidence: list[Mapping[str, Any]], target: int) -> None:
        if len(evidence) != 16:
            raise C10Error("C10 saturation requires all sixteen JLL members")
        intervals: list[tuple[int, int]] = []
        observed: list[int] = []
        for item in evidence:
            unsigned = _safe_json(item, "C10 member evidence")
            try:
                start = int(unsigned["leaseStartMonotonicNs"])
                end = int(unsigned["leaseEndMonotonicNs"])
                active = unsigned["observedActivePages"]
                capacity = unsigned["configuredCapacity"]
            except (KeyError, TypeError, ValueError) as exc:
                raise C10Error("C10 signed member lease evidence is malformed") from exc
            if start >= end or type(active) is not int or capacity != target:
                raise C10Error(
                    "C10 signed member lease does not bind the target capacity"
                )
            intervals.append((start, end))
            observed.append(active)
        events = sorted(
            (point, delta)
            for start, end in intervals
            for point, delta in ((start, 1), (end, -1))
        )
        active, maximum = 0, 0
        for _, delta in events:
            active += delta
            maximum = max(maximum, active)
        if maximum != target or max(observed) != target:
            raise C10Error("C10 signed leases did not reach the exact P0/P1 target")

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
        sequence: int,
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
        remaining_ms = max(1, int(_remaining(deadline) * 1000))
        now_ms = int(time.time() * 1000)
        capability = {
            "protocolVersion": 3,
            "coordinatorKeyId": _key_id(keys.coordinator_public_pem),
            "nonce": secrets.token_urlsafe(32),
            "expiresAtMs": now_ms + min(remaining_ms, 120_000),
            "hostDeadlineAtMs": now_ms + remaining_ms,
            "cardSequence": sequence,
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
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.repo_root,
                start_new_session=True,
            )
            frame = (_canonical_text(payload) + "\n").encode("utf-8")
            if len(frame) > _MAX_CHILD_FRAME_BYTES:
                raise C10Error("C10 issued child frame exceeds its bound")
            output, _stderr = self._stream_child(process, frame, deadline)
            if process.returncode != 0:
                raise C10Error("C10 issued browser child failed")
            return _safe_json(
                json.loads(output.decode("utf-8")), "C10 issued browser child evidence"
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise C10Error("C10 issued browser child failed") from exc

    def _stream_child(
        self, process: subprocess.Popen[bytes], frame: bytes, deadline: float
    ) -> tuple[bytes, bytes]:
        """Bound every pipe while the child is still alive, never after it."""
        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._kill_child_group(process)
            raise C10Error("C10 issued browser child pipes are unavailable")
        selector = selectors.DefaultSelector()
        output, stderr, pending = bytearray(), bytearray(), memoryview(frame)
        streams = {process.stdout.fileno(): output, process.stderr.fileno(): stderr}
        try:
            for descriptor in (*streams, process.stdin.fileno()):
                os.set_blocking(descriptor, False)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                if _remaining(deadline) <= 0:
                    raise C10Error("C10 issued browser child exceeded its deadline")
                for key, _ in selector.select(min(_remaining(deadline), 0.1)):
                    if key.fileobj is process.stdin:
                        if pending:
                            sent = os.write(process.stdin.fileno(), pending)
                            pending = pending[sent:]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    destination = streams[key.fileobj.fileno()]
                    destination.extend(chunk)
                    limit = (
                        _MAX_CHILD_STDOUT_BYTES
                        if destination is output
                        else _MAX_CHILD_STDERR_BYTES
                    )
                    if len(destination) > limit:
                        raise C10Error("C10 issued child output exceeds its bound")
                if process.poll() is not None and not pending:
                    # Keep reading until both EOFs to avoid accepting a child
                    # that exits while buffered attacker bytes remain.
                    continue
            process.wait(timeout=_remaining(deadline))
            return bytes(output), bytes(stderr)
        except (OSError, subprocess.TimeoutExpired, C10Error):
            self._kill_child_group(process)
            raise
        finally:
            selector.close()

    @staticmethod
    def _kill_child_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, 9)
        except (ProcessLookupError, OSError):
            process.kill()

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
