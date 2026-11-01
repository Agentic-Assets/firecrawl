"""Synthetic provenance, outcome and cost-denominator tests."""
import json

import pytest
import runner
import summarize


def fixture(tmp_path, monkeypatch):
    manifest = {"documents": [{"id": "d1", "sha256": "a" * 64, "pages": 1}]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("synthetic manifest")
    monkeypatch.setattr(runner, "load_manifest", lambda _: (manifest, "manifest-hash"))
    monkeypatch.setattr(runner, "score", lambda *_: {"table_exact_row_hits": 0, "table_gold_rows": 0})
    gold = {"schema": "document-input-adjudication-gold/v1", "manifest_sha256": "manifest-hash",
            "documents": [{"id": "d1", "pdf_sha256": "a" * 64,
                           "facts": [{"id": "occ", "label_aliases": ["Occupancy"], "value_aliases": ["100%"]}],
                           "tables": []}]}
    gold_path = tmp_path / "gold.json"
    gold_path.write_bytes(runner.canonical(gold))
    results = tmp_path / "results"
    results.mkdir()
    prepared = runner.canonical({"pdf_sha256": "a" * 64, "pdftotext": {"text": "Occupancy 100%"},
                                 "anydoc": {"status": "extracted", "text": "Occupancy 100%", "elapsed_seconds": 0.1}})
    (results / "d1.input.json").write_bytes(prepared)
    prep_hash = runner.digest(prepared)
    (results / "preparation.json").write_bytes(runner.canonical({"d1": prep_hash}))
    (results / "run.json").write_bytes(runner.canonical({"manifest_sha256": "manifest-hash"}))
    raw_text = json.dumps({"transcription": "Occupancy 100%", "facts": [{"label": "Cap rate", "value": "100%"}], "tables": []})
    for index, model in enumerate(runner.MODELS):
        for arm in runner.ARMS:
            stem = f"d1--{index}--{arm}"
            result = {"document_id": "d1", "pdf_sha256": "a" * 64, "model": model, "arm": arm,
                      "preparation_sha256": prep_hash, "status": "incomplete", "http_status": 200,
                      "finish_reason": "stop", "truncated": False, "raw_output": f"```json\n{raw_text}\n```",
                      "actual_cost_usd": 0.1}
            (results / f"{stem}.result.json").write_bytes(runner.canonical(result))
            (results / f"{stem}.intent.json").write_bytes(runner.canonical({"dispatch": "started"}))
    return manifest_path, gold_path, results, runner.digest(gold_path.read_bytes())


def test_recovered_wrong_label_stays_strict_incomplete(tmp_path, monkeypatch):
    args = fixture(tmp_path, monkeypatch)
    report = summarize.generate(*args)
    row = report["rows"][0]
    assert not row["strict_json_completed"] and row["usable_after_fence_normalization"]
    assert row["fact_matched"] == 0 and row["fact_wrong_association_candidate"] == 1
    assert report["summary"][0]["api_dispatched"] == 1


def test_changed_gold_or_prepared_reference_refused(tmp_path, monkeypatch):
    manifest, gold, results, gold_hash = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="gold hash"):
        summarize.generate(manifest, gold, results, "0" * 64)
    (results / "d1.input.json").write_text("{}")
    with pytest.raises(ValueError, match="prepared reference"):
        summarize.generate(manifest, gold, results, gold_hash)


def test_truncated_fenced_output_never_upgraded_and_failed_dispatch_cost_counted(tmp_path, monkeypatch):
    args = fixture(tmp_path, monkeypatch)
    path = args[2] / "d1--0--anydoc_text.result.json"
    result = json.loads(path.read_bytes())
    result.update(finish_reason="length", truncated=True, http_status=429)
    path.write_bytes(runner.canonical(result))
    report = summarize.generate(*args)
    row, summary = report["rows"][0], report["summary"][0]
    assert not row["usable_after_fence_normalization"] and row["fact_unknown"] == 1
    assert summary["http_200"] == 0 and summary["actual_gateway_cost_usd_known"] == 0.1
