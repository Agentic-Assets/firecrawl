"""Synthetic association regressions; no network or provider calls."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("adjudication", Path(__file__).with_name("adjudication.py"))
adjudication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adjudication)


def test_fact_requires_label_value_unit_and_page_together():
    gold = {"label_aliases": ["Occupancy"], "value_aliases": ["100"], "unit_aliases": ["%"], "page": 2}
    wrong = [{"label": "Cap rate", "value": "100%", "page": 2}]
    assert adjudication.judge_fact(gold, wrong) == "wrong_association_candidate"
    assert adjudication.judge_fact(gold, [{"label": "Occupancy", "value": "100%", "page": 1}]) == "wrong_association_candidate"
    assert adjudication.judge_fact(gold, [{"label": "Occupancy", "value": "100 sf", "page": 2}]) == "missed"
    assert adjudication.judge_fact(gold, [{"label": "Occupancy", "value": "100 %", "page": 2}]) == "matched"
    assert adjudication.judge_fact({"aliases": ["100%"]}, wrong) == "unscorable"


def test_table_row_requires_same_table_title_header_and_page():
    gold = {"title_aliases": ["Rent roll"], "header_aliases": [["Tenant"], ["Rent"]], "page": 3}
    row = {"cells": ["A", "$10"]}
    wrong_header = [{"title": "Rent roll", "page": 3, "rows": [["Tenant", "Area"], ["A", "$10"]]}]
    assert adjudication.judge_table_row(gold, row, wrong_header) == "wrong_association_candidate"
    wrong_table = [{"title": "Sales", "page": 3, "rows": [["Tenant", "Rent"], ["A", "$10"]]}]
    assert adjudication.judge_table_row(gold, row, wrong_table) == "wrong_association_candidate"
    assert adjudication.judge_table_row(gold, row, [{"title": "Rent roll", "page": 3, "rows": [["Tenant", "Rent"], ["A", "$10"]]}]) == "matched"
    assert adjudication.judge_table_row({"title_aliases": ["Rent roll"]}, row, []) == "unscorable"


def test_partial_gold_unknown_outputs_and_no_value_echo():
    gold = {"facts": [{"id": "known", "label_aliases": ["Occupancy"], "value_aliases": ["100%"]},
                      {"id": "legacy", "aliases": ["secret"]}], "tables": []}
    result = {"status": "failed", "parsed": None}
    assert adjudication.adjudicate(gold, result) == [
        {"kind": "fact", "id": "known", "disposition": "unknown"},
        {"kind": "fact", "id": "legacy", "disposition": "unscorable"},
    ]


def test_cli_binds_gold_and_results_and_preserves_six_arm_denominator(tmp_path, monkeypatch, capsys):
    import hashlib
    import json
    import sys
    manifest = {"documents": [{"id": "doc-1", "sha256": "a" * 64}]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    sidecar = {"schema": "document-input-adjudication-gold/v1",
               "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
               "documents": [{"id": "doc-1", "pdf_sha256": "a" * 64,
                              "facts": [{"id": "f1", "label_aliases": ["Occupancy"], "value_aliases": ["100%"]}],
                              "tables": []}]}
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(sidecar))
    result = {"document_id": "doc-1", "pdf_sha256": "a" * 64, "model": "anthropic/claude-haiku-5.5",
              "arm": "anydoc_text", "status": "completed", "parsed": {"facts": [{"label": "Occupancy", "value": "100%"}]}}
    (tmp_path / "doc-1--0--anydoc_text.result.json").write_text(json.dumps(result))
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["adjudication", "--manifest", str(manifest_path), "--gold", str(gold_path),
                                      "--results", str(tmp_path), "--out", str(out)])
    adjudication.main()
    report = json.loads(out.read_bytes())
    assert len(report["rows"]) == 6
    assert report["counts"] == {"matched": 1, "unknown": 5}
    assert "100%" not in out.read_text()
    assert out.stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out)["result_rows"] == 6


def test_unreported_page_is_unknown_and_does_not_hide_other_wrong_associations():
    gold = {"label_aliases": ["Occupancy"], "value_aliases": ["100%"], "page": 2}
    missing_page = {"label": "Occupancy", "value": "100%", "page": None}
    assert adjudication.judge_fact(gold, [missing_page]) == "unknown"
    assert adjudication.judge_fact(gold, [{"label": "Cap rate", "value": "100%"}, missing_page]) == "wrong_association_candidate"
