import base64
import hashlib
import json
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest
from source_evidence import acquisition, automatic, events, native_pages
from test_automatic import SHA, setup_policy


def entry(body, *, status=200, offset=0):
    request = {"operationName": "GET_LISTINGS_PUBLIC_POSTS", "query": native_pages.QUERY,
               "variables": {"offset": offset, "limit": 100, "filter": {"contentTypesIds": [4, 10],
                             "sourcesIds": [12, 13], "locationsIds": [], "title": ""}}}
    request_raw = json.dumps(request, separators=(",", ":"))
    request_bytes = request_raw.encode()
    value = {"schema": "cre-nai-public-posts-page/v1",
             "receipt": {"sourceKey": "nai-global", "requestUrl": "https://infabode.com/graphql",
                         "finalUrl": "https://infabode.com/graphql", "method": "POST", "status": status,
                         "startedAt": "2026-10-28T10:00:00.000Z", "finishedAt": "2026-10-28T10:00:01.000Z",
                         "bodySha256": hashlib.sha256(body).hexdigest(), "bodyBytes": len(body)},
             "requestBody": request_raw, "requestBodySha256": hashlib.sha256(request_bytes).hexdigest(),
             "requestBodyBytes": len(request_bytes), "responseBodyBase64": base64.b64encode(body).decode(),
             "provenance": native_pages.PROVENANCE}
    raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    return raw, f"page-{offset}-{hashlib.sha256(raw).hexdigest()}.json"


def test_native_keeps_records_errors_and_exact_numbers():
    body = b'{"errors":[{"message":"partial"}],"data":{"publicPosts":[{"id":1},{"id":1},null,0.10000000000000000001]}}'
    raw, name = entry(body)
    observation, reason = native_pages.decode_entry(raw, name, {"attempt": 1})
    assert reason is None and len(observation["records"]) == 4
    assert acquisition.decode_json(acquisition.reconstruct_json(observation).encode()) == acquisition.decode_json(body)
    plan = events.plan(observation, source_key="nai-global")
    assert len(plan["events"]) == 5


@pytest.mark.parametrize("body,status", [(b'{"errors":[{"message":"missing"}],"data":null}', 200),
                                         (b'{"error":"unavailable","number":0.10000000000000000001}', 503)])
def test_json_errors_are_retained_as_context(body, status):
    raw, name = entry(body, status=status)
    observation, reason = native_pages.decode_entry(raw, name, {})
    assert reason is None and observation["records"] == [] and observation["arrayPointer"] is None
    assert observation["receipt"]["status"] == status
    assert acquisition.decode_json(acquisition.reconstruct_json(observation).encode()) == acquisition.decode_json(body)


def test_non_json_is_diagnostic():
    raw, name = entry(b'not json', status=503)
    assert native_pages.decode_entry(raw, name, {}) == (None, "invalid_json")


def test_tampered_or_wrong_operation_is_refused():
    raw, name = entry(b'{"data":{"publicPosts":[]}}')
    with pytest.raises(ValueError):
        native_pages.decode_entry(raw + b' ', name, {})
    value = json.loads(raw)
    request = json.loads(value["requestBody"])
    request["query"] = "query PRIVATE { privatePosts { id } }"
    value["requestBody"] = json.dumps(request)
    encoded = value["requestBody"].encode()
    value["requestBodyBytes"] = len(encoded)
    value["requestBodySha256"] = hashlib.sha256(encoded).hexdigest()
    forged = json.dumps(value).encode()
    with pytest.raises(ValueError):
        native_pages.decode_entry(forged, f"page-0-{hashlib.sha256(forged).hexdigest()}.json", {})


def native_run(tmp_path):
    config, policy, policy_path = setup_policy(tmp_path, sources=("nai-global",), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={"nai-global": "nai_public_posts_v1"}, max_native_pages=4)
    raw = events.encoded(policy)
    policy_path.write_bytes(raw)
    config = automatic.configuration(str(policy_path), hashlib.sha256(raw).hexdigest(), config["env_file"],
                                     config["target_sha256"], SHA, ["nai-global"])
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=("nai-global",),
                                    page_cap=4, concurrency=1, automatic_source_evidence=config)
    attempt = {"number": 1, "source_evidence": {"state": "no_artifact"}}
    manifest["sources"]["nai-global"]["attempts"].append(attempt)
    directory = native_pages.mark(run, manifest, "nai-global", attempt)
    return run, manifest, attempt, directory


def test_settlement_and_pump_keep_partial_pages_without_normalized_artifact(tmp_path, monkeypatch):
    run, manifest, attempt, directory = native_run(tmp_path)
    first = b'{"errors":[{"message":"partial"}],"data":{"publicPosts":[{"id":1},{"id":1},null]}}'
    for body, status, offset in [(first, 200, 0), (b'{}', 503, 100)]:
        raw, name = entry(body, status=status, offset=offset)
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    native_pages.settle(run, manifest, "nai-global", attempt, lambda *_: None)
    native = attempt["native_source_evidence"]
    assert native["state"] == "settled"
    assert [p["state"] for p in native["pages"]] == ["pending", "pending"]
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *args, **kwargs: calls.append(args))
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 2 and all(page["state"] == "appended" for page in native["pages"])
    assert attempt["source_evidence"]["state"] == "no_artifact"


def test_native_page_changed_after_sealing_refuses_before_database(tmp_path, monkeypatch):
    run, manifest, attempt, directory = native_run(tmp_path)
    raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}')
    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)
    native_pages.settle(run, manifest, "nai-global", attempt, lambda *_: None)
    path.write_bytes(raw + b' ')
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("database contacted"))
    automatic.pump(run, manifest, lambda *_: None)
    assert attempt["native_source_evidence"]["pages"][0]["state"] == "refused"


def test_serial_recovery_after_interruption_seals_and_replays_unknown_first(tmp_path, monkeypatch):
    run, manifest, attempt, directory = native_run(tmp_path)
    attempt["source_evidence"] = {"state": "no_artifact"}
    for offset in (0, 100):
        raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}', offset=offset)
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    automatic.recover_unsealed(run, manifest, lambda *_: None, make_private=lambda _: None)
    native = attempt["native_source_evidence"]
    assert native["state"] == "settled" and len(native["pages"]) == 2
    assert series.evidence_reconciliation_required(manifest)
    later = native["pages"][1]
    later["state"] = "outcome_unknown"
    automatic.prepare_resume(manifest, replay_unknown=True)
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda statement, *a, **k: calls.append(statement))
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 2 and all(page["state"] == "appended" for page in native["pages"])
    expected_later = native_pages.prepare_job(run, manifest, "nai-global", attempt, later)
    assert calls[0] == automatic.append_sql(expected_later)
    assert not series.evidence_reconciliation_required(manifest)


def test_serial_worker_page_survives_no_normalized_output(tmp_path, monkeypatch):
    run, manifest, _, initial_directory = native_run(tmp_path)
    initial_directory.rmdir()
    manifest["sources"]["nai-global"]["attempts"].clear()
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *_: None)
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: calls.append(a))

    def worker(_argv, _log, *, env):
        saved = json.loads((run / "manifest.json").read_bytes())
        marker = saved["sources"]["nai-global"]["attempts"][0]["native_source_evidence"]
        assert marker["state"] == "awaiting_artifact"
        directory = Path(env["CRE_NAI_NATIVE_CAPTURE_DIR"])
        assert directory == run / marker["directory"]
        assert env["CRE_NAI_NATIVE_CAPTURE_MAX_PAGES"] == "4"
        assert env["CRE_NAI_NATIVE_CAPTURE_MAX_BYTES"] == str(128 * 1024 * 1024)
        assert env["CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER"] == "1"
        raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}')
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return 1

    monkeypatch.setattr(refresh, "run_command", worker)
    assert refresh.collect_source(run, manifest, "nai-global", transactions=("sale", "lease"),
                                  page_cap=4, concurrency=1, attempts_this_run=1) is None
    attempt = manifest["sources"]["nai-global"]["attempts"][0]
    assert attempt["source_evidence"]["state"] == "no_artifact"
    assert attempt["native_source_evidence"]["pages"][0]["state"] == "appended" and len(calls) == 1


def test_interrupted_serial_worker_recovers_durable_native_page(tmp_path, monkeypatch):
    run, manifest, _, initial_directory = native_run(tmp_path)
    initial_directory.rmdir()
    manifest["sources"]["nai-global"]["attempts"].clear()
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda *_: None)

    def interrupted(_argv, _log, *, env):
        directory = Path(env["CRE_NAI_NATIVE_CAPTURE_DIR"])
        raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}')
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
        raise KeyboardInterrupt()

    monkeypatch.setattr(refresh, "run_command", interrupted)
    with pytest.raises(KeyboardInterrupt):
        refresh.collect_source(run, manifest, "nai-global", transactions=("sale", "lease"),
                               page_cap=4, concurrency=1, attempts_this_run=1)
    persisted = json.loads((run / "manifest.json").read_bytes())
    attempt = persisted["sources"]["nai-global"]["attempts"][0]
    assert attempt["native_source_evidence"]["state"] == "awaiting_artifact"
    automatic.recover_unsealed(run, persisted, refresh.save_manifest, make_private=refresh.make_private_artifact)
    assert attempt["source_evidence"]["state"] == "no_artifact"
    assert attempt["native_source_evidence"]["pages"][0]["state"] == "pending"


def test_unknown_native_write_replays_before_other_collector_job(tmp_path, monkeypatch):
    run, manifest, attempt, directory = native_run(tmp_path)
    raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}')
    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)
    native_pages.settle(run, manifest, "nai-global", attempt, lambda *_: None)
    native = attempt["native_source_evidence"]["pages"][0]
    native["state"] = "outcome_unknown"
    attempt["source_evidence"] = {"state": "pending"}
    automatic.prepare_resume(manifest, replay_unknown=True)
    calls = []

    def unknown(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("unknown outcome")

    monkeypatch.setattr(automatic.cli, "run_psql", unknown)
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 1 and native["state"] == "outcome_unknown"
    assert attempt["source_evidence"]["state"] == "pending"


@pytest.mark.parametrize("fails", [False, True])
def test_unknown_native_reconciles_with_collector_refused_without_other_writes(tmp_path, monkeypatch, fails):
    run, manifest, attempt, directory = native_run(tmp_path)
    for offset in (0, 100):
        raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}', offset=offset)
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    native_pages.settle(run, manifest, "nai-global", attempt, lambda *_: None)
    pages = attempt["native_source_evidence"]["pages"]
    pages[0]["state"] = "outcome_unknown"
    attempt["source_evidence"] = {"state": "refused", "reason": "unsafe_artifact"}
    manifest["automatic_source_evidence_paused"] = True
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert manifest["automatic_source_evidence_replay_only"]
    calls = []

    def append(*args, **kwargs):
        calls.append(args)
        if fails:
            raise RuntimeError("unknown outcome")

    monkeypatch.setattr(automatic.cli, "run_psql", append)
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 1 and pages[1]["state"] == "pending"
    assert pages[0]["state"] == ("outcome_unknown" if fails else "appended")
    assert attempt["source_evidence"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"]
    assert manifest["automatic_source_evidence_status"] == "incomplete"


def test_native_exact_replay_precedes_collector_exact_replay_on_tie(tmp_path, monkeypatch):
    run, manifest, attempt, directory = native_run(tmp_path)
    raw, name = entry(b'{"data":{"publicPosts":[{"id":1}]}}')
    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)
    native_pages.settle(run, manifest, "nai-global", attempt, lambda *_: None)
    native = attempt["native_source_evidence"]["pages"][0]
    native["state"] = "outcome_unknown"
    attempt["source_evidence"] = {"state": "outcome_unknown"}
    automatic.prepare_resume(manifest, replay_unknown=True)
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: calls.append(a))
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 1 and native["state"] == "appended"
    assert attempt["source_evidence"]["state"] == "refused"
