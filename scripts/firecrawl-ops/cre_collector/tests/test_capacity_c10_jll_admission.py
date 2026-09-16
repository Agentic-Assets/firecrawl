"""Hermetic JLL-only admission contracts; no provider transport is constructed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from capacity_c10 import admission, contracts, host_registry, jll_admission

# The sealed GraphQL enumeration response carries this many unique candidates
# so selection sorting and the take-sixteen cut are both exercised: sixteen
# get selected, two (17 and 18) are deliberately left out.
CANDIDATE_COUNT = 18


def _private(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _write(path: Path, value: object) -> None:
    path.write_bytes(contracts.canonical_bytes(value))
    path.chmod(0o600)


def _write_bytes(path: Path, body: bytes) -> None:
    path.write_bytes(body)
    path.chmod(0o600)


def _binding() -> dict[str, str]:
    return {
        key: "a" * 64
        for key in (
            "planSha256",
            "cohortSha256",
            "policySha256",
            "sourceSha256",
            "armSha256",
            "implementationSha256",
        )
    }


def _no_write() -> dict[str, int]:
    return {
        "database_writes": 0,
        "cache_writes": 0,
        "status_writes": 0,
        "scheduler_writes": 0,
        "model_or_ocr_changes": 0,
    }


def _receipt(
    *,
    stage: str,
    member: str | None,
    binding: dict[str, str],
    private_artifact_sha256: str,
    accounting: dict[str, Any],
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "cre_capacity_c10_private_source_receipt",
        "stage": stage,
        "sourceKey": "jll",
        "memberKey": member,
        "binding": binding,
        "noWrite": _no_write(),
        "requestAccounting": accounting,
        "privateArtifactSha256": private_artifact_sha256,
    }
    return {**unsigned, "receiptSha256": contracts.sha256(unsigned)}


def _resign_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in receipt.items() if key != "receiptSha256"}
    return {**unsigned, "receiptSha256": contracts.sha256(unsigned)}


def _resign_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    unsigned = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    return {**unsigned, "manifest_sha256": contracts.sha256(unsigned)}


def _candidate(n: int) -> dict[str, str]:
    return {
        "provider_id": str(1000 + n),
        "canonical_url": f"https://property.jll.com/listings/property-{n:02d}",
    }


def _graphql_payload(indices: list[int]) -> dict[str, Any]:
    items = [
        {"id": _candidate(n)["provider_id"], "pageUrl": f"/listings/property-{n:02d}"}
        for n in indices
    ]
    return {"errors": [], "data": {"properties": {"count": len(items), "items": items}}}


def _write_controller_completion(
    root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    session_sha256: str | None = None,
    run_sha256: str | None = None,
) -> str:
    """Seal a controller completion record exactly as the real controller would.

    Content-addressed name (``jll-admission-completion-<sha>.sealed``), owner
    0600, canonical bytes -- mirrors what
    ``_JllAdmissionController._seal_completion`` seals via
    ``PrivateReceiptStore.seal_json`` in production, so offline scenarios that
    represent a controller-accepted root can satisfy
    ``jll_admission._verify_controller_completion``. Returns the written name.
    """
    raw = manifest_path.read_bytes()
    unsigned = {
        "schema_version": 1,
        "kind": jll_admission.JLL_CONTROLLER_COMPLETION_KIND,
        "receipt_root": str(root),
        "receipt_manifest": {
            "name": manifest_path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        },
        "manifest_sha256": manifest["manifest_sha256"],
        "selection_digest": manifest["selection_digest"],
        "adapter_implementation_sha256": manifest["adapter_implementation_sha256"],
        "session_sha256": session_sha256 or "1" * 64,
        "run_sha256": run_sha256 or "2" * 64,
    }
    record = {**unsigned, "completion_sha256": contracts.sha256(unsigned)}
    body = contracts.canonical_bytes(record)
    digest = hashlib.sha256(body).hexdigest()
    name = f"{jll_admission.JLL_CONTROLLER_COMPLETION_STEM}-{digest}.sealed"
    _write_bytes(root / name, body)
    return name


def _replace_artifact(
    root: Path, artifacts: list[dict[str, Any]], old_sha256: str, new_body: bytes
) -> str:
    """Swap one indexed artifact's sealed bytes for new bytes; return its digest."""
    new_sha256 = hashlib.sha256(new_body).hexdigest()
    index = next(i for i, item in enumerate(artifacts) if item["sha256"] == old_sha256)
    (root / artifacts[index]["name"]).unlink()
    name = f"artifact-{new_sha256}.sealed"
    _write_bytes(root / name, new_body)
    artifacts[index] = {"name": name, "sha256": new_sha256, "bytes": len(new_body)}
    return new_sha256


class _Scenario:
    """A fully self-consistent, sealed JLL receipt manifest plus its raw pieces.

    Every mutation test starts from one of these and changes exactly the
    piece(s) needed to trip one check, re-signing whatever digests that
    change makes stale so every *other* check still passes.
    """

    def __init__(self, root: Path, **kwargs: Any) -> None:
        self.root = root
        self.artifacts: list[dict[str, Any]] = []
        self.binding = _binding()

        body_indices = kwargs.get(
            "body_indices", list(reversed(range(1, CANDIDATE_COUNT + 1)))
        )
        self.payload = _graphql_payload(body_indices)
        self.body_bytes = contracts.canonical_bytes(self.payload)
        self.body_sha256 = hashlib.sha256(self.body_bytes).hexdigest()

        # True recomputation from the sealed body; the default manifest order
        # matches this exactly (candidates 1..16, ascending canonical url).
        true_selection = jll_admission.select_jll_admission_members(self.payload)
        self.true_selection = true_selection

        manifest_order = kwargs.get("manifest_order", list(range(1, 17)))
        self.manifest_members = [
            {"key": f"jll-{position + 1}", **_candidate(n)}
            for position, n in enumerate(manifest_order)
        ]

        card = {
            "id": jll_admission.JLL_ENUMERATION_CARD_ID,
            "sourceKey": "jll",
            "stage": "enumeration",
            "method": "POST",
            "url": jll_admission.JLL_GRAPHQL_URL,
            "bodySha256": jll_admission.JLL_ENUMERATION_BODY_SHA256,
        }
        card.update(kwargs.get("card_overrides", {}))
        response = {
            "status": 200,
            "finalUrl": jll_admission.JLL_GRAPHQL_URL,
            "challengeDetected": False,
            "redirectCount": 0,
            "providerAttempts": 1,
            "cacheMode": "no-store",
            "bodySha256": self.body_sha256,
            "bodyArtifactSha256": self.body_sha256,
            "elapsedMs": 42,
        }
        response.update(kwargs.get("response_overrides", {}))
        self.card, self.response = card, response
        self.event = {"binding": self.binding, "card": card, "response": response}
        event_body = contracts.canonical_bytes(self.event)
        self.event_sha256 = hashlib.sha256(event_body).hexdigest()

        accounting_event = {
            "cardId": jll_admission.JLL_ENUMERATION_CARD_ID,
            "outcome": "accepted",
            "status": response["status"],
            "elapsedMs": response.get("elapsedMs"),
            "bytes": len(self.body_bytes),
            "bodySha256": self.body_sha256,
            "privateEventSha256": self.event_sha256,
        }
        self.accounting = kwargs.get(
            "accounting_overrides",
            {
                "logicalRequests": 1,
                "attempts": 1,
                "retries": 0,
                "eventsSha256": contracts.sha256([accounting_event]),
            },
        )

        expected_claim = {
            "rule": true_selection["rule"],
            "candidateCount": true_selection["candidate_count"],
            "selectedMembers": [
                {
                    "key": member["key"],
                    "providerId": member["provider_id"],
                    "canonicalUrl": member["canonical_url"],
                }
                for member in true_selection["members"]
            ],
            "digest": true_selection["digest"],
        }
        self.expected_claim = expected_claim
        evidence_selection = kwargs.get("evidence_selection_overrides")
        selection_claim = (
            expected_claim
            if evidence_selection is None
            else {**expected_claim, **evidence_selection}
        )
        enum_evidence = {"selection": selection_claim, "memberGraph": {"nodes": []}}

        self.enum_private = {
            "binding": self.binding,
            "sourceKey": "jll",
            "stage": "enumeration",
            "memberKey": None,
            "requestAccounting": self.accounting,
            "evidence": enum_evidence,
        }
        enum_body = contracts.canonical_bytes(self.enum_private)
        self.enum_sha256 = hashlib.sha256(enum_body).hexdigest()
        self.enum_receipt = _receipt(
            stage="enumeration",
            member=None,
            binding=self.binding,
            private_artifact_sha256=self.enum_sha256,
            accounting=self.accounting,
        )

        self.member_privates: list[dict[str, Any]] = []
        self.member_receipts: list[dict[str, Any]] = []
        member_evidence_overrides = kwargs.get("member_evidence_overrides", {})
        for position, member in enumerate(self.manifest_members):
            member_accounting = {
                "logicalRequests": 1,
                "attempts": 1,
                "retries": 0,
                "eventsSha256": "b" * 64,
            }
            member_evidence = {
                "member": {
                    "canonicalUrl": member["canonical_url"],
                    "providerId": member["provider_id"],
                }
            }
            member_evidence = member_evidence_overrides.get(position, member_evidence)
            member_private = {
                "binding": self.binding,
                "sourceKey": "jll",
                "stage": "member",
                "memberKey": member["key"],
                "requestAccounting": member_accounting,
                "evidence": member_evidence,
            }
            member_body = contracts.canonical_bytes(member_private)
            member_sha256 = hashlib.sha256(member_body).hexdigest()
            self.member_privates.append(member_private)
            self.member_receipts.append(
                _receipt(
                    stage="member",
                    member=member["key"],
                    binding=self.binding,
                    private_artifact_sha256=member_sha256,
                    accounting=member_accounting,
                )
            )

        # Seal every artifact: enumeration stage, event, raw body, sixteen
        # member stages. All are indexed so `_revalidate_artifact_index` and
        # the enumeration-event search both find them.
        self._seal(self.enum_private, expect_sha256=self.enum_sha256)
        self._seal(self.event, expect_sha256=self.event_sha256)
        self._seal_bytes(self.body_bytes, expect_sha256=self.body_sha256)
        for private, receipt in zip(
            self.member_privates, self.member_receipts, strict=True
        ):
            self._seal(private, expect_sha256=receipt["privateArtifactSha256"])

        unsigned = {
            "schema_version": 1,
            "kind": jll_admission.JLL_RECEIPT_MANIFEST_KIND,
            "receipt_root": str(root),
            "collection_intent": jll_admission._jll_intent(),
            "members": self.manifest_members,
            "enumeration": self.enum_receipt,
            "member_receipts": self.member_receipts,
            "artifacts": self.artifacts,
            "adapter_implementation_sha256": "d" * 64,
            "no_write": admission.NO_WRITE,
            "collection_intent_sha256": jll_admission._collection_intent_sha256(
                self.manifest_members
            ),
            "selection_digest": jll_admission._selection_digest(self.manifest_members),
        }
        self.manifest = _resign_manifest({**unsigned, "manifest_sha256": ""})
        self.path = root / "jll-manifest.json"
        _write(self.path, self.manifest)
        # A fresh scenario represents a root the controller already accepted:
        # seal the matching completion attestation, exactly like the real
        # controller does after `_verify_manifest` succeeds. Tests exercising
        # the reviewer-fix's recovery/quarantine/tamper surface remove or
        # mutate this file explicitly (see test_capacity_c10_jll_admission.py
        # section B and test_capacity_c10_jll_admission_controller.py).
        self.completion_name = _write_controller_completion(
            self.root, self.path, self.manifest
        )

    def _seal(self, value: Any, *, expect_sha256: str) -> None:
        body = contracts.canonical_bytes(value)
        sha256 = hashlib.sha256(body).hexdigest()
        assert sha256 == expect_sha256
        self._seal_bytes(body, expect_sha256=sha256)

    def _seal_bytes(self, body: bytes, *, expect_sha256: str) -> None:
        name = f"artifact-{expect_sha256}.sealed"
        _write_bytes(self.root / name, body)
        self.artifacts.append(
            {"name": name, "sha256": expect_sha256, "bytes": len(body)}
        )

    def save(self) -> None:
        _write(self.path, self.manifest)


def _placeholder_manifest(root: Path, *, count: int = 16) -> Path:
    """A cheap, structurally-valid-enough manifest for the count-arity test.

    `_validate_manifest` rejects a member/receipt count other than sixteen
    before it does any of the deeper (selection, evidence, event) checks, so
    this does not need to be a fully realistic scenario.
    """
    members = [
        {
            "key": f"jll-{index + 1}",
            "provider_id": str(index + 1),
            "canonical_url": f"https://property.jll.com/listings/member-{index + 1}",
        }
        for index in range(count)
    ]
    receipts: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    for index, (stage, member) in enumerate(
        [("enumeration", None), *(("member", item["key"]) for item in members)]
    ):
        provisional = _receipt(
            stage=stage,
            member=member,
            binding=_binding(),
            private_artifact_sha256="0" * 64,
            accounting={
                "logicalRequests": 1,
                "attempts": 1,
                "retries": 0,
                "eventsSha256": "b" * 64,
            },
        )
        stage_payload = {
            "binding": provisional["binding"],
            "sourceKey": "jll",
            "stage": stage,
            "memberKey": member,
            "requestAccounting": provisional["requestAccounting"],
            "evidence": {"sealed": index},
        }
        body = contracts.canonical_bytes(stage_payload)
        name = f"artifact-{index}.sealed"
        target = root / name
        target.write_bytes(body)
        target.chmod(0o600)
        artifacts.append(
            {
                "name": name,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
        receipts.append(
            _receipt(
                stage=stage,
                member=member,
                binding=provisional["binding"],
                private_artifact_sha256=artifacts[-1]["sha256"],
                accounting=provisional["requestAccounting"],
            )
        )
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "kind": jll_admission.JLL_RECEIPT_MANIFEST_KIND,
        "receipt_root": str(root),
        "collection_intent": jll_admission._jll_intent(),
        "members": members,
        "enumeration": receipts[0],
        "member_receipts": receipts[1:],
        "artifacts": artifacts,
        "adapter_implementation_sha256": "d" * 64,
        "no_write": admission.NO_WRITE,
        "collection_intent_sha256": jll_admission._collection_intent_sha256(members),
        "selection_digest": jll_admission._selection_digest(members),
    }
    manifest = {**unsigned, "manifest_sha256": contracts.sha256(unsigned)}
    path = root / "jll-manifest.json"
    _write(path, manifest)
    return path


def _manifest(root: Path, *, count: int = 16) -> Path:
    if count == 16:
        return _Scenario(root).path
    return _placeholder_manifest(root, count=count)


def _bundle(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch, admission_root: Path
) -> dict[str, str]:
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    return jll_admission.build_jll_bundle(
        receipt_root=scenario.root,
        receipt_manifest=scenario.path,
        admission_root=admission_root,
    )


def test_jll_receipts_seal_exactly_sixteen_members_and_render_review_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )

    result = jll_admission.build_jll_bundle(
        receipt_root=receipt_root,
        receipt_manifest=manifest,
        admission_root=admission_root,
    )
    proposal = jll_admission.render_jll_authority(Path(result["path"]))

    assert proposal["kind"] == "cre_capacity_c10_jll_v1_authority"
    assert proposal["approved_cohort_sha256"] == result["cohort_sha256"]
    assert proposal["approved_adapter_sha256"] == "d" * 64


def test_jll_plan_needs_its_own_pin_and_never_uses_the_twenty_source_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    bundle = jll_admission.build_jll_bundle(
        receipt_root=receipt_root,
        receipt_manifest=manifest,
        admission_root=admission_root,
    )
    bundle_path = Path(bundle["path"])
    plan = jll_admission.render_jll_plan(bundle_path)
    with pytest.raises(contracts.C10Error, match="separate repository authority"):
        jll_admission.validate_jll_plan(plan)
    proposal = jll_admission.render_jll_authority(bundle_path)
    monkeypatch.setattr(jll_admission, "load_jll_authority", lambda: proposal)
    jll_admission.validate_jll_plan(plan)
    stored = json.loads(bundle_path.read_text())
    registry = host_registry.C10SealedCardRegistry(plan, stored["cohort"])
    assert registry.resolve("jll-enumeration")["id"] == "jll-enumeration"


@pytest.mark.parametrize("count", [15, 17])
def test_jll_partial_or_excess_member_sets_never_publish_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root, count=count)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )

    with pytest.raises(contracts.C10Error, match="exactly sixteen"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=manifest,
            admission_root=admission_root,
        )
    assert list(admission_root.iterdir()) == []


def test_tampered_receipt_manifest_cannot_render_jll_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    bundle = jll_admission.build_jll_bundle(
        receipt_root=receipt_root,
        receipt_manifest=manifest,
        admission_root=admission_root,
    )
    value = json.loads(manifest.read_text())
    value["members"][0]["provider_id"] = "999"
    _write(manifest, value)

    with pytest.raises(contracts.C10Error, match="manifest digest"):
        jll_admission.render_jll_authority(Path(bundle["path"]))


def test_jll_manifest_rejects_mixed_receipt_binding_before_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root)
    value = json.loads(manifest.read_text())
    value["member_receipts"][0]["binding"]["planSha256"] = "e" * 64
    receipt = value["member_receipts"][0]
    receipt["receiptSha256"] = contracts.sha256(
        {key: item for key, item in receipt.items() if key != "receiptSha256"}
    )
    value["manifest_sha256"] = contracts.sha256(
        {key: item for key, item in value.items() if key != "manifest_sha256"}
    )
    _write(manifest, value)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )

    with pytest.raises(contracts.C10Error, match="sealed stage artifact"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=manifest,
            admission_root=admission_root,
        )


def test_jll_tampered_or_missing_sealed_artifact_never_publishes_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    stored = json.loads(manifest.read_text())
    target_name = stored["artifacts"][0]["name"]
    (receipt_root / target_name).write_text("tampered")
    (receipt_root / target_name).chmod(0o600)
    with pytest.raises(contracts.C10Error, match="sealed receipt artifact"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=manifest,
            admission_root=admission_root,
        )
    assert list(admission_root.iterdir()) == []


def test_jll_public_receipts_cannot_share_or_mislabel_a_sealed_stage_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    manifest = _manifest(receipt_root)
    value = json.loads(manifest.read_text())
    value["member_receipts"][0]["privateArtifactSha256"] = value["enumeration"][
        "privateArtifactSha256"
    ]
    receipt = value["member_receipts"][0]
    receipt["receiptSha256"] = contracts.sha256(
        {key: item for key, item in receipt.items() if key != "receiptSha256"}
    )
    value["manifest_sha256"] = contracts.sha256(
        {key: item for key, item in value.items() if key != "manifest_sha256"}
    )
    _write(manifest, value)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    with pytest.raises(contracts.C10Error, match="cannot share"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=manifest,
            admission_root=admission_root,
        )


def test_jll_collection_cli_is_dry_run_only_outside_production_controller(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert jll_admission.main(["collect-jll"]) == 0
    assert json.loads(capsys.readouterr().out)["external_calls"] is False
    with pytest.raises(SystemExit, match="2"):
        jll_admission.main(["collect-jll", "--execute"])


# --- Independent-recomputation attack surface ------------------------------
#
# `_validate_manifest` independently recomputes the JLL cohort from the
# sealed native enumeration response body; it must not trust the manifest's
# own member list, selection digest, or evidence claims. Each test below
# starts from one fully self-consistent `_Scenario` and mutates exactly one
# thing, re-signing whatever digests that change makes stale so *only* the
# targeted check can fail.


def test_reordered_members_are_rejected_as_recomputed_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    # Same sixteen candidates, but positions 1 and 2 are swapped: the manifest
    # digests are self-consistent with this order, but it no longer matches
    # the ascending canonical-url order recomputed from the sealed body.
    order = [2, 1, *range(3, 17)]
    scenario = _Scenario(receipt_root, manifest_order=order)

    with pytest.raises(contracts.C10Error, match="recomputed source selection"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_swapped_to_unselected_candidate_is_rejected_as_recomputed_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    # Candidate 17 (never selected: only 1..16 are) replaces candidate 16.
    order = [*range(1, 16), 17]
    scenario = _Scenario(receipt_root, manifest_order=order)

    with pytest.raises(contracts.C10Error, match="recomputed source selection"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_evidence_selection_disagreement_is_rejected_as_recomputed_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(
        receipt_root, evidence_selection_overrides={"candidateCount": 999}
    )

    with pytest.raises(contracts.C10Error, match="recomputed source selection"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_replaced_body_with_different_selection_is_rejected_as_recomputed_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)

    # A different, still-valid GraphQL body (candidates shifted by one) whose
    # true selection differs from what the manifest and evidence still claim.
    new_payload = _graphql_payload(list(reversed(range(2, CANDIDATE_COUNT + 2))))
    new_body = contracts.canonical_bytes(new_payload)
    new_body_sha256 = hashlib.sha256(new_body).hexdigest()
    _replace_artifact(receipt_root, scenario.artifacts, scenario.body_sha256, new_body)

    new_response = {
        **scenario.response,
        "bodySha256": new_body_sha256,
        "bodyArtifactSha256": new_body_sha256,
    }
    new_event = {**scenario.event, "response": new_response}
    new_accounting_event = {
        "cardId": jll_admission.JLL_ENUMERATION_CARD_ID,
        "outcome": "accepted",
        "status": new_response["status"],
        "elapsedMs": new_response.get("elapsedMs"),
        "bytes": len(new_body),
        "bodySha256": new_body_sha256,
        "privateEventSha256": "",
    }
    new_event_body = contracts.canonical_bytes(new_event)
    new_event_sha256 = hashlib.sha256(new_event_body).hexdigest()
    new_accounting_event["privateEventSha256"] = new_event_sha256
    new_event_body = contracts.canonical_bytes(
        new_event
    )  # unchanged; sha computed above
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.event_sha256, new_event_body
    )

    new_accounting = {
        "logicalRequests": 1,
        "attempts": 1,
        "retries": 0,
        "eventsSha256": contracts.sha256([new_accounting_event]),
    }
    scenario.enum_private = {
        **scenario.enum_private,
        "requestAccounting": new_accounting,
    }
    new_enum_body = contracts.canonical_bytes(scenario.enum_private)
    new_enum_sha256 = hashlib.sha256(new_enum_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.enum_sha256, new_enum_body
    )
    scenario.enum_receipt = _resign_receipt(
        {
            **scenario.enum_receipt,
            "requestAccounting": new_accounting,
            "privateArtifactSha256": new_enum_sha256,
        }
    )
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "enumeration": scenario.enum_receipt}
    )
    scenario.save()

    with pytest.raises(contracts.C10Error, match="recomputed source selection"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_events_sha256_not_matching_the_sealed_event_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)

    bad_accounting = {**scenario.accounting, "eventsSha256": "f" * 64}
    scenario.enum_private = {
        **scenario.enum_private,
        "requestAccounting": bad_accounting,
    }
    new_enum_body = contracts.canonical_bytes(scenario.enum_private)
    new_enum_sha256 = hashlib.sha256(new_enum_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.enum_sha256, new_enum_body
    )
    scenario.enum_receipt = _resign_receipt(
        {
            **scenario.enum_receipt,
            "requestAccounting": bad_accounting,
            "privateArtifactSha256": new_enum_sha256,
        }
    )
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "enumeration": scenario.enum_receipt}
    )
    scenario.save()

    with pytest.raises(
        contracts.C10Error, match="does not account for its sealed event"
    ):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_missing_enumeration_event_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    entry = next(
        item for item in scenario.artifacts if item["sha256"] == scenario.event_sha256
    )
    (receipt_root / entry["name"]).unlink()
    scenario.artifacts.remove(entry)
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "artifacts": scenario.artifacts}
    )
    scenario.save()

    with pytest.raises(
        contracts.C10Error, match="exactly one sealed enumeration event"
    ):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_duplicate_enumeration_event_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    duplicate_event = {
        **scenario.event,
        "response": {**scenario.response, "elapsedMs": 999},
    }
    duplicate_body = contracts.canonical_bytes(duplicate_event)
    duplicate_sha256 = hashlib.sha256(duplicate_body).hexdigest()
    assert duplicate_sha256 != scenario.event_sha256
    _write_bytes(receipt_root / f"artifact-{duplicate_sha256}.sealed", duplicate_body)
    scenario.artifacts.append(
        {
            "name": f"artifact-{duplicate_sha256}.sealed",
            "sha256": duplicate_sha256,
            "bytes": len(duplicate_body),
        }
    )
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "artifacts": scenario.artifacts}
    )
    scenario.save()

    with pytest.raises(
        contracts.C10Error, match="exactly one sealed enumeration event"
    ):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


@pytest.mark.parametrize(
    "response_overrides",
    [
        {"bodyArtifactSha256": "9" * 64, "bodySha256": "9" * 64},
        {"finalUrl": "https://property.jll.com/other"},
        {"challengeDetected": True},
    ],
)
def test_event_response_binding_mismatch_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_overrides: dict[str, Any],
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    new_response = {**scenario.response, **response_overrides}
    new_event = {**scenario.event, "response": new_response}
    new_event_body = contracts.canonical_bytes(new_event)
    new_event_sha256 = hashlib.sha256(new_event_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.event_sha256, new_event_body
    )

    # eventsSha256 must still reference this exact (now-invalid) event so the
    # only tripped check is the response-binding one, not the accounting one.
    accounting_event = {
        "cardId": jll_admission.JLL_ENUMERATION_CARD_ID,
        "outcome": "accepted",
        "status": new_response["status"],
        "elapsedMs": new_response.get("elapsedMs"),
        "bytes": len(scenario.body_bytes),
        "bodySha256": scenario.body_sha256,
        "privateEventSha256": new_event_sha256,
    }
    new_accounting = {
        "logicalRequests": 1,
        "attempts": 1,
        "retries": 0,
        "eventsSha256": contracts.sha256([accounting_event]),
    }
    scenario.enum_private = {
        **scenario.enum_private,
        "requestAccounting": new_accounting,
    }
    new_enum_body = contracts.canonical_bytes(scenario.enum_private)
    new_enum_sha256 = hashlib.sha256(new_enum_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.enum_sha256, new_enum_body
    )
    scenario.enum_receipt = _resign_receipt(
        {
            **scenario.enum_receipt,
            "requestAccounting": new_accounting,
            "privateArtifactSha256": new_enum_sha256,
        }
    )
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "enumeration": scenario.enum_receipt}
    )
    scenario.save()

    with pytest.raises(contracts.C10Error, match="does not bind its response"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_member_evidence_canonical_url_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(
        receipt_root,
        member_evidence_overrides={
            0: {
                "member": {
                    "canonicalUrl": "https://property.jll.com/listings/not-the-member",
                    "providerId": _candidate(1)["provider_id"],
                }
            }
        },
    )

    with pytest.raises(contracts.C10Error, match="does not bind its selected member"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_body_with_insufficient_candidates_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)

    new_payload = _graphql_payload(list(range(1, 16)))  # only fifteen candidates
    new_body = contracts.canonical_bytes(new_payload)
    new_body_sha256 = hashlib.sha256(new_body).hexdigest()
    _replace_artifact(receipt_root, scenario.artifacts, scenario.body_sha256, new_body)

    new_response = {
        **scenario.response,
        "bodySha256": new_body_sha256,
        "bodyArtifactSha256": new_body_sha256,
    }
    new_event = {**scenario.event, "response": new_response}
    new_event_body = contracts.canonical_bytes(new_event)
    new_event_sha256 = hashlib.sha256(new_event_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.event_sha256, new_event_body
    )

    accounting_event = {
        "cardId": jll_admission.JLL_ENUMERATION_CARD_ID,
        "outcome": "accepted",
        "status": new_response["status"],
        "elapsedMs": new_response.get("elapsedMs"),
        "bytes": len(new_body),
        "bodySha256": new_body_sha256,
        "privateEventSha256": new_event_sha256,
    }
    new_accounting = {
        "logicalRequests": 1,
        "attempts": 1,
        "retries": 0,
        "eventsSha256": contracts.sha256([accounting_event]),
    }
    scenario.enum_private = {
        **scenario.enum_private,
        "requestAccounting": new_accounting,
    }
    new_enum_body = contracts.canonical_bytes(scenario.enum_private)
    new_enum_sha256 = hashlib.sha256(new_enum_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.enum_sha256, new_enum_body
    )
    scenario.enum_receipt = _resign_receipt(
        {
            **scenario.enum_receipt,
            "requestAccounting": new_accounting,
            "privateArtifactSha256": new_enum_sha256,
        }
    )
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "enumeration": scenario.enum_receipt}
    )
    scenario.save()

    with pytest.raises(contracts.C10Error, match="insufficient"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


def test_body_with_duplicate_candidate_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)

    new_payload = _graphql_payload(
        [*range(1, CANDIDATE_COUNT + 1), 1]
    )  # candidate 1 twice
    new_body = contracts.canonical_bytes(new_payload)
    new_body_sha256 = hashlib.sha256(new_body).hexdigest()
    _replace_artifact(receipt_root, scenario.artifacts, scenario.body_sha256, new_body)

    new_response = {
        **scenario.response,
        "bodySha256": new_body_sha256,
        "bodyArtifactSha256": new_body_sha256,
    }
    new_event = {**scenario.event, "response": new_response}
    new_event_body = contracts.canonical_bytes(new_event)
    new_event_sha256 = hashlib.sha256(new_event_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.event_sha256, new_event_body
    )

    accounting_event = {
        "cardId": jll_admission.JLL_ENUMERATION_CARD_ID,
        "outcome": "accepted",
        "status": new_response["status"],
        "elapsedMs": new_response.get("elapsedMs"),
        "bytes": len(new_body),
        "bodySha256": new_body_sha256,
        "privateEventSha256": new_event_sha256,
    }
    new_accounting = {
        "logicalRequests": 1,
        "attempts": 1,
        "retries": 0,
        "eventsSha256": contracts.sha256([accounting_event]),
    }
    scenario.enum_private = {
        **scenario.enum_private,
        "requestAccounting": new_accounting,
    }
    new_enum_body = contracts.canonical_bytes(scenario.enum_private)
    new_enum_sha256 = hashlib.sha256(new_enum_body).hexdigest()
    _replace_artifact(
        receipt_root, scenario.artifacts, scenario.enum_sha256, new_enum_body
    )
    scenario.enum_receipt = _resign_receipt(
        {
            **scenario.enum_receipt,
            "requestAccounting": new_accounting,
            "privateArtifactSha256": new_enum_sha256,
        }
    )
    scenario.manifest = _resign_manifest(
        {**scenario.manifest, "enumeration": scenario.enum_receipt}
    )
    scenario.save()

    with pytest.raises(contracts.C10Error, match="duplicate"):
        _bundle(scenario, monkeypatch, admission_root)
    assert list(admission_root.iterdir()) == []


# --- Controller-completion attestation (reviewer recovery fix) -------------
#
# The child seals its manifest *before* the controller verifies it, so a
# manifest alone on disk never proves the controller accepted the root (see
# `_verify_controller_completion` in jll_admission.py and
# `_JllAdmissionController._seal_completion` in admission_controller.py). Every
# `_Scenario` now seals a matching completion record at construction time
# (`scenario.completion_name`); these tests remove or mutate that file to
# prove `build_jll_bundle`, `render_jll_authority`, and `render_jll_plan` all
# refuse a root the controller never (or ambiguously) attested.


def test_missing_controller_completion_rejects_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    (receipt_root / scenario.completion_name).unlink()
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    with pytest.raises(contracts.C10Error, match="controller completion attestation"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=scenario.path,
            admission_root=admission_root,
        )
    assert list(admission_root.iterdir()) == []


@pytest.mark.parametrize("render_name", ["render_jll_authority", "render_jll_plan"])
def test_missing_controller_completion_rejects_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render_name: str
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    bundle = _bundle(scenario, monkeypatch, admission_root)
    (receipt_root / scenario.completion_name).unlink()
    render = getattr(jll_admission, render_name)
    with pytest.raises(contracts.C10Error, match="controller completion attestation"):
        render(Path(bundle["path"]))


def test_two_controller_completion_files_reject_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    # A second, differently-nonced completion record for the same manifest:
    # distinct content -> distinct content-addressed filename -> two files.
    second_name = _write_controller_completion(
        receipt_root, scenario.path, scenario.manifest, session_sha256="3" * 64
    )
    assert second_name != scenario.completion_name
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    with pytest.raises(contracts.C10Error, match="controller completion attestation"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=scenario.path,
            admission_root=admission_root,
        )
    assert list(admission_root.iterdir()) == []


@pytest.mark.parametrize("render_name", ["render_jll_authority", "render_jll_plan"])
def test_two_controller_completion_files_reject_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render_name: str
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    bundle = _bundle(scenario, monkeypatch, admission_root)
    _write_controller_completion(
        receipt_root, scenario.path, scenario.manifest, session_sha256="3" * 64
    )
    render = getattr(jll_admission, render_name)
    with pytest.raises(contracts.C10Error, match="controller completion attestation"):
        render(Path(bundle["path"]))


def test_tampered_controller_completion_digest_rejects_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    completion_path = receipt_root / scenario.completion_name
    record = json.loads(completion_path.read_text())
    # Mutate the record's content without renaming the content-addressed
    # file, so the embedded digest in the filename no longer matches.
    record["run_sha256"] = "9" * 64
    _write(completion_path, record)
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    with pytest.raises(contracts.C10Error, match="digest is invalid"):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=scenario.path,
            admission_root=admission_root,
        )
    assert list(admission_root.iterdir()) == []


@pytest.mark.parametrize("render_name", ["render_jll_authority", "render_jll_plan"])
def test_tampered_controller_completion_digest_rejects_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render_name: str
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    bundle = _bundle(scenario, monkeypatch, admission_root)
    completion_path = receipt_root / scenario.completion_name
    record = json.loads(completion_path.read_text())
    record["run_sha256"] = "9" * 64
    _write(completion_path, record)
    render = getattr(jll_admission, render_name)
    with pytest.raises(contracts.C10Error, match="digest is invalid"):
        render(Path(bundle["path"]))


def test_controller_completion_manifest_name_mismatch_rejects_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion sealed for a differently-named manifest cannot admit this one.

    Same bytes, same digests, only the recorded receipt-manifest *name*
    differs -- proving `receipt_manifest.name` is checked, not inferred.
    """
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    (receipt_root / scenario.completion_name).unlink()
    decoy_manifest_path = receipt_root / "decoy-manifest.json"
    decoy_manifest_path.write_bytes(scenario.path.read_bytes())
    decoy_manifest_path.chmod(0o600)
    _write_controller_completion(receipt_root, decoy_manifest_path, scenario.manifest)
    decoy_manifest_path.unlink()
    monkeypatch.setattr(
        jll_admission, "repository_implementation_sha256", lambda _key: "d" * 64
    )
    with pytest.raises(
        contracts.C10Error, match="does not attest this receipt manifest"
    ):
        jll_admission.build_jll_bundle(
            receipt_root=receipt_root,
            receipt_manifest=scenario.path,
            admission_root=admission_root,
        )
    assert list(admission_root.iterdir()) == []


@pytest.mark.parametrize("render_name", ["render_jll_authority", "render_jll_plan"])
def test_controller_completion_manifest_name_mismatch_rejects_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render_name: str
) -> None:
    receipt_root, admission_root = (
        _private(tmp_path / "receipts"),
        _private(tmp_path / "admission"),
    )
    scenario = _Scenario(receipt_root)
    bundle = _bundle(scenario, monkeypatch, admission_root)
    (receipt_root / scenario.completion_name).unlink()
    decoy_manifest_path = receipt_root / "decoy-manifest.json"
    decoy_manifest_path.write_bytes(scenario.path.read_bytes())
    decoy_manifest_path.chmod(0o600)
    _write_controller_completion(receipt_root, decoy_manifest_path, scenario.manifest)
    decoy_manifest_path.unlink()
    render = getattr(jll_admission, render_name)
    with pytest.raises(
        contracts.C10Error, match="does not attest this receipt manifest"
    ):
        render(Path(bundle["path"]))
