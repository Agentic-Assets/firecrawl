"""Synthetic-only tests: no network, credentials, PDF uploads, or database."""
import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

SPEC = importlib.util.spec_from_file_location("document_benchmark_runner", Path(__file__).with_name("runner.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def corpus(tmp_path):
    documents = []
    for index in range(30):
        data = f"%PDF-1.4 synthetic {index}".encode()
        gold = runner.canonical({"schema": "document-input-benchmark-gold/v1", "facts": [{"id": "one", "aliases": ["example"]}],
                                 "tables": [{"id": "table", "rows": [{"cells": ["A", "0"]}]}]})
        pdf_name, gold_name = f"doc-{index}.pdf", f"gold-{index}.json"
        (tmp_path / pdf_name).write_bytes(data)
        (tmp_path / gold_name).write_bytes(gold)
        documents.append({"id": f"doc-{index}", "path": pdf_name, "sha256": runner.digest(data),
                          "gold_path": gold_name, "gold_sha256": runner.digest(gold)})
    path = tmp_path / "manifest.json"
    path.write_bytes(runner.canonical({"schema": "document-input-benchmark-corpus/v1", "documents": documents}))
    return path, documents


def setup(tmp_path, monkeypatch):
    manifest, documents = corpus(tmp_path)
    converter = tmp_path / "converter"
    package = converter / "node_modules/@firecrawl/anydoc"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"0.2.4"}')
    (converter / "package-lock.json").write_text('{}')
    monkeypatch.setattr(runner, "extraction", lambda *_: {"status": "extracted", "text": "Example 0", "elapsed_seconds": 0.25})
    monkeypatch.setattr(runner, "reference", lambda *_, **__: {"status": "extracted", "text": "Example 0"})
    out = tmp_path / "private"
    runner.prepare(manifest, out, converter)
    return manifest, documents, out


def test_manifest_hash_binding_and_unknown_schema(tmp_path):
    path, docs = corpus(tmp_path)
    assert len(runner.load_manifest(path)[0]["documents"]) == 30
    (tmp_path / docs[0]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        runner.load_manifest(path)


def test_native_and_text_have_same_prompt_settings_and_pinned_provider(tmp_path, monkeypatch):
    path, docs, out = setup(tmp_path, monkeypatch)
    text = runner.make_spec(docs[0], runner.MODELS[0], "anydoc_text", path, out, "high", 1000, 0.4)
    pdf = runner.make_spec(docs[0], runner.MODELS[0], "native_pdf", path, out, "high", 1000, 0.4)
    assert text["request"]["messages"][0]["content"][0] == pdf["request"]["messages"][0]["content"][0]
    assert text["request"]["reasoning"] == pdf["request"]["reasoning"] == {"effort": "high"}
    assert pdf["request"]["providerOptions"] == {"gateway": {"only": ["anthropic"]}}
    assert pdf["request"]["messages"][0]["content"][1]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_resumable_full_grid_and_cost_unknown(tmp_path, monkeypatch):
    path, _, out = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "synthetic-key")
    answer = json.dumps({"transcription": "Example 0", "facts": [{"label": "Name", "value": "Example"}], "tables": [{"rows": [["A", "0"]]}]})
    calls = []
    def fake_request(url, key, *, data=None):
        calls.append(url)
        assert key == "synthetic-key"
        if "/generation?" in url:
            return {"http_status": 404, "error": "http_error"}
        return {"http_status": 200, "body": {"id": "gen_abc", "choices": [{"message": {"content": answer}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}}
    monkeypatch.setattr(runner, "request_json", fake_request)
    kwargs = {"effort": "high", "max_tokens": 1000, "workers": 3, "budget": 75, "reservation": 0.4}
    first = runner.run_calls(path, out, **kwargs)
    assert first["dispatched"] == 180 and len(calls) == 360
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in out.glob("*.result.json"))
    assert runner.run_calls(path, out, **kwargs)["existing"] == 180 and len(calls) == 360
    report = runner.report(path, out)
    assert len(report["rows"]) == 180 and report["rows"][0]["actual_cost_usd"] is None
    assert report["rows"][0]["metrics"]["table_exact_row_hits"] == 1


def test_interrupted_intent_and_tampered_preparation_refuse(tmp_path, monkeypatch):
    path, docs, out = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "synthetic-key")
    kwargs = {"effort": "high", "max_tokens": 1000, "workers": 1, "budget": 75, "reservation": 0.4}
    spec = runner.make_spec(docs[0], runner.MODELS[0], "anydoc_text", path, out, "high", 1000, 0.4)
    runner.write_new(out / (spec["name"] + ".intent.json"), runner.canonical({"identity": spec["identity"], "dispatch": "started"}))
    with mock.patch.object(runner, "request_json", side_effect=AssertionError("must not call")), pytest.raises(ValueError, match="unsettled"):
        runner.run_calls(path, out, **kwargs)
    (out / f"{docs[0]['id']}.input.json").write_text('{}')
    with pytest.raises(ValueError, match="prepared inputs changed"):
        runner.run_calls(path, out, **kwargs)


def test_invalid_json_truncation_and_bounded_budget(tmp_path, monkeypatch):
    path, _, out = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "synthetic-key")
    monkeypatch.setattr(runner, "request_json", lambda url, key, **kwargs: {"http_status": 200, "body": {"choices": [{"message": {"content": "not json"}, "finish_reason": "length"}]}})
    counts = runner.run_calls(path, out, effort="high", max_tokens=1000, workers=1, budget=0.8, reservation=0.4)
    assert counts["dispatched"] == 2 and counts["budget_blocked"] == 178
    rows = runner.report(path, out)["rows"]
    assert rows[0]["invalid_json"] and rows[0]["truncated"] and rows[-1]["status"] == "not_completed"


def test_gateway_lookup_cost_unwrap_and_private_env(tmp_path):
    assert runner.lookup_cost({"body": {"data": {"total_cost": "0.0125", "is_byok": False}}}) == 0.0125
    assert runner.lookup_cost({"body": {"data": {"total_cost": "nan", "is_byok": False}}}) is None
    path = tmp_path / "private.env"
    path.write_text('UNRELATED=ignored\nAI_GATEWAY_API_KEY="synthetic-key"\n')
    path.chmod(0o600)
    assert runner.read_env_key(path) == "synthetic-key"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        runner.read_env_key(path)


def test_malformed_fact_and_table_members_are_invalid():
    for value in ({"transcription": "x", "facts": ["bad"], "tables": []},
                  {"transcription": "x", "facts": [], "tables": [{"rows": [[False]]}]}):
        body = {"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]}
        assert runner.response_content(body)[1] is None


def test_qualified_unsupported_route_keeps_30_cells(tmp_path, monkeypatch):
    path, _, out = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "synthetic-key")
    qualification = {"model": "alibaba/qwen3.8-flash", "provider": "alibaba", "http_status": 400,
                     "pdf_sha256": "a" * 64, "text_layer_empty": True,
                     "response": {"error": {"type": "AI_UnsupportedFunctionalityError", "message": "Only image file parts are supported"}}}
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(runner.canonical(qualification))
    proof_hash = runner.unsupported_evidence(evidence)
    monkeypatch.setattr(runner, "request_json", lambda url, key, **kw: {"http_status": 400, "error": "http_error"})
    result = runner.run_calls(path, out, effort="medium", max_tokens=1000, workers=1, budget=0.4, reservation=0.4,
                              unsupported_hash=proof_hash)
    assert result["skipped"] == 30 and result["dispatched"] == 1
    report = runner.report(path, out)
    unsupported = [r for r in report["rows"] if r["status"] == "unsupported_not_run"]
    assert len(unsupported) == 30 and len(report["rows"]) == 180
    qualification["provider"] = "wrong"
    evidence.write_bytes(runner.canonical(qualification))
    with pytest.raises(ValueError, match="does not establish"):
        runner.unsupported_evidence(evidence)


def test_fact_presence_does_not_imply_label_match(tmp_path, monkeypatch):
    path, docs, out = setup(tmp_path, monkeypatch)
    gold_path = tmp_path / docs[0]["gold_path"]
    gold = json.loads(gold_path.read_bytes())
    gold["facts"][0]["label"] = "Price"
    gold_path.write_bytes(runner.canonical(gold))
    inputs = json.loads((out / f"{docs[0]['id']}.input.json").read_bytes())
    result = {"parsed": {"transcription": "Example 0", "facts": [{"label": "Year", "value": "Example"}], "tables": []}}
    metrics = runner.score(docs[0], result, inputs, path)
    assert metrics["fact_value_presence_proxy_hits"] == 1
    assert metrics["label_value_exact_proxy_hits"] == 0


def test_reconcile_records_authoritative_cost_without_model_calls(tmp_path, monkeypatch):
    path, _, out = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "synthetic-key")
    def query(url, key, **kwargs):
        if "/generation?" in url:
            return {"http_status": 404, "error": "http_error"}
        return {"http_status": 200, "body": {"id": "gen_test", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}}
    monkeypatch.setattr(runner, "request_json", query)
    runner.run_calls(path, out, effort="medium", max_tokens=100, workers=1, budget=0.4, reservation=0.4)
    def cost_only(url, key, **kwargs):
        assert "/generation?" in url
        return {"http_status": 200, "body": {"data": {"total_cost": "0.012", "is_byok": False}}}
    monkeypatch.setattr(runner, "request_json", cost_only)
    assert runner.reconcile(out, "synthetic-key") == {"lookups": 1, "resolved": 1}
    output = next(out.glob("*.result.json"))
    assert runner.reconciled_cost(output, json.loads(output.read_bytes())) == 0.012
    assert runner.reconcile(out, "synthetic-key") == {"lookups": 0, "resolved": 0}


def test_unknown_or_byok_cost_keeps_reservation():
    assert runner.lookup_cost({"body": {"data": {"total_cost": 0, "is_byok": True}}}) is None
    assert runner.lookup_cost({"body": {"data": {"total_cost": 0}}}) is None


def test_atomic_publication_hides_incomplete_file(tmp_path, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    real_fsync = runner.os.fsync
    def paused(fd):
        entered.set()
        assert release.wait(3)
        real_fsync(fd)
    monkeypatch.setattr(runner.os, "fsync", paused)
    target = tmp_path / "result.json"
    thread = threading.Thread(target=runner.write_new, args=(target, b'{"ok":true}'))
    thread.start()
    assert entered.wait(3)
    assert not target.exists()
    release.set()
    thread.join(3)
    assert not thread.is_alive() and json.loads(target.read_bytes()) == {"ok": True}
    with pytest.raises(FileExistsError):
        runner.write_new(target, b"replaced")
