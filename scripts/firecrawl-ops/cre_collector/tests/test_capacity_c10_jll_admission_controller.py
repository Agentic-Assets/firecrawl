"""Hermetic controller-to-TypeScript JLL admission bridge contracts."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Self

import pytest

from capacity_c10 import contracts, jll_admission, production
from capacity_c10.admission_controller import (
    JLL_MEMBER_COUNT,
    _expected_enumeration_card,
    _expected_member_card,
    _JllAdmissionController,
)
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


def _fresh_root(tmp_path: Path, name: str = "receipts") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


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
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _evidence(dict(issued), keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    result = controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)
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
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _evidence(dict(issued), keys, malformed=True),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="enumeration evidence"):
        controller._run(
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
            receipt_root=_fresh_root(tmp_path),
            endpoint="http://127.0.0.1:38111",
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
            deadline=time.monotonic() + 10,
            authority=object(),
            keys=_keys(),
            profile_sha256="e" * 64,
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
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        executor,
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="signature"):
        controller._run(
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
        )
    assert not store.values


# --------------------------------------------------------------------------
# New coverage below this line.
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parents[4]


def _enumeration_body(items: list[dict[str, str]], *, pad_bytes: int = 0) -> bytes:
    payload: dict[str, Any] = {
        "data": {"properties": {"count": len(items), "items": items}}
    }
    if pad_bytes:
        payload["padding"] = "x" * pad_bytes
    return json.dumps(payload).encode()


def _member_body(provider_id: str, page_url: str, *, pad_bytes: int = 0) -> bytes:
    html = (
        "<html><body>"
        + (f"<!--{'x' * pad_bytes}-->" if pad_bytes else "")
        + '<script id="__NEXT_DATA__">'
        + json.dumps(
            {
                "props": {
                    "pageProps": {
                        "property": {
                            "id": provider_id,
                            "pageUrl": page_url,
                            "images": [],
                        }
                    }
                }
            }
        )
        + "</script></body></html>"
    )
    return html.encode()


def _flex_evidence(
    issued: dict[str, Any],
    keys: C10EphemeralKeys,
    *,
    enumeration_items: list[dict[str, str]] | None = None,
    enumeration_pad_bytes: int = 0,
    member_pad_bytes: int = 0,
    tamper_signature: bool = False,
    wrong_final_url: bool = False,
) -> dict[str, Any]:
    card = issued["card"]
    if card["stage"] == "enumeration":
        items = (
            enumeration_items
            if enumeration_items is not None
            else [
                {"id": str(i + 1), "pageUrl": f"/listings/member-{i + 1}"}
                for i in range(16)
            ]
        )
        body = _enumeration_body(items, pad_bytes=enumeration_pad_bytes)
        content_type = "application/json"
    else:
        provider_id = str(card["url"]).rsplit("member-", 1)[-1]
        body = _member_body(provider_id, card["url"], pad_bytes=member_pad_bytes)
        content_type = "text/html"
    final_url = (
        "https://property.jll.com/listings/does-not-exist"
        if wrong_final_url
        else card["url"]
    )
    unsigned = {
        "protocolVersion": 3,
        "binding": issued["capability"]["binding"],
        "status": 200,
        "finalUrl": final_url,
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
    signature = _OpenSsl.sign(
        keys.sidecar_private_pem,
        contracts.canonical_bytes(unsigned),
        time.monotonic() + 30,
    )
    if tamper_signature:
        signature = "tampered-" + signature
    return {**unsigned, "evidenceSignature": signature}


def _has_manifest(store_values: dict[str, bytes]) -> bool:
    for body in store_values.values():
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("kind") == (
            jll_admission.JLL_RECEIPT_MANIFEST_KIND
        ):
            return True
    return False


# --- 1. Issued capability shape + selection order -------------------------


def test_issued_capability_shape_and_selection_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    # 18 shuffled candidates whose numeric order differs from lexicographic
    # string order (member-1, member-10, member-11 ... sort before member-2).
    slugs = list(range(1, 19))
    rng_order = [17, 3, 1, 12, 9, 2, 16, 18, 4, 11, 6, 14, 8, 13, 5, 10, 15, 7]
    assert sorted(rng_order) == slugs
    items = [
        {"id": str(slug), "pageUrl": f"/listings/member-{slug}"} for slug in rng_order
    ]
    expected_routes = sorted(
        f"https://property.jll.com/listings/member-{slug}" for slug in slugs
    )[:16]

    issued_calls: list[dict[str, Any]] = []

    def executor(issued: dict[str, Any], _deadline: float) -> dict[str, Any]:
        issued_calls.append(dict(issued))
        return _flex_evidence(issued, keys, enumeration_items=items)

    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        executor,
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)

    assert len(issued_calls) == 17
    for issued in issued_calls:
        binding = issued["capability"]["binding"]
        assert set(binding) == {
            "planSha256",
            "cohortSha256",
            "cardSha256",
            "manifestSha256",
            "sessionSha256",
            "armSha256",
            "profileSha256",
        }
        assert binding["profileSha256"] == controller.profile_sha256
        assert binding["cardSha256"] == contracts.sha256(issued["card"])
        assert issued["card"]["expectedMemberRoutes"] is None
    session_digests = {
        issued["capability"]["binding"]["sessionSha256"] for issued in issued_calls
    }
    assert len(session_digests) == 1

    member_urls = [issued["card"]["url"] for issued in issued_calls[1:]]
    assert member_urls == expected_routes


def test_session_digest_differs_across_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys = _keys()
    captured_sessions: list[str] = []

    def make_controller(receipt_root: Path) -> _JllAdmissionController:
        store = _MemoryStore()
        monkeypatch.setattr(
            "capacity_c10.admission_controller.PrivateReceiptStore.create",
            lambda _root, _store=store: _store,
        )

        def executor(issued: dict[str, Any], _deadline: float) -> dict[str, Any]:
            captured_sessions.append(issued["capability"]["binding"]["sessionSha256"])
            return _flex_evidence(issued, keys)

        controller = _JllAdmissionController(
            REPO_ROOT,
            receipt_root,
            "http://127.0.0.1:38111",
            executor,
            "e" * 64,
        )
        monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
        return controller

    make_controller(_fresh_root(tmp_path, "receipts-a"))._run(
        binding=_binding(), adapter_implementation_sha256="d" * 64
    )
    make_controller(_fresh_root(tmp_path, "receipts-b"))._run(
        binding=_binding(), adapter_implementation_sha256="d" * 64
    )
    assert len(captured_sessions) == 34
    first_run, second_run = captured_sessions[:17], captured_sessions[17:]
    assert len(set(first_run)) == 1
    assert len(set(second_run)) == 1
    assert first_run[0] != second_run[0]


# --- 2. Exact card pinning (static _validate_card) -------------------------


def test_validate_card_static_pins_exact_card_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Capture one real enumeration card body and the real selected members
    # from an actual controller-driven run so the static-method tests below
    # exercise the exact production card grammar rather than a hand-rolled
    # guess at the enumeration body text.
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    captured_cards: list[dict[str, Any]] = []

    def executor(issued: dict[str, Any], _deadline: float) -> dict[str, Any]:
        captured_cards.append(dict(issued["card"]))
        return _flex_evidence(issued, keys)

    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        executor,
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)
    assert len(captured_cards) == 17
    # The issued card carries an extra controller-added "expectedMemberRoutes"
    # key (always None in the admission lane); the raw card the child sent,
    # which _validate_card actually checks, does not have that key.
    enumeration_card = {
        k: v for k, v in captured_cards[0].items() if k != "expectedMemberRoutes"
    }
    member_cards = [
        {k: v for k, v in card.items() if k != "expectedMemberRoutes"}
        for card in captured_cards[1:]
    ]
    selected = [{"canonical_url": card["url"]} for card in member_cards]

    # Correct cards pass and yield the expected shape.
    good_enum = _JllAdmissionController._validate_card(enumeration_card, [], 0)
    assert good_enum == _expected_enumeration_card(enumeration_card)
    good_member = _JllAdmissionController._validate_card(member_cards[0], selected, 1)
    assert good_member == _expected_member_card(0, selected[0]["canonical_url"])

    # Enumeration variants: extra header, different accept header, timeout,
    # maxBytes, extra key, wrong id, sequence beyond the graph.
    def enum_variant(**overrides: Any) -> dict[str, Any]:
        card = dict(enumeration_card)
        if "headers" in overrides:
            card["headers"] = {**card["headers"], **overrides.pop("headers")}
        card.update(overrides)
        return card

    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            enum_variant(headers={"x-extra": "1"}), [], 0
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            enum_variant(headers={"accept": "text/plain"}), [], 0
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(enum_variant(timeoutMs=30001), [], 0)
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            enum_variant(maxBytes=2 * 1024 * 1024 + 1), [], 0
        )
    extra_key_card = dict(enumeration_card)
    extra_key_card["extraField"] = "nope"
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(extra_key_card, [], 0)
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            enum_variant(id="jll-enumeration-wrong"), [], 0
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            enumeration_card, [], JLL_MEMBER_COUNT + 1
        )

    # Enumeration body whose hash does not match the fixed constant fails.
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            enum_variant(body="not the real body", bodySha256=None), [], 0
        )

    # Member variants.
    def member_variant(**overrides: Any) -> dict[str, Any]:
        card = dict(member_cards[0])
        if "headers" in overrides:
            card["headers"] = {**card["headers"], **overrides.pop("headers")}
        card.update(overrides)
        return card

    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_variant(headers={"x-extra": "1"}), selected, 1
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_variant(headers={"accept": "text/plain"}), selected, 1
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_variant(timeoutMs=30001), selected, 1
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_variant(maxBytes=2 * 1024 * 1024 + 1), selected, 1
        )
    extra_key_member = dict(member_cards[0])
    extra_key_member["extraField"] = "nope"
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(extra_key_member, selected, 1)
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_variant(url=selected[1]["canonical_url"]), selected, 1
        )
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_variant(id="jll-member-wrong"), selected, 1
        )
    # Sequence beyond the graph (17 with a full 16-member selection).
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(
            member_cards[0], selected, JLL_MEMBER_COUNT + 1
        )
    # Sequence 1 before any selection exists (empty selected list).
    with pytest.raises(contracts.C10Error):
        _JllAdmissionController._validate_card(member_cards[0], [], 1)


# --- 3. Fresh receipt root ---------------------------------------------


def test_fresh_root_rejects_nonempty_missing_and_lenient_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def refuse_popen(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("subprocess must not start before the fresh-root check")

    monkeypatch.setattr(
        "capacity_c10.admission_controller.subprocess.Popen", refuse_popen
    )

    def refuse_executor(_issued: Any, _deadline: float) -> Any:
        raise AssertionError("executor must not run before the fresh-root check")

    # Nonempty root.
    nonempty = _fresh_root(tmp_path, "nonempty")
    (nonempty / "stray-file").write_text("x")
    controller = _JllAdmissionController(
        REPO_ROOT, nonempty, "http://127.0.0.1:38111", refuse_executor, "e" * 64
    )
    with pytest.raises(contracts.C10Error, match="fresh and empty"):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)

    # Missing root.
    missing = tmp_path / "does-not-exist"
    controller = _JllAdmissionController(
        REPO_ROOT, missing, "http://127.0.0.1:38111", refuse_executor, "e" * 64
    )
    with pytest.raises(contracts.C10Error):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)

    # Lenient (0755) permissions.
    lenient = tmp_path / "lenient"
    lenient.mkdir(mode=0o755)
    lenient.chmod(0o755)
    controller = _JllAdmissionController(
        REPO_ROOT, lenient, "http://127.0.0.1:38111", refuse_executor, "e" * 64
    )
    with pytest.raises(contracts.C10Error):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)


# --- 4. No `members` parameter anywhere in the run/execute surface --------


def test_run_and_execute_have_no_members_parameter() -> None:
    targets = (
        _JllAdmissionController._run,
        production.execute_jll_admission_collection,
        production._execute_authorized_jll_admission_action,
    )
    for target in targets:
        assert "members" not in inspect.signature(target).parameters
        with pytest.raises(TypeError):
            target(members=[])  # type: ignore[call-arg]


# --- 5. Seal bounds -------------------------------------------------------


def test_seal_bounds_reject_excess_artifact_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    monkeypatch.setattr("capacity_c10.admission_controller._MAX_SEALED_ARTIFACTS", 5)
    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(issued, keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="sealed artifact bound"):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)


def test_seal_bounds_reject_excess_byte_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    monkeypatch.setattr("capacity_c10.admission_controller._MAX_SEALED_BYTES", 100)
    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(issued, keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="sealed byte bound"):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)


# --- 6. Manifest binding to controller-observed state ----------------------


class _TamperingStore(_MemoryStore):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def read_sealed(self, artifact: dict[str, Any]) -> bytes:
        raw = super().read_sealed(artifact)
        if not artifact["name"].startswith("jll-admission-manifest-"):
            return raw
        manifest = json.loads(raw)
        if self.mode == "member":
            member = dict(manifest["members"][0])
            member["provider_id"] = "999999999"
            manifest["members"][0] = member
        elif self.mode == "artifacts":
            manifest["artifacts"] = manifest["artifacts"][:-1]
        else:
            raise AssertionError(f"unknown tamper mode {self.mode}")
        unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        manifest["manifest_sha256"] = contracts.sha256(unsigned)
        return contracts.canonical_bytes(manifest)


@pytest.mark.parametrize("mode", ["member", "artifacts"])
def test_manifest_binding_rejects_state_it_did_not_observe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    keys = _keys()
    store = _TamperingStore(mode)
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(issued, keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="controller-observed state"):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)


# --- 7. Large member bodies and large enumeration body ---------------------


def test_large_member_bodies_and_large_enumeration_succeed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(
            issued,
            keys,
            enumeration_pad_bytes=70_000,
            member_pad_bytes=1_500_000,
        ),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    result = controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)
    assert result["manifest"]["name"].startswith("jll-admission-manifest-")
    assert any(len(body) > 65536 for body in store.values.values())
    assert any(len(body) > 1_500_000 for body in store.values.values())


# --- 8. Deadline enforcement and oversized unframed line -------------------


def test_deadline_kills_hung_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def hung_popen(_argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        spawned.append(process)
        return process

    monkeypatch.setattr(
        "capacity_c10.admission_controller.subprocess.Popen", hung_popen
    )
    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(issued, keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    started = time.monotonic()
    with pytest.raises(contracts.C10Error):
        controller._run(
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
            timeout_seconds=1.5,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert len(spawned) == 1
    process = spawned[0]
    for _ in range(50):
        if process.poll() is not None:
            break
        time.sleep(0.1)
    assert process.poll() is not None


def test_oversized_unframed_line_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )
    monkeypatch.setattr(
        "capacity_c10.admission_controller._MAX_ADMISSION_FRAME_BYTES", 1024
    )
    real_popen = subprocess.Popen

    def noisy_popen(_argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        script = (
            "import sys, time\n"
            "sys.stdout.write('x' * 200000)\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n"
        )
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(
        "capacity_c10.admission_controller.subprocess.Popen", noisy_popen
    )
    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(issued, keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="output is invalid"):
        controller._run(
            binding=_binding(),
            adapter_implementation_sha256="d" * 64,
            timeout_seconds=10,
        )


# --- 9. Tampered member-stage evidence --------------------------------


def test_member_evidence_final_url_mismatch_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )

    def executor(issued: dict[str, Any], _deadline: float) -> dict[str, Any]:
        wrong = issued["card"]["id"] == "jll-member-0"
        return _flex_evidence(issued, keys, wrong_final_url=wrong)

    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        executor,
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)
    assert not _has_manifest(store.values)


def test_member_evidence_tampered_signature_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys, store = _keys(), _MemoryStore()
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: store,
    )

    def executor(issued: dict[str, Any], _deadline: float) -> dict[str, Any]:
        tamper = issued["card"]["id"] == "jll-member-0"
        return _flex_evidence(issued, keys, tamper_signature=tamper)

    controller = _JllAdmissionController(
        REPO_ROOT,
        _fresh_root(tmp_path),
        "http://127.0.0.1:38111",
        executor,
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    with pytest.raises(contracts.C10Error, match="signature"):
        controller._run(binding=_binding(), adapter_implementation_sha256="d" * 64)
    assert not _has_manifest(store.values)


# --- 10. Cross-language end-to-end: real TS child -> independent Python ---
# validator (jll_admission.build_jll_bundle / render_jll_authority).


class _DiskStore:
    """Disk-backed mirror of PrivateReceiptStore's public surface.

    ``PrivateReceiptStore.create`` refuses to run outside Linux; this test
    runs on macOS/CI hosts that are not Linux, so it substitutes a minimal
    disk-backed store with the same descriptor-relative seal/read contract
    the controller relies on, to prove that a real TS-produced manifest on
    disk satisfies the independent Python validator end to end.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        os.close(self._fd)

    def seal_json(self, stem: str, value: Any) -> dict[str, Any]:
        return self.seal_bytes(stem, contracts.canonical_bytes(value))

    def seal_bytes(self, stem: str, body: bytes) -> dict[str, Any]:
        digest = hashlib.sha256(body).hexdigest()
        final = f"{stem}-{digest}.sealed"
        temporary = f".{final}.tmp-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self._fd)
        try:
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
        finally:
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass
        return {"name": final, "sha256": digest, "bytes": len(body)}

    def read_sealed(self, artifact: dict[str, Any]) -> bytes:
        descriptor = os.open(
            artifact["name"], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd
        )
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()


def test_cross_language_manifest_satisfies_independent_python_validator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys = _keys()
    receipt_root = _fresh_root(tmp_path, "receipts")
    disk_store = _DiskStore(receipt_root)
    monkeypatch.setattr(
        "capacity_c10.admission_controller.PrivateReceiptStore.create",
        lambda _root: disk_store,
    )
    adapter_sha = "d" * 64
    controller = _JllAdmissionController(
        REPO_ROOT,
        receipt_root,
        "http://127.0.0.1:38111",
        lambda issued, _deadline: _flex_evidence(issued, keys),
        "e" * 64,
    )
    monkeypatch.setattr(controller, "_keys", lambda _deadline: keys)
    result = controller._run(
        binding=_binding(), adapter_implementation_sha256=adapter_sha
    )
    manifest_name = result["manifest"]["name"]
    assert manifest_name.startswith("jll-admission-manifest-")

    expected_routes = sorted(
        f"https://property.jll.com/listings/member-{i + 1}" for i in range(16)
    )

    monkeypatch.setattr(
        "capacity_c10.jll_admission.repository_implementation_sha256",
        lambda _key: adapter_sha,
    )
    admission_root = tmp_path / "admission"
    admission_root.mkdir(mode=0o700)
    admission_root.chmod(0o700)

    bundle_result = jll_admission.build_jll_bundle(
        receipt_root=receipt_root,
        receipt_manifest=receipt_root / manifest_name,
        admission_root=admission_root,
    )
    bundle_path = Path(bundle_result["path"])
    assert bundle_result["cohort_sha256"]

    authority = jll_admission.render_jll_authority(bundle_path)
    assert authority["kind"] == "cre_capacity_c10_jll_v1_authority"
    assert authority["approved_cohort_sha256"] == bundle_result["cohort_sha256"]
    assert result["selection_digest"]

    # Independently re-verify the manifest members equal the sorted first
    # sixteen canonical routes, using the manifest bytes straight off disk.
    # The store's descriptor was closed when the run's `with` block exited,
    # so reopen a fresh one to read back what actually landed on disk.
    with _DiskStore(receipt_root) as reopened_store:
        manifest_bytes = reopened_store.read_sealed(result["manifest"])
    manifest = json.loads(manifest_bytes)
    assert [member["canonical_url"] for member in manifest["members"]] == (
        expected_routes
    )
    assert manifest["selection_digest"] == result["selection_digest"]
