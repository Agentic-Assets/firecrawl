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
import subprocess
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir

from .contracts import (
    C10Error,
    canonical_bytes,
    require_sha256,
    sha256,
    validate_plan,
)
from .host_crypto import _OpenSsl
from .host_registry import C10SealedCardRegistry
from .host_sidecar import C10EphemeralKeys, DockerComposeSidecar, SidecarLifecycle
from .host_store import C10SessionStore, PrivateReceiptStore

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
# `up -d` returns before the listener binds. Only connection refused/reset is
# a not-yet-ready signal; any HTTP response is verified strictly and at once.
_HEALTH_READINESS_SECONDS = 60.0
_HEALTH_POLL_INTERVAL_SECONDS = 0.25
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
    "admissionLane",
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


def _graphql_errors_absent(payload: Mapping[str, Any]) -> bool:
    """An absent or empty GraphQL ``errors`` array means no errors.

    Any other value (non-empty, null, or a non-array) is a failure.  The
    sidecar and the JLL selection rule share this exact contract.
    """
    errors = payload.get("errors", [])
    return isinstance(errors, list) and not errors


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise C10Error("C10 lifecycle deadline expired")
    return value


class _C10HostTransport:
    """Private transport mechanics; production.py owns lifecycle authority."""

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

    def _retired_direct_execution(
        self,
        plan: Mapping[str, Any],
        *,
        timeout_seconds: float,
        _claim: Mapping[str, Any],
        _held_shared_lock: SharedLock,
        _deadline: float,
    ) -> Mapping[str, Any]:
        """Refuse the removed direct host execution surface.

        This private primitive deliberately cannot acquire a lock or create a
        claim. The canonical production entrypoint owns runtime preflight,
        P1 approval/transition, settlement, rollback, and authorization of the
        supplied lock-held claim before this host may start a sidecar.
        """
        raise C10Error(
            "C10 direct host execution is retired; use production.execute_production_arm"
        )
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise C10Error("C10 lifecycle timeout is outside its reviewed bound")
        validate_plan(plan)
        # Reject an alternate but syntactically valid Plan B before lock
        # acquisition, durable claim, key generation, or Compose activity.
        self.cards.assert_plan_identity(plan)
        deadline = _deadline
        _remaining(deadline)
        lock = _held_shared_lock
        if lock.path.resolve() != canonical_shared_lock_dir(self.repo_root).resolve():
            raise C10Error("C10 host rejected a noncanonical SharedLock identity")
        try:
            descriptor = lock._owned_directory_fd()
        except Exception as exc:
            raise C10Error("C10 host requires the owned canonical SharedLock") from exc
        os.close(descriptor)
        sidecar_attempted = False
        claim: Mapping[str, Any] = _claim
        result: Mapping[str, Any] | None = None
        try:
            durable = self.session_store.read_bound(plan, _claim)
            if dict(durable) != dict(_claim):
                raise C10Error("C10 host rejects an unbound durable claim")
            claim = durable
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
                    endpoint, claim, plan, profile, keys, store, deadline
                )
                result = {
                    "claim": claim,
                    "receipt_root": store.descriptor(),
                    "evidence_manifest": artifacts,
                    "evidence_manifest_sha256": sha256(evidence),
                    "evidence_public_key": keys.sidecar_public_pem,
                    "evidence_key_id": _key_id(keys.sidecar_public_pem),
                    "binding": evidence[0]["binding"],
                }
        except BaseException as exc:
            lock.retain_on_exit = True
            self.session_store._controller_record_quarantine(claim, str(exc))
            self.quarantine(str(exc))
            raise
        finally:
            try:
                if sidecar_attempted:
                    self.sidecar.stop(deadline)
            except BaseException as cleanup_error:
                lock.retain_on_exit = True
                self.session_store._controller_record_quarantine(
                    claim, str(cleanup_error)
                )
                self.quarantine(f"C10 sidecar cleanup failed: {cleanup_error}")
                raise
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
        enumeration = self._run_child(issued_cards[0], deadline)
        self._verify_evidence(enumeration, issued_cards[0], keys, deadline)
        evidence: list[Mapping[str, Any]] = [enumeration]
        pool = ThreadPoolExecutor(max_workers=target, thread_name_prefix="c10-jll")
        try:
            futures = {
                pool.submit(self._run_child, issued, deadline): issued
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
        *,
        admission_lane: str | None = None,
    ) -> None:
        """Verify signed health, including the sidecar's lane.

        P0/P1 calibration requires a strict sidecar (``admissionLane`` null);
        only the JLL admission action may require its named admission lane.
        """
        raw = self._poll_health(endpoint, keys, deadline)
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
            or unsigned.get("admissionLane") != admission_lane
        ):
            raise C10Error("C10 signed health binding is invalid")

    @staticmethod
    def _poll_health(endpoint: str, keys: C10EphemeralKeys, deadline: float) -> Any:
        """Wait, bounded, for the listener to accept; never retry a response.

        Connection refused or reset (including a docker-proxy accept followed
        by an immediate close) means the sidecar is not listening yet.  Every
        other failure, and every HTTP response, is returned to strict signed
        verification or fails closed immediately.
        """
        readiness_deadline = min(deadline, time.monotonic() + _HEALTH_READINESS_SECONDS)
        while True:
            request = Request(
                f"{endpoint}/health",
                headers={"x-firecrawl-host-transport-key": keys.transport_key},
            )
            try:
                with urlopen(request, timeout=_remaining(deadline)) as response:
                    return json.loads(response.read(_MAX_PRIVATE_ARTIFACT + 1))
            except Exception as exc:
                reason = exc.reason if isinstance(exc, URLError) else exc
                not_listening = isinstance(
                    reason, ConnectionRefusedError | ConnectionResetError
                ) and not isinstance(exc, HTTPError)
                if (
                    not not_listening
                    or time.monotonic() + _HEALTH_POLL_INTERVAL_SECONDS
                    >= readiness_deadline
                ):
                    raise C10Error("C10 signed sidecar health is unavailable") from exc
            time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)

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
                # `--import tsx` resolves from cwd; only the collector package
                # (not the repository root) owns the pinned tsx dependency.
                cwd=self.repo_root / "scripts/firecrawl-ops/cre_collector",
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
        *,
        admission_enumeration: bool = False,
    ) -> None:
        """Verify signed sidecar evidence for one issued card.

        P0/P1 enumeration cards must carry their sixteen sealed member routes.
        Only the JLL admission controller, which recomputes membership from
        this very body before issuing any member card, may omit them.
        """
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
        status = unsigned.get("status")
        final_url = unsigned.get("finalUrl")
        content_type = unsigned.get("contentType")
        expected_url = issued["card"].get("url")
        accepted_content_types = {
            "application/json",
            "text/html",
            "application/xhtml+xml",
        }
        normalized_content_type = (
            content_type.split(";", 1)[0].strip().lower()
            if isinstance(content_type, str)
            else None
        )
        if (
            type(status) is not int
            or not 200 <= status < 300
            or not isinstance(expected_url, str)
            or final_url != expected_url
            or normalized_content_type not in accepted_content_types
            or unsigned.get("challengeDetected") is not False
        ):
            raise C10Error("C10 sidecar evidence is not an accepted reviewed response")
        card = issued.get("card")
        if not isinstance(card, Mapping):
            raise C10Error("C10 issued card is invalid")
        if card.get("stage") == "enumeration":
            expected = card.get("expectedMemberRoutes")
            allowed_host = card.get("allowedHost")
            if admission_enumeration:
                if not isinstance(allowed_host, str) or expected is not None:
                    raise C10Error("C10 admission enumeration card is invalid")
            elif (
                not isinstance(expected, list)
                or len(expected) != 16
                or not all(isinstance(route, str) for route in expected)
                or not isinstance(allowed_host, str)
            ):
                raise C10Error("C10 enumeration card lacks sealed membership")
            try:
                payload = json.loads(base64.b64decode(body, validate=True))
                items = payload["data"]["properties"]["items"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise C10Error("C10 enumeration evidence is not usable JSON") from exc
            if (
                not isinstance(payload, Mapping)
                or not _graphql_errors_absent(payload)
                or not isinstance(items, list)
            ):
                raise C10Error("C10 enumeration evidence contains no accepted cohort")
            observed: set[str] = set()
            for item in items:
                route = item.get("pageUrl") if isinstance(item, Mapping) else None
                if not isinstance(route, str):
                    continue
                parsed = urlsplit(urljoin(f"https://{allowed_host}", route))
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != allowed_host
                    or parsed.query
                    or parsed.fragment
                ):
                    continue
                observed.add(f"https://{parsed.netloc}{parsed.path}".rstrip("/"))
            if not admission_enumeration and not set(expected).issubset(observed):
                raise C10Error("C10 enumeration evidence does not bind sealed cohort")
