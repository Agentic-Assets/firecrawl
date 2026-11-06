"""Serial no-write holds rederive the sealed source proof before peer progress."""

import copy
import json

import pytest
import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
from daum import serial_hold
from test_daum_verified_subset import admit, complete, subset  # noqa: F401


@pytest.fixture
def held_source(subset, monkeypatch):
    import test_essex_current_inventory as helpers

    root, path, data, manifest, report = subset
    for entry in data["sources"]:
        for key in ("listings", "inventoryObserved", "detailObserved"):
            entry["freshness"][key] = entry["listingsCollected"]
    (root / "sources").mkdir(exist_ok=True)
    artifact = root / "sources" / "daum-commercial.json"
    helpers.refresh(artifact, data, manifest)
    manifest["sources"]["daum-commercial"]["artifact"]["path"] = "sources/daum-commercial.json"
    prepared = (root, artifact, data, manifest, report)
    report["generated_for_artifact"] = str(artifact)
    original = copy.deepcopy(report)
    admit(prepared)
    report.clear()
    report.update(original)
    checkpoint = manifest["sources"]["daum-commercial"]
    for item in checkpoint["daum_identity_probe"]["partition"]:
        item["conflicted"] = True
    (root / "gates").mkdir(exist_ok=True)
    gate = root / "gates" / "daum-commercial.json"
    gate.write_text(json.dumps(report))
    code = manifest["collector_git_sha"]
    monkeypatch.setattr(refresh, "git_identity", lambda: (code, False))
    return root, manifest, gate


@pytest.mark.parametrize("fault", ["none", "artifact", "gate", "target", "generation", "probe", "ingest", "code"])
def test_serial_hold_rederives_exact_no_write_source_evidence(held_source, monkeypatch, fault):
    root, manifest, gate = held_source
    checkpoint = manifest["sources"]["daum-commercial"]
    checkpoint["serial_all_held"] = serial_hold.binding(root, manifest)
    checkpoint.update(state="gate_blocked", partial_probe_hold="all_emitted_target_held")
    if fault == "artifact": (root / checkpoint["artifact"]["path"]).write_text("{}")
    if fault == "gate": gate.write_text("{}")
    if fault == "target": manifest["preflight"]["database_target"]["value"] = "c" * 64
    if fault == "generation": manifest["run_id"] = "different"
    if fault == "probe": checkpoint["daum_identity_probe"]["partition"][0]["conflicted"] = "invalid"
    if fault == "ingest": checkpoint["ingest"] = {"rc": 0}
    if fault == "code": monkeypatch.setattr(refresh, "git_identity", lambda: ("f" * 40, False))
    assert serial_hold.verified(root, manifest) is (fault == "none")


@pytest.mark.parametrize("case", ["local", "tampered", "unsettled", "wrong_target", "prior_ingest", "malformed_probe"])
def test_serial_singleton_classifies_only_rederived_settled_hold_locally(held_source, tmp_path, monkeypatch, case):
    root, child, gate = held_source
    checkpoint = child["sources"]["daum-commercial"]
    checkpoint["serial_all_held"] = serial_hold.binding(root, child)
    checkpoint.update(state="gate_blocked", partial_probe_hold="all_emitted_target_held")
    child.update(status="failed", error="all emitted DAUM observations held by target checks")
    if case == "tampered": gate.write_text("{}")
    target = child["preflight"]["database_target"]
    config = series.series_config(sources=("daum-commercial",), page_cap=400, concurrency=1,
                                  attempts_per_source=1, max_resume_age_hours=24,
                                  max_host_cpu_percent=90, cpu_sustain_seconds=30,
                                  cpu_sample_seconds=2, nice=10, admit_daum_verified_subset=True)
    parent = series.new_manifest(tmp_path, git_sha=child["collector_git_sha"], config=config, database_target=target)
    if case == "wrong_target":
        child["preflight"]["database_target"] = {"algorithm": "sha256", "value": "c" * 64}
    if case == "prior_ingest": checkpoint["ingest"] = {"rc": 0}
    if case == "malformed_probe": checkpoint["daum_identity_probe"]["partition"][0]["conflicted"] = "invalid"
    started = []
    outcomes = iter([refresh.DAUM_ALL_HELD_SETTLED_EXIT])
    children = iter([(root / "manifest.json", child)])
    class Process:
        def __init__(self, *_args, **_kwargs):
            self.pid = 4321
            self.rc = next(outcomes)
            started.append(self)
        def wait(self, timeout=None): return self.rc
        def poll(self): return self.rc
    monkeypatch.setattr(series.subprocess, "Popen", Process)
    monkeypatch.setattr(series, "_load_child_manifest", lambda *_args, **_kwargs: next(children))
    def settle(*_args):
        if case == "unsettled": raise refresh.ProcessCleanupUnknown("unknown")
    monkeypatch.setattr(refresh, "_require_completed_group_settlement", settle)
    rc = series.run_series(tmp_path, parent, env_file=None, retry_failed=False)
    if case == "local":
        assert rc == 2 and len(started) == 1
        assert parent["status"] == "complete_with_source_failures"
        assert parent["sources"]["daum-commercial"]["state"] == "failed_source"
    else:
        assert rc == 1 and len(started) == 1
        assert parent["sources"]["daum-commercial"]["state"] == "failed_global"


@pytest.mark.parametrize("case", ["settled", "missing_receipt", "unsettled", "tampered_gate", "wrong_target", "prefix_spoof"])
def test_stale_serial_hold_requires_exact_controlled_settlement(held_source, tmp_path, monkeypatch, case):
    import shutil
    from cre_series_control import publish_checkpoint_settlement

    old_root, child, _gate = held_source
    series_dir = tmp_path / "series"
    runs = series_dir / "runs"
    runs.mkdir(parents=True)
    root = runs / old_root.name
    shutil.move(str(old_root), str(root))
    gate = root / "gates" / "daum-commercial.json"
    report = json.loads(gate.read_text())
    report["generated_for_artifact"] = str(root / "sources" / "daum-commercial.json")
    gate.write_text(json.dumps(report))
    config = series.series_config(sources=("daum-commercial",), page_cap=400, concurrency=1,
                                  attempts_per_source=1, max_resume_age_hours=24,
                                  max_host_cpu_percent=90, cpu_sustain_seconds=30,
                                  cpu_sample_seconds=2, nice=10, admit_daum_verified_subset=True)
    child["config"] = series._expected_child_config("daum-commercial", config)
    checkpoint = child["sources"]["daum-commercial"]
    checkpoint["serial_all_held"] = serial_hold.binding(root, child)
    checkpoint.update(state="gate_blocked", partial_probe_hold="all_emitted_target_held")
    child.update(status="failed", finished_at=refresh.utc_now(),
                 error="all emitted DAUM observations held by target checks")
    if case == "prefix_spoof":
        child["error"] = "RefreshError: source checkpoints remain incomplete: daum-commercial"
    (root / "manifest.json").write_text(json.dumps(child))
    target = copy.deepcopy(child["preflight"]["database_target"])
    parent = series.new_manifest(series_dir, git_sha=child["collector_git_sha"], config=config, database_target=target)
    invocation, child_id = "a" * 32, "b" * 32
    parent["latest_invocation_control"] = {"invocation_id": invocation}
    outer = parent["sources"]["daum-commercial"]
    outer.update(state="running", checkpoint_run=f"runs/{root.name}",
                 attempts=[{"started_at": child["started_at"], "series_control_child_id": child_id}])
    if case not in {"missing_receipt", "prefix_spoof"}:
        publish_checkpoint_settlement(root, invocation, child, child_invocation_id=child_id,
                                      guards_stopped=True, nested_cleanup_confirmed=True,
                                      terminal_evidence_saved=True)
    if case == "tampered_gate": gate.write_text("{}")
    if case == "wrong_target":
        child["preflight"]["database_target"]["value"] = "c" * 64
        (root / "manifest.json").write_text(json.dumps(child))
    monkeypatch.setattr(refresh, "_group_alive", lambda *_: case == "unsettled")
    if case == "wrong_target":
        with pytest.raises(series.SeriesError):
            series.reconcile_stale_running_sources(series_dir, parent)
    else:
        series.reconcile_stale_running_sources(series_dir, parent)
        assert outer["state"] == ("failed_source" if case == "settled" else "failed_global")
