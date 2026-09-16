"""Controller-owned, framed bridge for the JLL admission receipt set.

The public offline admission helpers deliberately cannot collect evidence.  This
module is their only executable counterpart: it owns the private receipt root,
the deadline, and C10 v3 capability issuance.  The TypeScript child receives no
provider URL authority, browser endpoint, key, or filesystem descriptor.  It
can ask this controller to execute one exactly pinned source card or seal one
private artifact, and nothing else.

Selection authority is shared, not delegated: the child's source selector
chooses the sixteen members, and this controller independently recomputes
``jll-canonical-url-lexicographic-v1`` from the verified signed enumeration body
before issuing any member capability.  Callers cannot supply members.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .admission_chain import _open_private_root
from .contracts import C10Error, canonical_bytes, require_sha256, sha256
from .host_crypto import _OpenSsl
from .host_orchestration import (
    _C10HostTransport,
    _key_id,
    _remaining,
)
from .host_sidecar import C10EphemeralKeys
from .host_store import PrivateReceiptStore
from .jll_admission import (
    JLL_ENUMERATION_BODY_SHA256,
    JLL_ENUMERATION_CARD_ID,
    JLL_GRAPHQL_URL,
    JLL_MEMBER_COUNT,
    JLL_RECEIPT_MANIFEST_KIND,
    _collection_intent_sha256,
    _jll_intent,
    _parse_enumeration_body,
    select_jll_admission_members,
)

_PROTOCOL = "c10-jll-admission-rpc-v1"
_JLL_HOST = "property.jll.com"
_JLL_BOOTSTRAP_URL = "https://property.jll.com/"
# Mirrors MAX_FRAME_BYTES in receipts/jll_admission_child.ts.  A reply carries a
# response body (<= 2 MiB) twice in base64 inside its signed evidence.
_MAX_ADMISSION_FRAME_BYTES = 8 * 1024 * 1024
# One enumeration plus sixteen members seal at most ~70 artifacts; bounded
# headroom prevents a misbehaving child from filling the private root.
_MAX_SEALED_ARTIFACTS = 96
_MAX_SEALED_BYTES = 160 * 1024 * 1024
_STEM = re.compile(r"[a-z0-9][a-z0-9-]{0,160}")
_REQUEST_ID = re.compile(r"[1-9][0-9]{0,8}")
_CARD_TIMEOUT_MS = 30_000
_CARD_MAX_BYTES = 2 * 1024 * 1024
_BINDING_FIELDS = frozenset(
    {
        "planSha256",
        "cohortSha256",
        "policySha256",
        "sourceSha256",
        "armSha256",
        "implementationSha256",
    }
)


def _frame(value: Mapping[str, Any]) -> bytes:
    raw = canonical_bytes(value)
    if len(raw) > _MAX_ADMISSION_FRAME_BYTES:
        raise C10Error("JLL admission controller frame exceeds its bound")
    return raw + b"\n"


def _expected_enumeration_card(card: Mapping[str, Any]) -> dict[str, Any]:
    body = card.get("body")
    if (
        not isinstance(body, str)
        or hashlib.sha256(body.encode("utf-8")).hexdigest()
        != JLL_ENUMERATION_BODY_SHA256
        or card.get("bodySha256") != JLL_ENUMERATION_BODY_SHA256
    ):
        raise C10Error("JLL admission enumeration card is invalid")
    return {
        "id": JLL_ENUMERATION_CARD_ID,
        "sourceKey": "jll",
        "stage": "enumeration",
        "method": "POST",
        "url": JLL_GRAPHQL_URL,
        "allowedHost": _JLL_HOST,
        "headers": {
            "accept": "application/json",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "pragma": "no-cache",
        },
        "contentType": "application/json",
        "body": body,
        "browserBootstrapUrl": _JLL_BOOTSTRAP_URL,
        "cacheMode": "no-store",
        "timeoutMs": _CARD_TIMEOUT_MS,
        "maxBytes": _CARD_MAX_BYTES,
        "bodySha256": JLL_ENUMERATION_BODY_SHA256,
    }


def _expected_member_card(index: int, route: str) -> dict[str, Any]:
    return {
        "id": f"jll-member-{index}",
        "sourceKey": "jll",
        "stage": "member",
        "method": "GET",
        "url": route,
        "allowedHost": _JLL_HOST,
        "headers": {"accept": "text/html,application/xhtml+xml"},
        "contentType": None,
        "body": None,
        "browserBootstrapUrl": _JLL_BOOTSTRAP_URL,
        "cacheMode": "no-store",
        "timeoutMs": _CARD_TIMEOUT_MS,
        "maxBytes": _CARD_MAX_BYTES,
        "bodySha256": None,
    }


class _FramedChild:
    """Deadline-bounded newline framing over a child's stdin/stdout pipes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdin is None or process.stdout is None:
            raise C10Error("JLL admission child pipes are unavailable")
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._buffer = bytearray()
        os.set_blocking(self._stdin.fileno(), False)
        os.set_blocking(self._stdout.fileno(), False)

    def send(self, frame: bytes, deadline: float) -> None:
        pending = memoryview(frame)
        with selectors.DefaultSelector() as selector:
            selector.register(self._stdin, selectors.EVENT_WRITE)
            while pending:
                ready = selector.select(min(_remaining(deadline), 0.1))
                if not ready:
                    continue
                try:
                    written = os.write(self._stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise C10Error("JLL admission child input closed") from exc
                pending = pending[written:]

    def receive(self, deadline: float) -> bytes:
        with selectors.DefaultSelector() as selector:
            selector.register(self._stdout, selectors.EVENT_READ)
            while True:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self._buffer[:newline])
                    del self._buffer[: newline + 1]
                    if len(line) > _MAX_ADMISSION_FRAME_BYTES:
                        raise C10Error("JLL admission child output is invalid")
                    return line
                if len(self._buffer) > _MAX_ADMISSION_FRAME_BYTES:
                    raise C10Error("JLL admission child output is invalid")
                ready = selector.select(min(_remaining(deadline), 0.1))
                if not ready:
                    continue
                try:
                    chunk = os.read(self._stdout.fileno(), 256 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise C10Error("JLL admission child output is invalid")
                self._buffer.extend(chunk)


class _JllAdmissionController:
    """Private bridge, constructed only by a production-owned caller.

    ``executor`` is intentionally an internal capability, not a URL/fetch
    callback exposed to source modules.  It receives only a signed C10 issued
    card and must return the sidecar's signed v3 evidence envelope.
    """

    def __init__(
        self,
        repo_root: Path,
        receipt_root: Path,
        endpoint: str,
        executor: Callable[[Mapping[str, Any], float], Mapping[str, Any]],
        profile_sha256: str,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.profile_sha256 = require_sha256(profile_sha256, "JLL admission profile")
        self.receipt_root = receipt_root.resolve()
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise C10Error(
                "JLL admission controller requires a loopback sidecar endpoint"
            )
        self.endpoint = endpoint.rstrip("/")
        self._executor = executor

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

    def _require_fresh_root(self) -> None:
        """Refuse a reused receipt root so no prior artifact can be mistaken.

        Recovery after any partial failure is a new provisioned owner-0700
        root; this controller never resumes into, or appends to, an old one.
        """
        with _open_private_root(self.receipt_root, "receipt root") as root:
            try:
                entries = os.listdir(root.fd)
            except OSError as exc:
                raise C10Error("JLL admission receipt root is unavailable") from exc
            root.recheck()
        if entries:
            raise C10Error("JLL admission receipt root must be fresh and empty")

    def _sidecar_binding(
        self, binding: Mapping[str, str], session_nonce: str
    ) -> dict[str, str]:
        """Project the receipt binding onto the sidecar's seven-digest v3 shape.

        There is no admitted manifest or durable session before admission, so
        this lane binds the fixed collection intent plus receipt binding as its
        manifest digest and a fresh per-run nonce as its session digest.
        """
        return {
            "planSha256": binding["planSha256"],
            "cohortSha256": binding["cohortSha256"],
            "armSha256": binding["armSha256"],
            "manifestSha256": sha256(
                {
                    "kind": "cre_capacity_c10_jll_v1_admission_intent",
                    "intent": _jll_intent(),
                    "receiptBinding": dict(binding),
                }
            ),
            "sessionSha256": sha256(
                {
                    "kind": "cre_capacity_c10_jll_v1_admission_session",
                    "nonce": session_nonce,
                }
            ),
            "profileSha256": self.profile_sha256,
        }

    def _issue(
        self,
        card: Mapping[str, Any],
        sidecar_binding: Mapping[str, str],
        keys: C10EphemeralKeys,
        deadline: float,
        sequence: int,
    ) -> dict[str, Any]:
        _remaining(deadline)
        # The sidecar's exact v3 card schema carries prior membership only for
        # P0/P1 enumeration; an admission lane sidecar requires it to be null.
        issued_card = {**card, "expectedMemberRoutes": None}
        now = int(time.time() * 1000)
        remaining = max(1, int(_remaining(deadline) * 1000))
        capability = {
            "protocolVersion": 3,
            "coordinatorKeyId": _key_id(keys.coordinator_public_pem),
            "nonce": secrets.token_urlsafe(32),
            "expiresAtMs": now + min(remaining, 120_000),
            "hostDeadlineAtMs": now + remaining,
            "cardSequence": sequence,
            "sourceKey": "jll",
            "binding": {**sidecar_binding, "cardSha256": sha256(issued_card)},
        }
        payload = (
            base64.urlsafe_b64encode(canonical_bytes(capability))
            .decode("ascii")
            .rstrip("=")
        )
        return {
            "endpoint": self.endpoint,
            "capability": capability,
            "authorization": f"{payload}.{_OpenSsl.sign(keys.coordinator_private_pem, payload.encode(), deadline)}",
            "card": issued_card,
            "hostTransportKey": keys.transport_key,
        }

    @staticmethod
    def _validate_card(
        card: Any, selected: list[dict[str, str]], sequence: int
    ) -> dict[str, Any]:
        """Accept only the exact source card for this position in the graph."""
        if not isinstance(card, Mapping):
            raise C10Error("JLL admission child requested an unbound card")
        if sequence == 0:
            expected = _expected_enumeration_card(card)
        elif 1 <= sequence <= JLL_MEMBER_COUNT and len(selected) == JLL_MEMBER_COUNT:
            expected = _expected_member_card(
                sequence - 1, selected[sequence - 1]["canonical_url"]
            )
        else:
            raise C10Error("JLL admission child exceeded its fixed card graph")
        if dict(card) != expected:
            raise C10Error("JLL admission card is not bound to its sealed cohort")
        return expected

    def _verify_manifest(
        self,
        store: PrivateReceiptStore,
        descriptor: Any,
        sealed: list[Mapping[str, Any]],
        selection: Mapping[str, Any],
        adapter_implementation_sha256: str,
    ) -> dict[str, Any]:
        """Bind the child's terminal manifest to controller-observed state."""
        if (
            not isinstance(descriptor, Mapping)
            or not sealed
            or dict(descriptor) != dict(sealed[-1])
            or not str(descriptor.get("name", "")).startswith("jll-admission-manifest-")
        ):
            raise C10Error("JLL admission manifest was not the final sealed artifact")
        try:
            manifest = json.loads(store.read_sealed(descriptor))
        except (UnicodeDecodeError, ValueError) as exc:
            raise C10Error("JLL admission manifest JSON is invalid") from exc
        if not isinstance(manifest, Mapping):
            raise C10Error("JLL admission manifest JSON is invalid")
        unsigned = {
            key: value for key, value in manifest.items() if key != "manifest_sha256"
        }
        members = selection["members"]
        if (
            manifest.get("kind") != JLL_RECEIPT_MANIFEST_KIND
            or manifest.get("manifest_sha256") != sha256(unsigned)
            or manifest.get("receipt_root") != str(self.receipt_root)
            or manifest.get("adapter_implementation_sha256")
            != adapter_implementation_sha256
            or manifest.get("collection_intent") != _jll_intent()
            or manifest.get("members") != members
            or manifest.get("selection_digest") != selection["digest"]
            or manifest.get("collection_intent_sha256")
            != _collection_intent_sha256(members)
            or manifest.get("artifacts") != [dict(item) for item in sealed[:-1]]
        ):
            raise C10Error(
                "JLL admission manifest does not match controller-observed state"
            )
        return dict(manifest)

    def _run(
        self,
        *,
        binding: Mapping[str, str],
        adapter_implementation_sha256: str,
        timeout_seconds: float = 120.0,
        keys: C10EphemeralKeys | None = None,
    ) -> Mapping[str, Any]:
        """Run exactly one enumeration and sixteen member cards, or fail closed.

        The caller must be a production controller that already owns a
        sidecar lifecycle.  This method never creates a provider client,
        writes database/cache/listing/scheduler state, or accepts members,
        cards, URLs, or artifact paths from its caller.
        """
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise C10Error("JLL admission controller timeout is outside its bound")
        require_sha256(adapter_implementation_sha256, "JLL adapter implementation")
        if set(binding) != _BINDING_FIELDS:
            raise C10Error("JLL admission binding is incomplete")
        for name, digest in binding.items():
            require_sha256(digest, f"JLL admission {name}")
        deadline = time.monotonic() + timeout_seconds
        self._require_fresh_root()
        collector_root = self.repo_root / "scripts/firecrawl-ops/cre_collector"
        script = collector_root / "capacity_c10/receipts/jll_admission_child.ts"
        keys = keys or self._keys(deadline)
        sidecar_binding = self._sidecar_binding(binding, secrets.token_hex(32))
        sequence = 0
        selection: dict[str, Any] | None = None
        sealed: list[Mapping[str, Any]] = []
        sealed_bytes = 0
        process: subprocess.Popen[bytes] | None = None
        try:
            with PrivateReceiptStore.create(self.receipt_root) as store:
                process = subprocess.Popen(
                    ["node", "--import", "tsx", str(script)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    # `--import tsx` resolves from cwd; the collector package
                    # owns the pinned tsx dependency, the repository root has none.
                    cwd=collector_root,
                    start_new_session=True,
                )
                child = _FramedChild(process)
                child.send(
                    _frame(
                        {
                            "protocol": _PROTOCOL,
                            "type": "init",
                            "receiptRoot": str(self.receipt_root),
                            "binding": dict(binding),
                            "adapterImplementationSha256": adapter_implementation_sha256,
                        }
                    ),
                    deadline,
                )
                while True:
                    raw = child.receive(deadline)
                    try:
                        message = json.loads(raw)
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise C10Error(
                            "JLL admission child protocol is invalid"
                        ) from exc
                    if (
                        not isinstance(message, Mapping)
                        or message.get("protocol") != _PROTOCOL
                        or (
                            message.get("type") != "result"
                            and (
                                not isinstance(message.get("id"), str)
                                or _REQUEST_ID.fullmatch(message["id"]) is None
                            )
                        )
                    ):
                        raise C10Error(
                            "JLL admission child protocol binding is invalid"
                        )
                    if message.get("type") == "result":
                        if message.get("ok") is not True:
                            detail = message.get("error")
                            raise C10Error(
                                f"JLL admission child rejected the receipt set: {detail if isinstance(detail, str) else 'unknown error'}"
                            )
                        if sequence != JLL_MEMBER_COUNT + 1 or selection is None:
                            raise C10Error(
                                "JLL admission child did not complete its bounded receipt set"
                            )
                        manifest = self._verify_manifest(
                            store,
                            message.get("manifest"),
                            sealed,
                            selection,
                            adapter_implementation_sha256,
                        )
                        return {
                            "manifest": dict(sealed[-1]),
                            "manifest_sha256": manifest["manifest_sha256"],
                            "selection_digest": selection["digest"],
                            "artifacts": [dict(item) for item in sealed[:-1]],
                        }
                    if message.get("type") == "seal":
                        stem = message.get("stem")
                        if not isinstance(stem, str) or _STEM.fullmatch(stem) is None:
                            raise C10Error(
                                "JLL admission child artifact stem is invalid"
                            )
                        if len(sealed) >= _MAX_SEALED_ARTIFACTS:
                            raise C10Error(
                                "JLL admission child exceeded its sealed artifact bound"
                            )
                        if message.get("encoding") == "json":
                            body = canonical_bytes(message.get("value"))
                        elif message.get("encoding") == "base64" and isinstance(
                            message.get("bodyBase64"), str
                        ):
                            try:
                                body = base64.b64decode(
                                    message["bodyBase64"], validate=True
                                )
                            except ValueError as exc:
                                raise C10Error(
                                    "JLL admission child artifact encoding is invalid"
                                ) from exc
                        else:
                            raise C10Error(
                                "JLL admission child artifact encoding is invalid"
                            )
                        sealed_bytes += len(body)
                        if sealed_bytes > _MAX_SEALED_BYTES:
                            raise C10Error(
                                "JLL admission child exceeded its sealed byte bound"
                            )
                        artifact = store.seal_bytes(stem, body)
                        sealed.append(dict(artifact))
                        reply = {
                            "protocol": _PROTOCOL,
                            "type": "reply",
                            "id": message.get("id"),
                            "ok": True,
                            "artifact": artifact,
                        }
                    elif message.get("type") == "execute":
                        card = self._validate_card(
                            message.get("card"),
                            [] if selection is None else selection["members"],
                            sequence,
                        )
                        issued = self._issue(
                            card, sidecar_binding, keys, deadline, sequence
                        )
                        evidence = self._executor(issued, deadline)
                        # Reuse the C10 v3 signature/response acceptance gate.
                        _C10HostTransport._verify_evidence(
                            object.__new__(_C10HostTransport),
                            evidence,
                            issued,
                            keys,
                            deadline,
                            admission_enumeration=sequence == 0,
                        )
                        raw_body = evidence["bodyBase64"]
                        if sequence == 0:
                            selection = select_jll_admission_members(
                                _parse_enumeration_body(
                                    base64.b64decode(raw_body, validate=True)
                                )
                            )
                        reply = {
                            "protocol": _PROTOCOL,
                            "type": "reply",
                            "id": message.get("id"),
                            "ok": True,
                            "response": {
                                "status": evidence["status"],
                                "finalUrl": evidence["finalUrl"],
                                "redirectCount": evidence["redirectCount"],
                                "elapsedMs": evidence["elapsedMs"],
                                "challengeDetected": evidence["challengeDetected"],
                                "bodyBase64": raw_body,
                                "contentType": evidence["contentType"],
                                "providerAttempts": 1,
                                "cacheMode": "no-store",
                                "trustedBrowserEvidence": evidence,
                            },
                        }
                        sequence += 1
                    else:
                        raise C10Error("JLL admission child frame is unsupported")
                    child.send(_frame(reply), deadline)
        except (OSError, subprocess.SubprocessError) as exc:
            raise C10Error("JLL admission controller child failed") from exc
        finally:
            if process is not None and process.poll() is None:
                _C10HostTransport._kill_child_group(process)
