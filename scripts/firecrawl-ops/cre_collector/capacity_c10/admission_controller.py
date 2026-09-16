"""Controller-owned, framed bridge for the JLL admission receipt set.

The public offline admission helpers deliberately cannot collect evidence.  This
module is their only executable counterpart: it owns the private receipt root,
the deadline, and C10 v3 capability issuance.  The TypeScript child receives no
provider URL authority, browser endpoint, key, or filesystem descriptor.  It
can ask this controller to execute one already-source-bound card or seal one
private artifact, and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import C10Error, canonical_bytes, require_sha256, sha256
from .host_crypto import _OpenSsl
from .host_orchestration import (
    _MAX_CHILD_FRAME_BYTES,
    _MAX_CHILD_STDOUT_BYTES,
    _C10HostTransport,
    _key_id,
    _remaining,
)
from .host_sidecar import C10EphemeralKeys
from .host_store import PrivateReceiptStore
from .jll_admission import JLL_ENUMERATION_BODY_SHA256

_PROTOCOL = "c10-jll-admission-rpc-v1"
_JLL_HOST = "property.jll.com"
_JLL_PREFIX = "https://property.jll.com/listings/"
_JLL_GRAPHQL = "https://property.jll.com/api/graphql"
_MEMBER_COUNT = 16


def _frame(value: Mapping[str, Any]) -> bytes:
    raw = canonical_bytes(value) + b"\n"
    if len(raw) > _MAX_CHILD_FRAME_BYTES:
        raise C10Error("JLL admission controller frame exceeds its bound")
    return raw


def _members(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != _MEMBER_COUNT:
        raise C10Error("JLL admission controller requires exactly sixteen members")
    result: list[dict[str, str]] = []
    routes: set[str] = set()
    ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise C10Error("JLL admission controller member is invalid")
        key, provider_id, route = (
            item.get("key"),
            item.get("providerId"),
            item.get("canonicalUrl"),
        )
        if (
            key != f"jll-{index + 1}"
            or not isinstance(provider_id, str)
            or not provider_id.isdigit()
            or not isinstance(route, str)
            or not route.startswith(_JLL_PREFIX)
            or "?" in route
            or "#" in route
            or route in routes
            or provider_id in ids
        ):
            raise C10Error("JLL admission controller member is invalid")
        routes.add(route)
        ids.add(provider_id)
        result.append({"key": key, "providerId": provider_id, "canonicalUrl": route})
    return result


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
    ) -> None:
        self.repo_root = repo_root.resolve()
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

    def _issue(
        self,
        card: Mapping[str, Any],
        binding: Mapping[str, str],
        keys: C10EphemeralKeys,
        deadline: float,
        sequence: int,
    ) -> dict[str, Any]:
        _remaining(deadline)
        source_key = card.get("sourceKey")
        if source_key != "jll":
            raise C10Error("JLL admission controller source binding is invalid")
        now = int(time.time() * 1000)
        remaining = max(1, int(_remaining(deadline) * 1000))
        capability = {
            "protocolVersion": 3,
            "coordinatorKeyId": _key_id(keys.coordinator_public_pem),
            "nonce": secrets.token_urlsafe(32),
            "expiresAtMs": now + min(remaining, 120_000),
            "hostDeadlineAtMs": now + remaining,
            "cardSequence": sequence,
            "sourceKey": source_key,
            "binding": {**binding, "cardSha256": sha256(card)},
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
            "card": dict(card),
            "hostTransportKey": keys.transport_key,
        }

    @staticmethod
    def _validate_card(
        card: Any, members: list[dict[str, str]], sequence: int
    ) -> Mapping[str, Any]:
        if (
            not isinstance(card, Mapping)
            or card.get("sourceKey") != "jll"
            or card.get("cacheMode") != "no-store"
        ):
            raise C10Error("JLL admission child requested an unbound card")
        stage = card.get("stage")
        if sequence == 0:
            if (
                stage != "enumeration"
                or card.get("id") != "jll-enumeration-0"
                or card.get("method") != "POST"
                or card.get("url") != _JLL_GRAPHQL
                or card.get("allowedHost") != _JLL_HOST
                or card.get("contentType") != "application/json"
                or card.get("browserBootstrapUrl") != "https://property.jll.com/"
                or not isinstance(card.get("body"), str)
                or hashlib.sha256(card["body"].encode("utf-8")).hexdigest()
                != JLL_ENUMERATION_BODY_SHA256
            ):
                raise C10Error("JLL admission enumeration card is invalid")
        else:
            member = members[sequence - 1]
            if (
                stage != "member"
                or card.get("id") != f"jll-member-{sequence - 1}"
                or card.get("method") != "GET"
                or card.get("url") != member["canonicalUrl"]
                or card.get("allowedHost") != _JLL_HOST
                or card.get("contentType") is not None
                or card.get("body") is not None
                or card.get("browserBootstrapUrl") != "https://property.jll.com/"
            ):
                raise C10Error(
                    "JLL admission member card is not bound to its sealed cohort"
                )
        return card

    def _run(
        self,
        *,
        members: list[dict[str, str]],
        binding: Mapping[str, str],
        adapter_implementation_sha256: str,
        timeout_seconds: float = 120.0,
        keys: C10EphemeralKeys | None = None,
    ) -> Mapping[str, Any]:
        """Run exactly one enumeration and sixteen member cards, or fail closed.

        The caller must be a production controller that already owns a
        sidecar lifecycle.  This method never creates a provider client,
        writes database/cache/listing/scheduler state, or accepts caller paths.
        """
        fixed_members = _members(members)
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise C10Error("JLL admission controller timeout is outside its bound")
        require_sha256(adapter_implementation_sha256, "JLL adapter implementation")
        for name, digest in binding.items():
            if name not in {
                "planSha256",
                "cohortSha256",
                "policySha256",
                "sourceSha256",
                "armSha256",
                "implementationSha256",
            }:
                raise C10Error("JLL admission binding is invalid")
            require_sha256(digest, f"JLL admission {name}")
        if len(binding) != 6:
            raise C10Error("JLL admission binding is incomplete")
        deadline = time.monotonic() + timeout_seconds
        script = (
            self.repo_root
            / "scripts/firecrawl-ops/cre_collector/capacity_c10/receipts/jll_admission_child.ts"
        )
        keys = keys or self._keys(deadline)
        sequence = 0
        try:
            with PrivateReceiptStore.create(self.receipt_root) as store:
                process = subprocess.Popen(
                    ["node", "--import", "tsx", str(script)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.repo_root,
                    text=False,
                    start_new_session=True,
                )
                if process.stdin is None or process.stdout is None:
                    raise C10Error("JLL admission child pipes are unavailable")
                process.stdin.write(
                    _frame(
                        {
                            "protocol": _PROTOCOL,
                            "type": "init",
                            "receiptRoot": str(self.receipt_root),
                            "binding": dict(binding),
                            "members": fixed_members,
                            "adapterImplementationSha256": adapter_implementation_sha256,
                        }
                    )
                )
                process.stdin.flush()
                while True:
                    if _remaining(deadline) <= 0:
                        raise C10Error("JLL admission child exceeded its deadline")
                    raw = process.stdout.readline(_MAX_CHILD_STDOUT_BYTES + 1)
                    if not raw or len(raw) > _MAX_CHILD_STDOUT_BYTES:
                        raise C10Error("JLL admission child output is invalid")
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise C10Error(
                            "JLL admission child protocol is invalid"
                        ) from exc
                    if (
                        not isinstance(message, Mapping)
                        or message.get("protocol") != _PROTOCOL
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
                        if sequence != _MEMBER_COUNT + 1:
                            raise C10Error(
                                "JLL admission child did not complete its bounded receipt set"
                            )
                        manifest = message.get("manifest")
                        if not isinstance(manifest, Mapping):
                            raise C10Error("JLL admission child manifest is invalid")
                        store.read_sealed(manifest)
                        return {
                            "manifest": dict(manifest),
                            "receipt_set_sha256": message.get("receiptSetSha256"),
                            "artifacts": message.get("artifacts"),
                        }
                    if message.get("type") == "seal":
                        stem = message.get("stem")
                        if not isinstance(stem, str):
                            raise C10Error(
                                "JLL admission child artifact stem is invalid"
                            )
                        if message.get("encoding") == "json":
                            artifact = store.seal_json(stem, message.get("value"))
                        elif message.get("encoding") == "base64" and isinstance(
                            message.get("bodyBase64"), str
                        ):
                            artifact = store.seal_bytes(
                                stem,
                                base64.b64decode(message["bodyBase64"], validate=True),
                            )
                        else:
                            raise C10Error(
                                "JLL admission child artifact encoding is invalid"
                            )
                        reply = {
                            "protocol": _PROTOCOL,
                            "type": "reply",
                            "id": message.get("id"),
                            "ok": True,
                            "artifact": artifact,
                        }
                    elif message.get("type") == "execute":
                        card = self._validate_card(
                            message.get("card"), fixed_members, sequence
                        )
                        issued = self._issue(card, binding, keys, deadline, sequence)
                        evidence = self._executor(issued, deadline)
                        # Reuse the C10 v3 signature/response acceptance gate.
                        _C10HostTransport._verify_evidence(
                            object.__new__(_C10HostTransport),
                            evidence,
                            issued,
                            keys,
                            deadline,
                        )
                        if sequence == 0:
                            try:
                                payload = json.loads(
                                    base64.b64decode(
                                        evidence["bodyBase64"], validate=True
                                    )
                                )
                                items = payload["data"]["properties"]["items"]
                                observed = {
                                    f"https://{_JLL_HOST}{str(item['pageUrl']).split('?', 1)[0].split('#', 1)[0]}".rstrip(
                                        "/"
                                    )
                                    for item in items
                                    if isinstance(item, Mapping)
                                    and isinstance(item.get("pageUrl"), str)
                                }
                            except (
                                KeyError,
                                TypeError,
                                ValueError,
                                json.JSONDecodeError,
                            ) as exc:
                                raise C10Error(
                                    "JLL admission enumeration is not usable JSON"
                                ) from exc
                            if any(
                                member["canonicalUrl"].rstrip("/") not in observed
                                for member in fixed_members
                            ):
                                raise C10Error(
                                    "JLL admission enumeration does not bind its sealed cohort"
                                )
                        raw_body = evidence["bodyBase64"]
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
                    process.stdin.write(_frame(reply))
                    process.stdin.flush()
        except (OSError, subprocess.SubprocessError) as exc:
            raise C10Error("JLL admission controller child failed") from exc
        finally:
            try:
                if "process" in locals() and process.poll() is None:
                    os.killpg(process.pid, 9)
            except OSError:
                pass
