"""Hermetic JLL-only admission contracts; no provider transport is constructed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from capacity_c10 import admission, contracts, host_registry, jll_admission


def _private(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)


def _receipt(stage: str, member: str | None, artifact_sha256: str) -> dict[str, object]:
    binding = {
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
    unsigned: dict[str, object] = {
        "schemaVersion": 1,
        "kind": "cre_capacity_c10_private_source_receipt",
        "stage": stage,
        "sourceKey": "jll",
        "memberKey": member,
        "binding": binding,
        "noWrite": {
            "database_writes": 0,
            "cache_writes": 0,
            "status_writes": 0,
            "scheduler_writes": 0,
            "model_or_ocr_changes": 0,
        },
        "requestAccounting": {
            "logicalRequests": 1,
            "attempts": 1,
            "retries": 0,
            "eventsSha256": "b" * 64,
        },
        "privateArtifactSha256": artifact_sha256,
    }
    return {**unsigned, "receiptSha256": contracts.sha256(unsigned)}


def _manifest(root: Path, *, count: int = 16) -> Path:
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
        provisional = _receipt(stage, member, "0" * 64)
        stage_payload = {
            "binding": provisional["binding"],
            "sourceKey": "jll",
            "stage": stage,
            "memberKey": member,
            "requestAccounting": provisional["requestAccounting"],
            "evidence": {"sealed": index},
        }
        body = json.dumps(stage_payload, sort_keys=True, separators=(",", ":")).encode()
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
        receipts.append(_receipt(stage, member, artifacts[-1]["sha256"]))
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
    }
    manifest = {**unsigned, "manifest_sha256": contracts.sha256(unsigned)}
    path = root / "jll-manifest.json"
    _write(path, manifest)
    return path


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
    (receipt_root / "artifact-0.sealed").write_text("tampered")
    (receipt_root / "artifact-0.sealed").chmod(0o600)
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
