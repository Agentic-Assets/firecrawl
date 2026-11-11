"""Mixed partial sources remain distinct through serial pipeline writes."""
import subprocess
import sys
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline


@pytest.mark.parametrize("order", [("lyon-stahl", "franklin-street"), ("franklin-street", "lyon-stahl")])
def test_mixed_partial_unit_uses_distinct_source_claims_in_both_orders(tmp_path, monkeypatch, order):
    root = tmp_path / "run"
    root.mkdir()
    manifest = refresh.new_manifest(root, git_sha="a" * 40, git_dirty=False, sources=order, page_cap=1,
        concurrency=1, pipeline=True, database_target={"algorithm": "sha256", "value": "b" * 64})
    manifest["pipeline"] = {"source_timings": {source: {} for source in order}, "commit_sequence": 0}
    for source in order:
        artifact = root / (source + ".json")
        artifact.write_text("{}")
        claim = root / (source + "-claims.json")
        claim.write_text("{}")
        key = "lyon_partial" if source == "lyon-stahl" else "franklin_partial"
        claim_key = "lyon_partial_claims" if source == "lyon-stahl" else "franklin_partial_claims"
        manifest["sources"][source].update(artifact={"path": artifact.name, key: True},
            subset_ingest_attempt_id="c" * 32,
            **{claim_key: {"path": str(claim), "sha256": refresh.sha256_file(claim), "attempt_id": "c" * 32}})
    before = root / "before.json"
    before.write_text("{}")
    evidence = root / "pipeline" / "unit-1"
    events = []
    unit = {"sources": list(order), "unit_id": "unit-1"}

    def before_report(_run, _manifest, unit, *_args, **_kwargs):
        unit["before_sha256"] = refresh.sha256_file(before)
        return before

    def aggregate(_run, _manifest, _env, **_kwargs):
        (evidence / "aggregate-gate.json").write_text("{}")
        return {"ok": True}

    def ingest_source(_run, state, source, _artifact, _env, *, require_buildout_preseal):
        assert require_buildout_preseal is True
        flags = refresh.subset_ingest_arguments(root, state, source)
        events.append((source, flags[0]))

    def validation(_run, _manifest, _env, **_kwargs):
        path = evidence / "validation.json"
        path.write_text("{}")
        return {"path": str(path.relative_to(root))}

    monkeypatch.setattr(pipeline, "unit_before_report", before_report)
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)
    monkeypatch.setattr(refresh, "dry_run_source", lambda *_: True)
    monkeypatch.setattr(refresh, "run_aggregate_gate", aggregate)
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", lambda run, state, source: (run / state["sources"][source]["artifact"]["path"], {}))
    monkeypatch.setattr(refresh, "_admit_current_inventory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(refresh, "ingest_source", ingest_source)
    monkeypatch.setattr(refresh, "run_final_validation", validation)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    pipeline.run_database_unit(root, manifest, unit, None, None)
    expected = {"lyon-stahl": "--lyon-partial-claims", "franklin-street": "--franklin-site-claims"}
    assert events == [(source, expected[source]) for source in order]
    assert manifest["pipeline"]["commit_sequence"] == 2


def test_ingest_cli_rejects_mixed_partial_claim_flags_before_reading_files():
    collector = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "cre_ingest.py", "--in", "nonexistent.json", "--dry-run",
        "--lyon-partial-claims", "lyon.json", "--franklin-site-claims", "franklin.json"],
        cwd=collector, text=True, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert "refusing mixed Franklin and Lyon partial claims" in result.stderr
