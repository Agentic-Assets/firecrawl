"""Hermetic contracts for the offline reviewed C10 admission chain."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from capacity_c10 import adapters, admission, admission_chain, contracts, policy


def _private(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _write_private(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)


def _ready_cohort() -> dict[str, object]:
    sources: list[dict[str, object]] = []
    for source in policy.load_policy()["sources"]:
        members = [{"provider_id": f"{source['key']}-{index}"} for index in range(16)]
        sources.append(
            {
                "source_key": source["key"],
                "plane": source["plane"],
                "core_state": "ready",
                "core_target_rows": 16,
                "core_selected_rows": 16,
                "core": members,
                "fresh_enumeration": {
                    "population_state": "verified",
                    "total_population": 16,
                    "receipt_sha256": contracts.sha256(source["key"] + "enum"),
                },
            }
        )
    cohort: dict[str, object] = {
        "schema_version": 1,
        "kind": "cre_capacity_multisource_v1_cohort",
        "config_sha256": contracts.sha256("test-config"),
        "sampling": {"core_per_source": 16},
        "sources": sources,
        "planes": {},
        "aggregate": {"state": "ready_for_review"},
    }
    cohort["cohort_sha256"] = contracts.sha256(
        {key: cohort[key] for key in admission.COHORT_HASH_FIELDS}
    )
    return cohort


def _manifest(root: Path) -> Path:
    path = root / "manifest.json"
    _write_private(
        path,
        {
            "schema_version": 1,
            "kind": "cre_capacity_multisource_v1_receipts",
            "receipt_root": str(root),
            "receipts": [],
        },
    )
    return path


def _bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object]]:
    receipt_root = _private(tmp_path / "receipts")
    admission_root = _private(tmp_path / "admission")
    manifest = _manifest(receipt_root)
    cohort = _ready_cohort()
    monkeypatch.setattr(
        admission_chain.multisource,
        "prevalidate_cohort",
        lambda _receipts, **_kwargs: cohort,
    )
    result = admission_chain.build_bundle(
        receipt_root=receipt_root,
        receipt_manifest=manifest,
        admission_root=admission_root,
        now_utc=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )
    return Path(result["path"]), result


def test_provision_creates_only_fresh_owner_private_leaf_roots(tmp_path: Path) -> None:
    parent = _private(tmp_path / "trusted")
    receipt_root = parent / "receipts"
    admission_root = parent / "admission"

    provisioned = admission_chain.provision_roots(receipt_root, admission_root)

    assert {item["path"] for item in provisioned} == {
        str(receipt_root),
        str(admission_root),
    }
    assert receipt_root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(contracts.C10Error, match="fresh"):
        admission_chain.provision_roots(receipt_root, admission_root)


def test_bundle_is_offline_immutable_and_binds_manifest_cohort_and_current_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, result = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text())

    assert result["cohort_sha256"] == bundle["cohort"]["cohort_sha256"]
    assert bundle["no_write"] == admission.NO_WRITE
    assert bundle["receipt_manifest_sha256"] == contracts.sha256(
        json.loads((tmp_path / "receipts" / "manifest.json").read_text())
    )
    assert set(bundle["adapter_implementation_sha256"]) == {
        source["key"] for source in policy.load_policy()["sources"]
    }
    assert bundle_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(contracts.C10Error, match="already exists"):
        admission_chain.build_bundle(
            receipt_root=tmp_path / "receipts",
            receipt_manifest=tmp_path / "receipts" / "manifest.json",
            admission_root=tmp_path / "admission",
            now_utc=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("failure", ["partial", "stale", "tampered"])
def test_partial_stale_or_tampered_receipts_publish_no_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    receipt_root = _private(tmp_path / "receipts")
    admission_root = _private(tmp_path / "admission")
    manifest = _manifest(receipt_root)

    def reject(_receipts: object, **_kwargs: object) -> dict[str, object]:
        raise admission_chain.multisource.MultisourceError(f"{failure} receipt")

    monkeypatch.setattr(admission_chain.multisource, "prevalidate_cohort", reject)
    with pytest.raises(contracts.C10Error, match=failure):
        admission_chain.build_bundle(
            receipt_root=receipt_root,
            receipt_manifest=manifest,
            admission_root=admission_root,
            now_utc=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )
    assert list(admission_root.iterdir()) == []


def test_real_partial_manifest_fails_before_bundle_publication(tmp_path: Path) -> None:
    receipt_root = _private(tmp_path / "receipts")
    admission_root = _private(tmp_path / "admission")
    manifest = _manifest(receipt_root)

    with pytest.raises(contracts.C10Error, match="partial"):
        admission_chain.build_bundle(
            receipt_root=receipt_root,
            receipt_manifest=manifest,
            admission_root=admission_root,
            now_utc=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )

    assert list(admission_root.iterdir()) == []


def test_tampered_sealed_bundle_cannot_render_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, _ = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_path.read_text())
    bundle["cohort"]["aggregate"] = {"state": "ready_for_review", "tampered": True}
    _write_private(bundle_path, bundle)

    with pytest.raises(contracts.C10Error, match="digest"):
        admission_chain.render_authority(bundle_path)


def test_tampered_receipt_manifest_cannot_render_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, _ = _bundle(tmp_path, monkeypatch)
    _write_private(
        tmp_path / "receipts" / "manifest.json",
        {
            "schema_version": 1,
            "kind": "cre_capacity_multisource_v1_receipts",
            "receipt_root": str(tmp_path / "receipts"),
            "receipts": [{"tampered": True}],
        },
    )

    with pytest.raises(contracts.C10Error, match="manifest digest"):
        admission_chain.render_authority(bundle_path)


def test_sealed_writer_retries_a_short_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private(tmp_path / "admission")
    original_write = admission_chain.os.write
    calls = 0

    def short_write(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, data[:1])
        return original_write(fd, data)

    monkeypatch.setattr(admission_chain.os, "write", short_write)
    with admission_chain._open_private_root(root, "admission root") as descriptor:
        path = admission_chain._write_private_json(
            descriptor, "short-write.json", {"value": "sealed"}
        )

    assert calls > 1
    assert json.loads(path.read_text()) == {"value": "sealed"}


def test_failed_sealed_write_removes_its_unpublished_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private(tmp_path / "admission")

    def failed_write(_fd: int, _data: bytes) -> int:
        raise OSError("disk failure")

    monkeypatch.setattr(admission_chain.os, "write", failed_write)
    with (
        admission_chain._open_private_root(root, "admission root") as descriptor,
        pytest.raises(contracts.C10Error, match="durably publish"),
    ):
        admission_chain._write_private_json(
            descriptor, "failed-write.json", {"value": "sealed"}
        )

    assert not (root / "failed-write.json").exists()


def test_unreviewed_candidate_registry_cannot_render_an_authority_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, _ = _bundle(tmp_path, monkeypatch)

    with pytest.raises(contracts.C10Error, match="not independently reviewed"):
        admission_chain.render_authority(bundle_path)


def test_authority_digest_pin_cannot_promote_an_unreviewed_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = {
        source["key"]: admission_chain.repository_implementation_sha256(source["key"])
        for source in policy.load_policy()["sources"]
    }
    monkeypatch.setattr(
        adapters,
        "load_authority",
        lambda: {"approved_adapters": approved},
    )

    with pytest.raises(contracts.C10Error, match="independently reviewed"):
        adapters.verified_registry(policy.load_policy(), adapters.candidate_registry())


def test_collect_panel_seals_one_non_eligible_outcome_for_every_current_descriptor(
    tmp_path: Path,
) -> None:
    receipt_root = _private(tmp_path / "receipts")

    result = admission_chain.collect_panel(receipt_root=receipt_root)
    panel = json.loads(Path(result["path"]).read_text())

    assert len(panel["outcomes"]) == 20
    assert {outcome["source_key"] for outcome in panel["outcomes"]} == {
        source["key"] for source in policy.load_policy()["sources"]
    }
    assert {outcome["state"] for outcome in panel["outcomes"]} == {"blocked"}
    assert all(outcome["eligible"] is False for outcome in panel["outcomes"])
    assert panel["no_write"] == admission.NO_WRITE


def test_collect_panel_rejects_partial_or_tampered_descriptor_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root = _private(tmp_path / "receipts")
    original = admission_chain.candidate_registry
    partial = original()
    partial.pop(next(iter(partial)))
    monkeypatch.setattr(admission_chain, "candidate_registry", lambda: partial)

    with pytest.raises(contracts.C10Error, match="exactly match"):
        admission_chain.collect_panel(receipt_root=receipt_root)

    class Tampered:
        key = "tampered"
        fully_verified = False

    tampered = original()
    tampered[next(iter(tampered))] = Tampered()
    monkeypatch.setattr(admission_chain, "candidate_registry", lambda: tampered)
    with pytest.raises(contracts.C10Error, match="key"):
        admission_chain.collect_panel(receipt_root=receipt_root)
