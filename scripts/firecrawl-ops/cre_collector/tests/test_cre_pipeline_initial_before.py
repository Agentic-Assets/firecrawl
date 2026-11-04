"""Same-owner runtime handoff avoids the duplicate first-unit DB preflight."""

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from test_cre_checkpoint_refresh import lifecycle_schema_contract_validation
from test_cre_pipeline_units import owner


@pytest.fixture
def initial(tmp_path, monkeypatch):
    root, manifest = owner(tmp_path)
    manifest["pipeline"]["commit_sequence"] = 0
    before = root / "pre-validation.json"
    before.write_text(json.dumps(lifecycle_schema_contract_validation()))
    manifest["preflight"].update(
        validation_path="pre-validation.json",
        validation_sha256=refresh.sha256_file(before),
        validation_profile=refresh.CHECKPOINT_PREFLIGHT_PROFILE,
        validation_audit_scope=refresh.VALIDATION_AUDIT_SCOPES[
            refresh.CHECKPOINT_PREFLIGHT_PROFILE
        ],
        shared_lock=str(root / "lock"),
    )
    monkeypatch.setattr(refresh, "_lock_owner", lambda path: os.getpid())
    monkeypatch.setattr(refresh, "_lock_lease", lambda path: "test-lease")
    token = pipeline._OWNER_LEASE.set("test-lease")
    try:
        yield root, manifest, before
    finally:
        pipeline._OWNER_LEASE.reset(token)


def binding(root, manifest):
    return pipeline.initial_preflight_binding(
        root, manifest, fresh=True, resumed=False, owner_check=lambda: None
    )


def test_initial_qualified_runtime_preflight_is_copied_without_another_query(
    initial, monkeypatch
):
    root, manifest, source = initial
    handoff = binding(root, manifest)
    monkeypatch.setattr(
        refresh, "run_command", lambda *a, **k: pytest.fail("duplicate preflight query")
    )
    unit = {"unit_id": "unit-0001"}
    before = pipeline.unit_before_report(
        root, manifest, unit, None, None, initial_before=handoff
    )
    assert before == root / "pipeline" / "unit-0001" / "before.json"
    assert before.read_bytes() == source.read_bytes()
    assert unit["before_reused"] is True
    assert unit["before_reuse_kind"] == "initial_preflight"
    assert unit["before_source_path"] == "pre-validation.json"


@pytest.mark.parametrize(
    "field,value",
    [
        ("target", "e" * 64),
        ("owner_pid", -1),
        ("owner_lease", "foreign"),
        ("run_id", "foreign"),
    ],
)
def test_unbound_runtime_handoff_uses_fresh_query(initial, monkeypatch, field, value):
    root, manifest, _ = initial
    handoff = replace(binding(root, manifest), **{field: value})
    calls = []

    def query(argv, log, **kwargs):
        calls.append(argv)
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(lifecycle_schema_contract_validation()))
        return 0

    monkeypatch.setattr(refresh, "run_command", query)
    unit = {"unit_id": "unit-0001"}
    pipeline.unit_before_report(
        root, manifest, unit, None, None, initial_before=handoff
    )
    assert len(calls) == 1
    assert unit["before_reused"] is False


def test_nonzero_commit_sequence_cannot_reuse_initial_report(initial, monkeypatch):
    root, manifest, _ = initial
    handoff = binding(root, manifest)
    manifest["pipeline"]["commit_sequence"] = 1
    calls = []

    def query(argv, log, **kwargs):
        calls.append(argv)
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(lifecycle_schema_contract_validation()))
        return 0

    monkeypatch.setattr(refresh, "run_command", query)
    pipeline.unit_before_report(
        root, manifest, {"unit_id": "unit-0002"}, None, None, initial_before=handoff
    )
    assert len(calls) == 1


def test_changed_initial_hash_is_an_integrity_refusal(initial, monkeypatch):
    root, manifest, source = initial
    handoff = binding(root, manifest)
    source.write_text("{}")
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda *a, **k: pytest.fail("corrupt evidence silently replaced"),
    )
    with pytest.raises(refresh.GlobalStageError, match="changed"):
        pipeline.unit_before_report(
            root, manifest, {"unit_id": "unit-0001"}, None, None, initial_before=handoff
        )


@pytest.mark.parametrize("fresh,resumed", [(False, False), (True, True)])
def test_resumed_or_persisted_preflight_never_creates_runtime_handoff(
    initial, fresh, resumed
):
    root, manifest, _ = initial
    assert (
        pipeline.initial_preflight_binding(
            root, manifest, fresh=fresh, resumed=resumed, owner_check=lambda: None
        )
        is None
    )
