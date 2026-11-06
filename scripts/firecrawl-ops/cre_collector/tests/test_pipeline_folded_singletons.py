"""Real gate rollups for folded brokerage keys in singleton write units."""
import json
import sys

import pytest

import cre_checkpoint_refresh as refresh
import cre_gate as gate


PAIRS = [("cbre", "cbre-dealflow"), ("jll", "jll-investor"), ("colliers", "colliers-main")]


@pytest.mark.parametrize("pair", PAIRS)
def test_singleton_aggregate_gates_admit_each_source_but_do_not_claim_folded_coverage(tmp_path, monkeypatch, pair):
    run_dir = tmp_path / "2026-11-06T000000Z"
    run_dir.mkdir()
    (run_dir / "sources").mkdir()
    manifest = refresh.new_manifest(run_dir, git_sha="a" * 40, git_dirty=False,
                                    sources=pair, page_cap=400, concurrency=1, pipeline=True)
    for source in pair:
        artifact = run_dir / "sources" / f"{source}.json"
        payload = {"runMeta": {"finishedAt": "2026-11-06T00:01:00Z"}, "brokers": [],
                   "sources": [{"sourceKey": source, "transaction": "sale", "error": None}],
                   "listings": [{"sourceKey": source, "id": f"fixture-{i}", "transactionMode": "sale",
                                 "url": f"https://example.test/{source}/{i}"} for i in range(100)]}
        artifact.write_text(json.dumps(payload))
        manifest["sources"][source]["artifact"] = {"path": f"sources/{source}.json"}
    baseline = {source: {"median": 100, "last": 100} for source in pair}
    monkeypatch.setattr(gate, "read_baseline", lambda *args: (baseline, None, None))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_args: None)
    gate_calls = []

    def run_real_gate(argv, _log, **_kwargs):
        assert argv[1] == "cre_gate.py"
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", argv[1:])
            gate.main()
        gate_calls.append(argv)
        return 0

    monkeypatch.setattr(refresh, "run_command", run_real_gate)
    per_source = {}
    for index, source in enumerate(pair):
        unit_dir = run_dir / "pipeline" / f"unit-{index}"
        unit_dir.mkdir(parents=True)
        result = refresh.run_aggregate_gate(run_dir, manifest, None, sources=[source], evidence_dir=unit_dir)
        report = json.loads((unit_dir / "aggregate-gate.json").read_text())
        assert report["per_source"][source]["verdict"] == "ok"
        assert report["per_source"][source]["mark_missing_safe"] is True
        assert result["non_ok_sources"] == []
        slug = gate._slug_for(source)
        assert report["per_brokerage"][slug]["mark_missing_safe"] is False
        assert "incomplete folded coverage" in report["per_brokerage"][slug]["reason"]
        assert slug not in result["mark_missing_safe_brokerages"]
        argv = refresh.build_ingest_argv(run_dir / manifest["sources"][source]["artifact"]["path"], None)
        assert "--mark-missing" not in argv and "--activate-status" not in argv
        per_source[source] = report["per_source"][source]
    assert len(gate_calls) == 2
    assert gate.rollup_brokerages(per_source)[gate._slug_for(pair[0])]["mark_missing_safe"] is True
