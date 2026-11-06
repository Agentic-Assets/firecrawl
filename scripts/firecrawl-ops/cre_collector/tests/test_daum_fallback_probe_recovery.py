"""DAUM source-local preseal holds require settled, same-target evidence."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cre_checkpoint_refresh as refresh
import cre_daum_fallback_partition as daum
import cre_ingest as ingest
import cre_refresh_pipeline as pipeline
import cre_validate
from test_cre_pipeline_preseal_gate import closed_pipeline as _closed_pipeline
from test_daum_current_inventory import produce

closed_pipeline = _closed_pipeline


@pytest.fixture(scope="module")
def artifact_bytes(tmp_path_factory):
    folder = tmp_path_factory.mktemp("daum-probe-producer")
    artifact, _, _, _ = produce(
        folder, total=101, empty_title=True, corroborated_title=True,
        subset_policy="tenure-and-empty-title-v2",
    )
    return json.dumps(artifact).encode()


@pytest.fixture
def probe_case(tmp_path, monkeypatch, artifact_bytes):
    path = tmp_path / "artifact.json"
    path.write_bytes(artifact_bytes)
    sha = hashlib.sha256(artifact_bytes).hexdigest()
    target = "b" * 64
    producer = "a" * 40
    monkeypatch.setattr(refresh, "git_identity", lambda: (producer, False))
    monkeypatch.setattr(ingest, "load_db_url", lambda _: ("postgresql://fixture@localhost/test", None))
    monkeypatch.setattr(ingest, "database_target_fingerprint_from_url", lambda _: {"value": target})
    monkeypatch.setattr(ingest, "find_psql", lambda: "fixture-psql")
    return path, sha, producer, target


@pytest.mark.parametrize("failure", ["timeout", "source_sql", "malformed_output"])
def test_settled_source_probe_failure_needs_same_target_health(probe_case, monkeypatch, failure):
    path, sha, producer, target = probe_case
    calls = []

    def query(_psql, _url, script, timeout):
        calls.append(script)
        if "SELECT 1 AS healthy" in script:
            return SimpleNamespace(returncode=0, stdout="healthy\n1\n", stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fixture-psql", timeout)
        if failure == "source_sql":
            return SimpleNamespace(returncode=3, stdout="", stderr="ERROR:  22023: synthetic source")
        return SimpleNamespace(returncode=0, stdout="invalid\n", stderr="")

    monkeypatch.setattr(cre_validate, "_run_psql_script", query)
    with pytest.raises(daum.DaumSourceProbeRefusal):
        daum.run_identity_probe(path, sha, "generation", producer, target, None)
    assert sum("SELECT 1 AS healthy" in call for call in calls) == 1


@pytest.mark.parametrize("fault", ["shared_outage", "unhealthy", "target_change", "artifact_change"])
def test_uncertain_target_or_changed_owner_is_never_demoted(probe_case, monkeypatch, fault):
    path, sha, producer, target = probe_case
    calls = []

    def query(_psql, _url, script, timeout):
        calls.append(script)
        if fault == "shared_outage":
            return SimpleNamespace(returncode=2, stdout="", stderr="connection failed")
        if "SELECT 1 AS healthy" in script:
            if fault == "artifact_change":
                path.write_bytes(path.read_bytes() + b" ")
            return SimpleNamespace(returncode=1 if fault == "unhealthy" else 0,
                                   stdout="healthy\n1\n", stderr="")
        raise subprocess.TimeoutExpired("fixture-psql", timeout)

    monkeypatch.setattr(cre_validate, "_run_psql_script", query)
    if fault == "target_change":
        count = iter([{"value": target}, {"value": "c" * 64}])
        monkeypatch.setattr(ingest, "database_target_fingerprint_from_url", lambda _: next(count))
    with pytest.raises((RuntimeError, ValueError)) as exc:
        daum.run_identity_probe(path, sha, "generation", producer, target, None)
    assert not isinstance(exc.value, daum.DaumSourceProbeRefusal)


def test_real_timed_out_probe_child_is_reaped_before_source_hold(probe_case, tmp_path, monkeypatch):
    path, sha, producer, target = probe_case
    fake = tmp_path / "fake-psql"
    pid = tmp_path / "pid"
    fake.write_text(
        "#!/usr/bin/python3\nimport os,sys,time\nfrom pathlib import Path\n"
        "query=Path(sys.argv[-1]).read_text()\n"
        "if 'SELECT 1 AS healthy' in query:\n print('healthy'); print('1'); sys.exit(0)\n"
        f"Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(5)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(ingest, "find_psql", lambda: str(fake))
    real = cre_validate._run_psql_script

    def bounded(psql, url, script, timeout):
        return real(psql, url, script, timeout=0.25 if timeout == 45 else timeout)

    monkeypatch.setattr(cre_validate, "_run_psql_script", bounded)
    with pytest.raises(daum.DaumSourceProbeRefusal):
        daum.run_identity_probe(path, sha, "generation", producer, target, None)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.parametrize("closed_pipeline", [("daum-commercial", "svn")], indirect=True)
def test_settled_daum_probe_hold_keeps_verified_peer_running(closed_pipeline, monkeypatch):
    case = closed_pipeline
    original_compute = pipeline.run_artifact_compute
    original_gate = refresh.gate_source

    def compute(run_dir, kind, payload):
        if kind == "validate" and payload["source"] == "daum-commercial":
            raw = Path(payload["path"]).read_bytes()
            return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        return original_compute(run_dir, kind, payload)

    def gate(run_dir, manifest, source, artifact, env_file):
        if source == "daum-commercial":
            raise refresh.DaumPartialProbeHold("settled source-specific refusal")
        return original_gate(run_dir, manifest, source, artifact, env_file)

    monkeypatch.setattr(pipeline, "run_artifact_compute", compute)
    monkeypatch.setattr(refresh, "gate_source", gate)
    assert case.run() == ["daum-commercial"]
    assert case.manifest["sources"]["daum-commercial"]["state"] == "gate_blocked"
    assert ("write", "daum-commercial") not in case.events
    assert ("write", "svn") in case.events
    assert case.manifest["sources"]["svn"]["readback"]["ok"] is True


def test_gate_translates_only_typed_daum_source_refusal(tmp_path, monkeypatch, artifact_bytes):
    import test_essex_current_inventory as helpers
    import daum.identity_sql as identity

    data = json.loads(artifact_bytes)
    generation = data["runMeta"]["freshness"]["generationId"]
    started = data["runMeta"]["freshness"]["generationStartedAt"]
    monkeypatch.setattr(helpers, "SOURCE", "daum-commercial")
    prepared = helpers.setup(tmp_path, (data, "", started, generation))
    run, path, _, manifest, _ = prepared
    import cre_current_inventory_admission as admission
    manifest["config"]["current_inventory_admission"] = admission.subset_configuration("tenure-and-empty-title-v2")

    def gate_command(argv, _log, env=None):
        output = Path(argv[argv.index("--out") + 1])
        output.write_text(json.dumps({"per_source": {"daum-commercial": {}}}))
        return 0

    monkeypatch.setattr(refresh, "run_command", gate_command)
    monkeypatch.setattr(identity, "run_cohort_probe", lambda *_args: {"fixture": "settled"})
    monkeypatch.setattr(daum, "run_identity_probe", lambda *_args: (_ for _ in ()).throw(
        daum.DaumSourceProbeRefusal("source only")))
    with pytest.raises(refresh.DaumPartialProbeHold):
        refresh.gate_source(run, manifest, "daum-commercial", path, None)
    monkeypatch.setattr(daum, "run_identity_probe", lambda *_args: (_ for _ in ()).throw(
        RuntimeError("shared target unknown")))
    with pytest.raises(refresh.GlobalStageError):
        refresh.gate_source(run, manifest, "daum-commercial", path, None)


@pytest.mark.parametrize("closed_pipeline", [("daum-commercial", "svn")], indirect=True)
def test_daum_probe_hold_cannot_hide_target_change(closed_pipeline, monkeypatch):
    case = closed_pipeline
    original_compute = pipeline.run_artifact_compute

    def compute(run_dir, kind, payload):
        if kind == "validate" and payload["source"] == "daum-commercial":
            raw = Path(payload["path"]).read_bytes()
            return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        return original_compute(run_dir, kind, payload)

    def gate(_run_dir, manifest, source, _artifact, _env_file):
        assert source == "daum-commercial"
        manifest["preflight"]["database_target"]["value"] = "c" * 64
        raise refresh.DaumPartialProbeHold("settled source-specific refusal")

    monkeypatch.setattr(pipeline, "run_artifact_compute", compute)
    monkeypatch.setattr(refresh, "gate_source", gate)
    with pytest.raises(refresh.GlobalStageError, match="target changed"):
        case.run()
    assert not any(event[0] == "write" for event in case.events)
