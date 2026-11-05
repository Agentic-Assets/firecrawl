"""Lyon partial quality allowance must be backed by exact inserted-parent readback."""
import json
from pathlib import Path

import cre_checkpoint_refresh as refresh
import pytest
from tests.test_cre_checkpoint_refresh import absolute_quality_report


@pytest.mark.parametrize("case", ["verified", "old_deterioration", "bad_readback", "bad_children", "overclaim", "boolean", "missing_allowance", "other_source"])
def test_final_quality_keeps_verified_new_lyon_rows_but_rejects_regressions(tmp_path, monkeypatch, case):
    run = tmp_path / "run"
    (run / "logs").mkdir(parents=True)
    before = absolute_quality_report(source="lyon-stahl", missing_canonical_url="1584")
    after = absolute_quality_report(source="lyon-stahl", missing_canonical_url="1744" if case == "old_deterioration" else "1743")
    if case == "other_source":
        after["queries"]["quality_by_source"].append({"source_key": "jll", "missing_canonical_url": "1"})
    after["queries"]["freshness_generations"] = []
    after["queries"]["inventory_only_index"] = []
    (run / "pre-validation.json").write_text(json.dumps(before), encoding="utf-8")
    manifest = refresh.new_manifest(run, git_sha="a" * 40, git_dirty=False,
                                   sources=("lyon-stahl",), page_cap=400, concurrency=1)
    manifest["preflight"]["validation_path"] = "pre-validation.json"
    binding = {"artifact_sha256": "b" * 64, "generation_id": manifest["run_id"],
               "database_target_sha256": "c" * 64, "staged_sha256": "d" * 64,
               "held": [], "emitted_count": 203}
    checkpoint = manifest["sources"]["lyon-stahl"]
    checkpoint.update(state="ingested", artifact={"lyon_partial": True, "sha256": "b" * 64},
                      gate={"current_inventory_proof": binding}, subset_ingest_attempt_id="e" * 32)
    observed = {"ok": True, "outcome": "exact_commit", "artifact_sha256": "b" * 64,
                "generation_id": manifest["run_id"], "database_target_sha256": "c" * 64,
                "attempt_id": "e" * 32, "staged_sha256": "d" * 64, "held": [],
                "expected": 203, "new_missing_canonical_count": 159, "child_collections_verified": True}
    if case == "bad_readback":
        observed["attempt_id"] = "f" * 32
    elif case == "bad_children":
        observed["child_collections_verified"] = False
    elif case == "overclaim":
        observed["new_missing_canonical_count"] = 204
    elif case == "boolean":
        observed["new_missing_canonical_count"] = True
    elif case == "missing_allowance":
        observed.pop("new_missing_canonical_count")

    def write_validation(argv, _log, env):
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(after), encoding="utf-8")
        return 0

    monkeypatch.setattr(refresh, "run_command", write_validation)
    monkeypatch.setattr(refresh, "lyon_partial_readback", lambda *_: observed)
    if case == "verified":
        result = refresh.run_final_validation(run, manifest, None)
        assert result["quality_no_regression"] is True
        assert result["lyon_new_missing_canonical_count"] == 159
        assert result["absolute_quality_ok"] is False
        assert result["readback_ok"] is True
    else:
        with pytest.raises(refresh.GlobalStageError, match="final validation"):
            refresh.run_final_validation(run, manifest, None)
        assert manifest["validation"]["quality_no_regression"] is False
        if case in {"bad_readback", "bad_children", "overclaim", "boolean"}:
            assert manifest["validation"]["readback_ok"] is False
            assert "lyon_new_missing_canonical_count" not in manifest["validation"]
        elif case == "missing_allowance":
            assert manifest["validation"]["readback_ok"] is True
            assert "lyon_new_missing_canonical_count" not in manifest["validation"]


@pytest.mark.parametrize("invalid", [-1, True, "159"])
def test_quality_refuses_invalid_allowance(invalid):
    before = absolute_quality_report(source="lyon-stahl")
    with pytest.raises(ValueError, match="allowance"):
        refresh.compare_validation_quality(before, before, lyon_new_missing_canonical=invalid)


def test_new_parent_allowance_excludes_held_and_preexisting_missing():
    from cre_lyon_partial import new_missing_canonical_count, stage_record
    from tests.test_lyon_partial_stage import record

    staged = [stage_record(record(f"https://lyonstahl.com/properties/{key}/", "property_path", key))
              for key in ("new", "held", "old")]
    before = [{"external_id": key, "listing": ({"canonical_url": None} if key == "old" else None),
               "source_index": None, "children": {}} for key in ("new", "held", "old")]
    assert new_missing_canonical_count(staged, ["held"], before) == 1
