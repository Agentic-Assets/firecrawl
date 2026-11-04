import base64
import copy
import hashlib
import json

import pytest
from source_evidence import automatic, events, native_pages
from source_evidence import transwestern_pages as tw


def entry(value, tx="sale", bucket="Sale", occurrence=0):
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    receipt = {"sourceKey": "transwestern", "transaction": tx, "bucket": bucket, "occurrence": occurrence,
               "capturedAt": "2026-10-29T12:00:00Z", "freshRequested": False,
               "bodySha256": hashlib.sha256(body).hexdigest(), "bodyBytes": len(body)}
    data = {"schema": "cre-transwestern-parsed-page/v1", "receipt": receipt,
            "responseBodyBase64": base64.b64encode(body).decode(), "provenance": tw.PROVENANCE}
    raw = (json.dumps(data, separators=(",", ":")) + "\n").encode()
    return raw, f"page-{tx}-{occurrence}-{hashlib.sha256(raw).hexdigest()}.json"


@pytest.mark.parametrize("value", [[{"id": 1}, {"id": 1}, None, False, 0, ""], {"unknown": [None, 1]}, "partial"])
def test_reconstruct_and_plan_preserve_unqualified_values(value):
    raw, name = entry(value)
    observation, error = tw.decode_entry(raw, name, {"attempt": 1})
    assert error is None
    assert tw.reconstruct(observation) == value
    assert observation["arrayPointer"] == ("" if isinstance(value, list) else None)
    plan = events.plan(observation, source_key="transwestern")
    events.validate_plan(plan)
    assert len(plan["events"]) == (len(value) + 1 if isinstance(value, list) else 1)
    with pytest.raises(ValueError):
        events.plan(observation, source_key="cbre")
    with pytest.raises(ValueError):
        events.plan(observation, source_key="transwestern", external_id="claim")


def test_binding_and_tamper_refuse():
    raw, name = entry([1])
    with pytest.raises(ValueError): tw.decode_entry(raw, name + "x", {})
    value = json.loads(raw); value["receipt"]["bucket"] = "Lease"
    forged = json.dumps(value).encode()
    with pytest.raises(ValueError): tw.decode_entry(forged, "page-sale-0-" + hashlib.sha256(forged).hexdigest() + ".json", {})
    value = json.loads(raw); value["receipt"]["bodyBytes"] += 1
    forged = json.dumps(value).encode()
    with pytest.raises(ValueError): tw.decode_entry(forged, "page-sale-0-" + hashlib.sha256(forged).hexdigest() + ".json", {})
    observation, _ = tw.decode_entry(raw, name, {})
    observation["records"][0] = 2
    with pytest.raises(ValueError): events.validate_plan({**events.plan(tw.decode_entry(raw, name, {})[0], source_key="transwestern"), "events": []})
    assert tw.reconstruct(observation) == [2]  # Receipt is an assertion until sealed, not source authentication.


def test_numeric_witness_and_credential_gate():
    raw, name = entry([{"number": 1e-200, "text": "unchanged"}])
    observation, _ = tw.decode_entry(raw, name, {})
    tw.validate(observation)
    bad = copy.deepcopy(observation); bad["numericLiterals"] = {"/0/number": "not-a-number"}
    with pytest.raises(ValueError): tw.validate(bad)
    raw, name = entry([{"api_key": "synthetic-test-secret"}])
    observation, _ = tw.decode_entry(raw, name, {})
    with pytest.raises(ValueError): automatic._refuse_credential_fields(observation)


def test_format_and_environment_are_source_scoped(tmp_path):
    assert native_pages.format_supported("transwestern", tw.FORMAT)
    assert not native_pages.format_supported("cbre", tw.FORMAT)
    env = {"CRE_TRANSWESTERN_NATIVE_CAPTURE_DIR": "stale"}
    native_pages.configure_worker_env(env, tmp_path, "cbre", {}, {})
    assert not env
    attempt = {"number": 1, "native_source_evidence": {"directory": "source-evidence/native-transwestern-attempt-1"}}
    config = {"native_source_formats": {"transwestern": tw.FORMAT}, "max_native_pages": 2}
    native_pages.configure_worker_env(env, tmp_path, "transwestern", attempt, config)
    assert env["CRE_TRANSWESTERN_NATIVE_CAPTURE_SINGLE_WRITER"] == "1"


def test_settlement_without_canonical_artifact_and_content_refusal(tmp_path):
    import cre_checkpoint_refresh as refresh
    from test_automatic import setup_policy
    sha = "a" * 40
    config, policy, path = setup_policy(tmp_path, sources=("transwestern",), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={"transwestern": tw.FORMAT}, max_native_pages=4)
    raw_policy = events.encoded(policy); path.write_bytes(raw_policy)
    config = automatic.configuration(str(path), events.sha256(raw_policy), config["env_file"],
                                     config["target_sha256"], sha, ["transwestern"])
    run = tmp_path / "run"; run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=sha, git_dirty=False, sources=("transwestern",),
                                    page_cap=4, concurrency=1, automatic_source_evidence=config)
    attempt = {"number": 1, "source_evidence": {"state": "no_artifact"}}
    manifest["sources"]["transwestern"]["attempts"].append(attempt)
    directory = native_pages.mark(run, manifest, "transwestern", attempt)
    raw, name = entry([{"partial": True}, {"id": None}, None])
    page = directory / name; page.write_bytes(raw); page.chmod(0o600)
    native_pages.settle(run, manifest, "transwestern", attempt, lambda *_: None)
    state = attempt["native_source_evidence"]
    assert state["state"] == "settled" and attempt["source_evidence"]["state"] == "no_artifact"
    plan = native_pages.prepare_job(run, manifest, "transwestern", attempt, state["pages"][0])
    assert len(plan["events"]) == 4
    events.validate_plan(plan); events.validate_content_policy(plan)
    page.write_bytes(raw + b" ")
    with pytest.raises(ValueError): native_pages.prepare_job(run, manifest, "transwestern", attempt, state["pages"][0])


def test_scanner_refuses_sensitive_data_before_settlement(tmp_path):
    raw, name = entry([{"api_key": "synthetic-test-secret"}])
    observation, _ = tw.decode_entry(raw, name, {})
    with pytest.raises(ValueError): automatic._refuse_credential_fields(observation)


def checkpoint_fixture(tmp_path):
    import cre_checkpoint_refresh as refresh
    from test_automatic import setup_policy

    sha = "a" * 40
    config, policy, path = setup_policy(tmp_path, sources=("transwestern",), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={"transwestern": tw.FORMAT}, max_native_pages=4)
    raw = events.encoded(policy); path.write_bytes(raw)
    config = automatic.configuration(str(path), events.sha256(raw), config["env_file"],
                                     config["target_sha256"], sha, ["transwestern"])
    run = tmp_path / "checkpoint"; run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=sha, git_dirty=False, sources=("transwestern",),
                                    page_cap=4, concurrency=1, automatic_source_evidence=config)
    return refresh, run, manifest, sha


def test_nonzero_owned_worker_settles_earlier_bucket(tmp_path, monkeypatch):
    from pathlib import Path

    refresh, run, manifest, sha = checkpoint_fixture(tmp_path)
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *args, **kwargs: calls.append(args))
    def worker(_argv, _log, *, env):
        raw, name = entry([{"partial": True}, None])
        page = Path(env["CRE_TRANSWESTERN_NATIVE_CAPTURE_DIR"]) / name
        page.write_bytes(raw); page.chmod(0o600)
        return 1
    monkeypatch.setattr(refresh, "run_command", worker)
    assert refresh.collect_source(run, manifest, "transwestern", transactions=("sale", "lease"),
                                  page_cap=4, concurrency=1, attempts_this_run=1) is None
    attempt = manifest["sources"]["transwestern"]["attempts"][0]
    assert attempt["rc"] == 1 and attempt["source_evidence"]["state"] == "no_artifact"
    assert attempt["native_source_evidence"]["pages"][0]["state"] == "appended" and len(calls) == 1


def test_unknown_local_worker_settlement_does_not_scan(tmp_path, monkeypatch):
    from pathlib import Path

    refresh, run, manifest, _ = checkpoint_fixture(tmp_path)
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *_: None)
    def interrupted(_argv, _log, *, env):
        raw, name = entry([{"partial": True}])
        page = Path(env["CRE_TRANSWESTERN_NATIVE_CAPTURE_DIR"]) / name
        page.write_bytes(raw); page.chmod(0o600)
        raise KeyboardInterrupt()
    monkeypatch.setattr(refresh, "run_command", interrupted)
    monkeypatch.setattr(native_pages, "settle", lambda *_: pytest.fail("unsettled worker was scanned"))
    with pytest.raises(KeyboardInterrupt):
        refresh.collect_source(run, manifest, "transwestern", transactions=("sale", "lease"),
                               page_cap=4, concurrency=1, attempts_this_run=1)
    saved = json.loads((run / "manifest.json").read_bytes())
    state = saved["sources"]["transwestern"]["attempts"][0]["native_source_evidence"]
    assert state["state"] == "awaiting_artifact" and state["pages"] == []
