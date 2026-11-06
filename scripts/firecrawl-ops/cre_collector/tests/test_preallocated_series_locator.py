"""Preallocated series locator and terminal receipt; no provider or database calls."""
import json
from pathlib import Path

import pytest

import cre_checkpoint_series as series
import cre_series_control as control


@pytest.mark.parametrize("value", [
    "../outside", "2026-11-06T010203Z", "2026-02-30T010203Z-012345abcdef",
    "2026-11-06T010203Z-012345ABCDEF", "2026-11-06T010203Z-012345abcdef/extra",
])
def test_invalid_fresh_locator_refused_before_credentials(tmp_path, monkeypatch, value):
    monkeypatch.setattr(series, "database_target_fingerprint", lambda *_: pytest.fail("credentials touched"))
    with pytest.raises(SystemExit):
        series.main(["--fresh-series-id", value, "--out-root", str(tmp_path / "absent")])
    assert not (tmp_path / "absent").exists()


def test_fresh_locator_cannot_be_combined_with_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(series, "database_target_fingerprint", lambda *_: pytest.fail("credentials touched"))
    with pytest.raises(SystemExit):
        series.main(["--fresh-series-id", "2026-11-06T010203Z-012345abcdef",
                     "--resume", str(tmp_path / "old")])
    assert not (tmp_path / "old").exists()


def test_preallocated_controlled_series_receipt_is_bound_and_never_authorizes_continuation(
    tmp_path, monkeypatch, capsys
):
    identifier = "2026-11-06T010203Z-012345abcdef"
    target = {"algorithm": "sha256", "value": "b" * 64}
    monkeypatch.setattr(series, "__name__", "__main__")
    monkeypatch.setattr(series, "_CLI_CONTROL_CAPABILITY", object())
    monkeypatch.setattr(series, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(series, "admit_series_invocation", lambda *_: None)
    monkeypatch.setattr(series, "database_target_fingerprint", lambda *_: target)

    def work(_path, manifest, **_kwargs):
        manifest.update(status="complete", finished_at=series.utc_now())
        return 0

    monkeypatch.setattr(series, "run_series", work)
    args = ["--sources", "cbre", "--out-root", str(tmp_path), "--fresh-series-id", identifier,
            "--work-deadline-seconds", "30"]
    assert series.main(args) == 0
    handshake = json.loads(capsys.readouterr().out.splitlines()[0])
    assert handshake["series_dir"] == str(tmp_path / identifier)
    manifest = json.loads((tmp_path / identifier / "manifest.json").read_text())
    token = handshake["invocation_id"]
    result = control.verify_preallocated_receipt(tmp_path, identifier, token, expected_sha="a" * 40,
        expected_target=target, expected_config=manifest["config"])
    assert result == {"series_id": identifier, "invocation_id": token, "status": "complete",
                      "local_settlement": "not_started", "continuation_authorized": False}
    for sha, selected_target, config in [
        ("c" * 40, target, manifest["config"]),
        ("a" * 40, {"algorithm": "sha256", "value": "d" * 64}, manifest["config"]),
        ("a" * 40, target, {**manifest["config"], "sources": ["jll"]}),
    ]:
        with pytest.raises(control.SeriesControlError):
            control.verify_preallocated_receipt(tmp_path, identifier, token, expected_sha=sha,
                expected_target=selected_target, expected_config=config)
    with pytest.raises((OSError, control.SeriesControlError)):
        control.verify_preallocated_receipt(tmp_path, identifier, "f" * 32, expected_sha="a" * 40,
            expected_target=target, expected_config=manifest["config"])
    receipt_path = tmp_path / identifier / ".series-control" / (token + ".json")
    original_receipt = receipt_path.read_bytes()
    tampered = json.loads(original_receipt)
    tampered.update(status="cleanup_unknown", local_settlement="unknown")
    receipt_path.write_text(json.dumps(tampered))
    with pytest.raises(control.SeriesControlError):
        control.verify_preallocated_receipt(tmp_path, identifier, token, expected_sha="a" * 40,
            expected_target=target, expected_config=manifest["config"])
    receipt_path.write_bytes(original_receipt)
    before = (tmp_path / identifier / "manifest.json").read_bytes()
    with pytest.raises(series.SeriesError, match="already exists"):
        series.main(args)
    assert (tmp_path / identifier / "manifest.json").read_bytes() == before

    original_read_receipt = control.read_receipt
    def replace_manifest(*args):
        path = tmp_path / identifier / "manifest.json"
        replacement = path.with_name("replacement.json")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        return original_read_receipt(*args)

    with monkeypatch.context() as patch:
        patch.setattr(control, "read_receipt", replace_manifest)
        with pytest.raises(control.SeriesControlError, match="changed"):
            control.verify_preallocated_receipt(tmp_path, identifier, token, expected_sha="a" * 40,
                expected_target=target, expected_config=manifest["config"])


def test_receipt_status_does_not_allow_running_or_unknown(tmp_path, monkeypatch):
    identifier = "2026-11-06T010203Z-012345abcdef"
    directory = tmp_path / identifier
    directory.mkdir()
    target = {"algorithm": "sha256", "value": "b" * 64}
    manifest = {"series_id": identifier, "collector_git_sha": "a" * 40, "collector_git_dirty": False,
                "database_target": target, "config": {"sources": ["cbre"]}, "status": "running",
                "finished_at": None, "latest_invocation_control": {"invocation_id": "e" * 32,
                "receipt": ".series-control/" + "e" * 32 + ".json", "work_deadline_seconds": 30}}
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    directory_id = [directory.stat().st_dev, directory.stat().st_ino]
    monkeypatch.setattr(control, "read_receipt", lambda *_: {"stop_owner": {"series_directory_identity": directory_id},
        "status": "running", "local_settlement": "unknown", "work_deadline_seconds": 30, "finished_unix_ms": None})
    with pytest.raises(control.SeriesControlError):
        control.verify_preallocated_receipt(tmp_path, identifier, "e" * 32, expected_sha="a" * 40,
            expected_target=target, expected_config=manifest["config"])


def test_symlink_series_locator_is_rejected(tmp_path):
    identifier = "2026-11-06T010203Z-012345abcdef"
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / identifier).symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        control.verify_preallocated_receipt(tmp_path, identifier, "e" * 32, expected_sha="a" * 40,
            expected_target={}, expected_config={})
