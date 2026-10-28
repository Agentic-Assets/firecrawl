"""Draft offline policy and document append state tests; no database calls."""

import copy
import hashlib
import json

import pytest

from source_evidence import automatic, document_jobs
from source_evidence.document_process import DocumentProcessCleanupUnknown, cleanup_unknown_in_chain
from test_automatic import SHA, setup_policy


def v3_config(tmp_path):
    old, policy, path = setup_policy(tmp_path, sources=("faris-lee",))
    policy.update(schema_version=automatic.SCHEMA_V3, collector_projections={}, native_source_formats={},
                  max_native_pages=128, document_text_formats={"faris-lee": automatic.DOCUMENT_TEXT_FORMAT})
    raw = json.dumps(policy).encode()
    path.write_bytes(raw)
    return automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), old["env_file"],
                                   old["target_sha256"], SHA, ["faris-lee"]), policy, path


def manifest_for(config, count=1):
    documents = [{"append": {"kind": "document_text", "state": "pending", "ordinal": i}}
                 for i in range(count)]
    attempt = {"number": 1, "document_source_evidence": {"documents": documents}}
    manifest = {"run_id": "synthetic", "config": {"automatic_source_evidence": config,
                                                   "sources": ["faris-lee"]},
                "sources": {"faris-lee": {"attempts": [attempt]}}}
    return manifest, attempt, documents


def test_v3_policy_scope_and_legacy_identity(tmp_path):
    config, policy, path = v3_config(tmp_path)
    assert config["document_text_formats"] == {"faris-lee": automatic.DOCUMENT_TEXT_FORMAT}
    for change in ({"outside": automatic.DOCUMENT_TEXT_FORMAT}, {"faris-lee": "unknown"}, {"jll": automatic.DOCUMENT_TEXT_FORMAT}):
        invalid = {**policy, "document_text_formats": change}
        raw = json.dumps(invalid).encode()
        path.write_bytes(raw)
        with pytest.raises(ValueError):
            automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config["env_file"],
                                    config["target_sha256"], SHA, ["faris-lee"])
    legacy = dict(policy, schema_version=automatic.SCHEMA_V2)
    legacy.pop("document_text_formats")
    raw = json.dumps(legacy).encode()
    path.write_bytes(raw)
    restored = automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config["env_file"],
                                       config["target_sha256"], SHA, ["faris-lee"])
    assert "document_text_formats" not in restored


def test_78_jobs_share_existing_foreground_pump_and_durable_intent(tmp_path, monkeypatch):
    config, _, _ = v3_config(tmp_path)
    manifest, _, documents = manifest_for(config, 78)
    monkeypatch.setattr(automatic, "_verify_config", lambda *a: None)
    monkeypatch.setattr(document_jobs, "prepare_job", lambda run, m, source, attempt, document, job: {"ordinal": job["ordinal"]})
    monkeypatch.setattr(automatic, "append_sql", lambda plan: plan)
    calls = []
    def append(plan, *args, **kwargs):
        assert documents[plan["ordinal"]]["append"]["state"] == "appending"
        calls.append(plan["ordinal"])
    monkeypatch.setattr(automatic.cli, "run_psql", append)
    saved = []
    automatic.pump(tmp_path, manifest, lambda _, current: saved.append(copy.deepcopy(current)))
    assert calls == list(range(78)) and all(item["append"]["state"] == "appended" for item in documents)
    assert any(item["sources"]["faris-lee"]["attempts"][0]["document_source_evidence"]["documents"][0]["append"]["state"] == "appending" for item in saved)


def test_v2_without_document_admission_refuses_without_sql(tmp_path, monkeypatch):
    config, _, _ = setup_policy(tmp_path)
    manifest, _, documents = manifest_for(config)
    monkeypatch.setattr(automatic, "_verify_config", lambda *a: None)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("must not append"))
    automatic.pump(tmp_path, manifest, lambda *a: None)
    assert documents[0]["append"]["state"] == "refused"
    assert manifest["automatic_source_evidence_paused"] is True


def test_uncertain_document_replay_precedes_new_work_and_refusal_keeps_replay_only(tmp_path, monkeypatch):
    config, _, _ = v3_config(tmp_path)
    manifest, attempt, documents = manifest_for(config, 2)
    documents[0]["append"]["state"] = "outcome_unknown"
    attempt["source_evidence"] = {"state": "refused"}
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert manifest["automatic_source_evidence_replay_only"] is True
    monkeypatch.setattr(automatic, "_verify_config", lambda *a: None)
    monkeypatch.setattr(document_jobs, "prepare_job", lambda run, m, source, attempt, document, job: job["ordinal"])
    monkeypatch.setattr(automatic, "append_sql", lambda plan: plan)
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda plan, *args, **kwargs: calls.append(plan))
    automatic.pump(tmp_path, manifest, lambda *a: None)
    assert calls == [0] and documents[0]["append"]["state"] == "appended"
    assert documents[1]["append"]["state"] == "pending" and manifest["automatic_source_evidence_paused"] is True


def test_possible_write_failure_preserves_unknown_exact_plan(tmp_path, monkeypatch):
    config, _, _ = v3_config(tmp_path)
    manifest, _, documents = manifest_for(config)
    monkeypatch.setattr(automatic, "_verify_config", lambda *a: None)
    monkeypatch.setattr(document_jobs, "prepare_job", lambda *args: {"plan": "sealed"})
    monkeypatch.setattr(automatic, "append_sql", lambda plan: plan)
    def fail(*args, **kwargs):
        raise OSError("synthetic unknown transaction")
    monkeypatch.setattr(automatic.cli, "run_psql", fail)
    automatic.pump(tmp_path, manifest, lambda *a: None)
    assert documents[0]["append"]["state"] == "outcome_unknown"
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert documents[0]["append"]["exact_replay_required"] is True


def test_interruption_during_possible_write_is_saved_unknown_and_propagated(tmp_path, monkeypatch):
    config, _, _ = v3_config(tmp_path)
    manifest, _, documents = manifest_for(config)
    monkeypatch.setattr(automatic, "_verify_config", lambda *a: None)
    monkeypatch.setattr(document_jobs, "prepare_job", lambda *args: {"plan": "sealed"})
    monkeypatch.setattr(automatic, "append_sql", lambda plan: plan)
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(automatic.cli, "run_psql", interrupted)
    saved = []
    with pytest.raises(KeyboardInterrupt):
        automatic.pump(tmp_path, manifest, lambda _, current: saved.append(copy.deepcopy(current)))
    assert documents[0]["append"]["state"] == "outcome_unknown"
    assert saved[-1]["sources"]["faris-lee"]["attempts"][0]["document_source_evidence"]["documents"][0]["append"]["state"] == "outcome_unknown"


@pytest.mark.parametrize("save_fails", [False, True])
def test_sql_cleanup_unknown_keeps_foreground_barrier_even_if_save_fails(tmp_path, monkeypatch, save_fails):
    config, _, _ = v3_config(tmp_path)
    manifest, _, documents = manifest_for(config)
    monkeypatch.setattr(automatic, "_verify_config", lambda *a: None)
    monkeypatch.setattr(document_jobs, "prepare_job", lambda *args: {"plan": "sealed"})
    monkeypatch.setattr(automatic, "append_sql", lambda plan: plan)
    def failure(*args, **kwargs):
        try:
            raise DocumentProcessCleanupUnknown("synthetic unknown local settlement")
        except DocumentProcessCleanupUnknown as error:
            raise RuntimeError("wrapped") from error
    monkeypatch.setattr(automatic.cli, "run_psql", failure)
    saved = []
    def save(_, current):
        saved.append(copy.deepcopy(current))
        if save_fails and documents[0]["append"]["state"] == "outcome_unknown":
            raise OSError("synthetic checkpoint failure")
    with pytest.raises(BaseException) as caught:
        automatic.pump(tmp_path, manifest, save)
    assert cleanup_unknown_in_chain(caught.value)
    assert documents[0]["append"]["state"] == "outcome_unknown"
    assert manifest["automatic_source_evidence_paused"] is True
    assert saved[0]["sources"]["faris-lee"]["attempts"][0]["document_source_evidence"]["documents"][0]["append"]["state"] == "appending"
    if save_fails:
        assert isinstance(caught.value, DocumentProcessCleanupUnknown)
