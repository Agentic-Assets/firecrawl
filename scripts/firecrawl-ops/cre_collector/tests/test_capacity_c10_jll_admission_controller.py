"""Hermetic controller-to-TypeScript JLL admission bridge contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Self

import pytest
from capacity_c10 import contracts, production
from capacity_c10.admission_controller import _JllAdmissionController
from capacity_c10.host_crypto import _OpenSsl
from capacity_c10.host_sidecar import C10EphemeralKeys


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def _seal(self, stem: str, body: bytes) -> dict[str, Any]:
        digest = hashlib.sha256(body).hexdigest()
        artifact = {
            "name": f"{stem}-{digest}.sealed",
            "sha256": digest,
            "bytes": len(body),
        }
        self.values[digest] = body
        return artifact

    def seal_json(self, stem: str, value: Any) -> dict[str, Any]:
        return self._seal(stem, contracts.canonical_bytes(value))

    def seal_bytes(self, stem: str, value: bytes) -> dict[str, Any]:
        return self._seal(stem, value)

    def read_sealed(self, artifact: dict[str, Any]) -> bytes:
        body = self.values[artifact["sha256"]]
        assert len(body) == artifact["bytes"]
        return body


def _members() -> list[dict[str, str]]:
    return [
        {
            "key": f"jll-{index + 1}",
            "providerId": str(index + 1),
            "canonicalUrl": f"https://property.jll.com/listings/member-{index + 1}",
        }
        for index in range(16)
    ]


def _keys() -> C10EphemeralKeys:
    deadline = time.monotonic() + 30
    coordinator_private, coordinator_public = _OpenSsl.pair(deadline)
    sidecar_private, sidecar_public = _OpenSsl.pair(deadline)
    return C10EphemeralKeys(
        coordinator_private,
        coordinator_public,
        sidecar_private,
        sidecar_public,
        "transport",
    )


def _evidence(
    issued: dict[str, Any], keys: C10EphemeralKeys, *, malformed: bool = False
) -> dict[str, Any]:
    card = issued["card"]
    index = (
        int(str(card["url"]).split("member-")[-1]) if card["stage"] == "member" else 0
    )
    if card["stage"] == "enumeration":
        payload: object = (
            {"errors": [{"message": "no"}]}
            if malformed
            else {
                "data": {
                    "properties": {
                        "count": 16,
                        "items": [
                            {"id": str(i + 1), "pageUrl": f"/listings/member-{i + 1}"}
                            for i in range(16)
                        ],
                    }
                }
            }
        )
        body, content_type = json.dumps(payload).encode(), "application/json"
    else:
        property_id = index
        body = (
            '<script id="__NEXT_DATA__">'
            + json.dumps(
                {
                    "props": {
                        "pageProps": {
                            "property": {
                                "id": str(property_id),
                                "pageUrl": card["url"],
                                "images": [],
                            }
                        }
                    }
                }
            )
            + "</script>"
        ).encode()
        content_type = "text/html"
    unsigned = {
        "protocolVersion": 3,
        "binding": issued["capability"]["binding"],
        "status": 200,
        "finalUrl": card["url"],
        "redirectCount": 0,
        "elapsedMs": 1,
        "challengeDetected": False,
        "contentType": content_type,
        "bodyBase64": base64.b64encode(body).decode(),
        "jobId": "fixture",
        "pageLease": {"leaseId": "fixture", "slot": 0},
        "leaseStartMonotonicNs": "1",
        "leaseEndMonotonicNs": "2",
        "observedActivePages": 1,
        "configuredCapacity": 1,
        "queueMs": 0,
        "proxy": {"mode": "direct", "proxyId": None, "country": None},
        "engineAttempt": {
            "engine": "fixture",
            "ordinal": 1,
            "fallbackDisabled": True,
            "fallbackUsed": False,
        },
        "context": {"ephemeral": True, "storageState": "none", "cache": "disabled"},
        "cacheRead": False,
        "cacheWrite": False,
    }
    return {
        **unsigned,
        "evidenceSignature": _OpenSsl.sign(
            keys.sidecar_private_pem,
            contracts.canonical_bytes(unsigned),
            time.monotonic() + 30,
        ),
    }


def _binding() -> dict[str, str]:
    return {
        name: letter * 64
        for name, letter in zip(
            (
                "planSha256",
                "cohortSha256",
                "policySha256",
                "sourceSha256",
                "armSha256",
                "implementationSha256",
            ),
            "abcdef",
            strict=True,
        )
    }


def test_controller_runs_exact_jll_receipt_set_and_seals_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    controller = _JllAdmissionController(
        Path(__file__).parents[4],
        tmp_path / "receipts",
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _evidence(dict(issued), keys),
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    result = controller._run(
        members=_members(), binding=_binding(), adapter_implementation_sha256="d" * 64
    )
    assert result["manifest"]["name"].startswith("jll-admission-manifest-")
    assert len(store.values) >= 17


def test_controller_rejects_malformed_enumeration_before_terminal_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    controller = _JllAdmissionController(
        Path(__file__).parents[4],
        tmp_path / "receipts",
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _evidence(dict(issued), keys, malformed=True),
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="enumeration evidence"):
        controller._run(
            members=_members(),
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
        )
    assert not store.values


def test_production_gate_rejects_imported_controller_without_active_authority(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        contracts.C10Error, match="active production-controller authority"
    ):
        production._execute_authorized_jll_admission_action(
            repo_root=tmp_path,
            receipt_root=tmp_path / "receipts",
            endpoint="http://127.0.0.1:38111",
            members=_members(),
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
            deadline=time.monotonic() + 10,
            authority=object(),
            keys=_keys(),
        )


def test_controller_rejects_tampered_sidecar_signature_before_sealing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )

    def executor(issued: dict[str, Any], _deadline: float) -> dict[str, Any]:
        value = _evidence(issued, keys)
        value["evidenceSignature"] = "tampered"
        return value

    controller = _JllAdmissionController(
        Path(__file__).parents[4],
        tmp_path / "receipts",
        "http://127.0.0.1:38111",
        executor,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="signature"):
        controller._run(
            members=_members(),
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
        )
    assert not store.values
