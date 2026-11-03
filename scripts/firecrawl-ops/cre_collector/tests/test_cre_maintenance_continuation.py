"""Continuation uses actual checkout ancestry and unchanged image inputs."""

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_cre_code_handoff as fixtures

import cre_runtime_maintenance as maintenance
from cre_code_handoff_evidence import HandoffError

git_case = fixtures.git_case
git_command = fixtures.git_command


def api():
    return importlib.import_module("cre_runtime_maintenance_continuation")


def test_runtime_before_defaults_old_and_refuses_malformed_present():
    old = {"sha": "1" * 40, "tree": "2" * 40}
    assert api().runtime_before({"old": old}) == old
    for value in (None, {}, {"sha": "1" * 40, "tree": "bad"}):
        with pytest.raises(HandoffError):
            api().runtime_before({"old": old, "runtime_before": value})


def test_legacy_packet_cannot_smuggle_unused_continuation():
    old = {"sha": "1" * 40, "tree": "2" * 40}
    with pytest.raises(HandoffError, match="legacy maintenance"):
        api().validate_continuation({"old": old, "continuation": {}}, None, None, None)


@pytest.mark.parametrize("live", [False, True])
def test_continuation_calls_reviewed_stack_validator_and_live_boundary(monkeypatch, live, tmp_path):
    module = api()
    old = {"sha": "1" * 40, "tree": "2" * 40}
    before = {"sha": "3" * 40, "tree": "4" * 40}
    names = ("failed_release", "failure_manifest", "recovery_completed", "recovery_prepared",
             "recovery_expected_state", "current_stack", "current_stack_review")
    continuation = {**{name: name for name in names}, "failed_operation": "/operation"}
    packet = {"old": old, "runtime_before": before, "continuation": continuation}
    evidence = SimpleNamespace(inputs={name: {} for name in names}, obj=lambda name: {"role": name})
    calls = []
    monkeypatch.setattr(module, "validate_continuation_code", lambda *_: calls.append("code"))
    monkeypatch.setattr(module, "validate_continuation_predecessor", lambda *_: calls.append("predecessor"))
    stack = SimpleNamespace(validate_current_stack=lambda *_: calls.append("retained"),
                            validate_current_stack_live=lambda *args: calls.append(("live", args[-1])))
    monkeypatch.setitem(sys.modules, "cre_current_stack", stack)
    output = tmp_path / "capture"
    module.validate_continuation(packet, evidence, None, None, live=live, output=output if live else None)
    assert calls == ["code", "predecessor", ("live", output) if live else "retained"]


def test_fresh_stack_drift_refuses_before_lease(tmp_path, monkeypatch):
    import cre_code_handoff as operation

    current = datetime.now(UTC)
    release = tmp_path / "release"
    observation = tmp_path / "observation"
    authorization = tmp_path / "authorization"
    release.write_bytes(b"{}")
    observation.write_text(json.dumps({"observed_at": current.isoformat(), "qualified_at": current.isoformat()}))
    authorization.write_text(json.dumps({"authorized_at": current.isoformat(),
                                         "expires_at": (current + timedelta(seconds=299)).isoformat()}))
    args = SimpleNamespace(release=str(release), observation=str(observation), authorization=str(authorization),
                           output=str(tmp_path / "operation"))
    packet = {"new": {}, "expected_images": {}}
    monkeypatch.setattr(maintenance, "validate_maintenance_release", lambda *_: None)
    monkeypatch.setattr(maintenance, "validate_maintenance_authorization", lambda *_, **__: None)
    monkeypatch.setattr(maintenance, "validate_operation_paths", lambda *_: None)
    monkeypatch.setattr(operation, "now", lambda: current)
    monkeypatch.setattr(maintenance, "maintenance_readiness", lambda *_: pytest.fail("readiness reached"))
    def refuse(*_, **_kwargs):
        raise HandoffError("stack drift")
    monkeypatch.setattr(maintenance, "recheck_continuation_stack", refuse)
    checkpoint = SimpleNamespace(SharedLock=lambda *_, **__: pytest.fail("lease acquired"))
    git = SimpleNamespace(development=lambda _: None)
    with pytest.raises(HandoffError, match="stack drift"):
        maintenance.apply_maintenance(args, b"{}", packet, None, git, (checkpoint, None, None, None))
    assert not (tmp_path / "operation").exists()


@pytest.mark.parametrize("changed", [None, "apps/api/Dockerfile", "apps/playwright-service-ts/Dockerfile", "apps/nuq-postgres/Dockerfile", "docker-compose.yaml", "scripts/firecrawl-ops/cre_collector/package-lock.json"])
def test_real_git_continuation_requires_unchanged_image_and_dependency_inputs(git_case, changed):
    git, target, release, _, path = git_case
    source = target.parent / "source"
    before = release["selected"]
    (source / path).write_text("reviewed continuation\n")
    if changed:
        leaf = source / changed
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("changed\n")
    git_command(source, "add", ".")
    git_command(source, "commit", "-m", "continuation")
    new = {"sha": git.text(source, "rev-parse", "HEAD"), "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    packet = {"old": release["deployed"], "runtime_before": before, "native_before": before, "new": new,
              "expected_images": {}, "image_tags": {}, "install_scope": []}
    if changed:
        with pytest.raises(HandoffError):
            api().validate_continuation_code(packet, git, source)
    else:
        api().validate_continuation_code(packet, git, source)
        packet["runtime_before"] = {**before, "tree": release["deployed"]["tree"]}
        with pytest.raises(HandoffError):
            api().validate_continuation_code(packet, git, source)


@pytest.mark.parametrize("field,value", [("expected_images", {"api": "sha256:" + "a" * 64}),
                                          ("image_tags", {"api": "firecrawl-api:test"}),
                                          ("install_scope", ["collector"])])
def test_continuation_refuses_rebuild_or_dependency_install(git_case, field, value):
    git, target, release, _, path = git_case
    source = target.parent / "source"
    (source / path).write_text("continuation\n")
    git_command(source, "commit", "-am", "continuation")
    new = {"sha": git.text(source, "rev-parse", "HEAD"), "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    packet = {"old": release["deployed"], "runtime_before": release["selected"],
              "native_before": release["selected"], "new": new,
              "expected_images": {}, "image_tags": {}, "install_scope": []}
    packet[field] = value
    with pytest.raises(HandoffError):
        api().validate_continuation_code(packet, git, source)
