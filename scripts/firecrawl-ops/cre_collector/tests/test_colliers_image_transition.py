"""No-network contracts for transaction-bound Colliers image transition evidence."""

import copy
import json
from pathlib import Path

import pytest
import cre_checkpoint_refresh as refresh
import cre_ingest as ingest


def reports():
    def report(images):
        return {
            "queries": {
                "child_counts": [
                    {
                        "source_key": "colliers-main",
                        "child_type": "images",
                        "count": str(images),
                    }
                ]
            }
        }

    return report(200), report(105)


def outcome():
    return {
        "schema": "colliers-image-transition-v1",
        "source_key": "colliers-main",
        "binding": {
            "artifact_sha256": "a" * 64,
            "artifact_bytes": 123,
            "artifact_run_key": "test",
            "generation_id": "test-run",
            "generation_started_at": "2026-10-16T00:00:00Z",
            "database_target_sha256": "b" * 64,
            "attempt_id": "c" * 32,
        },
        "semantic_before": 100,
        "semantic_retained_after": 75,
        "raw_before": 200,
        "raw_retained_after": 95,
        "raw_new_after": 10,
        "raw_after": 105,
        "raw_retired_before": 0,
        "staged_current": 10,
        "current_without_images": 0,
    }


def test_bound_semantic_transition_qualifies_raw_decline_without_mutating_reports():
    before, after = reports()
    original = copy.deepcopy((before, after))
    result = refresh.compare_validation_quality(
        before, after, image_transition=outcome()
    )
    assert result["ok"] is True
    assert result["image_transition"]["schema"] == "colliers-image-transition-v1"
    assert (before, after) == original
    assert refresh.compare_validation_quality(before, after)["ok"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("semantic_retained_after", 69),
        ("current_without_images", 1),
        ("raw_before", 199),
        ("raw_after", 106),
        ("raw_new_after", 9),
        ("semantic_before", True),
        ("semantic_retained_after", -1),
        ("source_key", "colliers"),
        ("schema", "unknown"),
        ("staged_current", 0),
    ],
)
def test_invalid_transition_keeps_raw_refusal(field, value):
    receipt = outcome()
    receipt[field] = value
    assert (
        refresh.compare_validation_quality(*reports(), image_transition=receipt)["ok"]
        is False
    )


def test_semantic_transition_never_waives_other_child_or_source_loss():
    before, after = reports()
    for source, child in [("colliers-main", "documents"), ("hanley", "images")]:
        before["queries"]["child_counts"].append(
            {"source_key": source, "child_type": child, "count": "100"}
        )
        after["queries"]["child_counts"].append(
            {"source_key": source, "child_type": child, "count": "1"}
        )
    result = refresh.compare_validation_quality(
        before, after, image_transition=outcome()
    )
    assert len(result["failures"]) == 2


def test_sql_outcome_is_emitted_after_commit_only_for_bound_transition():
    binding = outcome()["binding"]
    sql = ingest.build_sql(
        [],
        [],
        "2026-10-16T00:00:00Z",
        set(),
        colliers_first_party_transition=True,
        image_transition_binding=binding,
        skip_post_commit_summary=True,
    )
    assert sql.index("COMMIT;") < sql.index("CRE_COLLIERS_IMAGE_TRANSITION ")
    assert "ON COMMIT PRESERVE ROWS" in sql
    assert "raw_retired_before" in sql and "raw_new_after" in sql
    plain = ingest.build_sql([], [], "2026-10-16T00:00:00Z", set())
    assert "CRE_COLLIERS_IMAGE_TRANSITION " not in plain


def checkpoint_fixture(tmp_path):
    import hashlib
    import cre_colliers_image_transition as transition

    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"synthetic":true}')
    manifest = {
        "run_id": "fixture-generation",
        "started_at": "2026-10-16T00:00:00Z",
        "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
        "sources": {
            "colliers-main": {
                "state": "ingested",
                "artifact": {
                    "path": "artifact.json",
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "bytes": artifact.stat().st_size,
                    "staged_unique": 10,
                },
                "ingest": {
                    "attempt_id": "c" * 32,
                    "rc": 0,
                    "started_at": "2026-10-16T00:00:01Z",
                    "finished_at": "2026-10-16T00:00:02Z",
                    "log": "ingest.log",
                    "mark_missing": False,
                    "status_activation": False,
                },
            }
        },
    }
    receipt = outcome()
    receipt["binding"] = transition.expected_binding(tmp_path, manifest, "c" * 32)
    log = tmp_path / "ingest.log"
    log.write_text(transition.PREFIX + json.dumps(receipt) + "\ningest complete\n")
    manifest["sources"]["colliers-main"]["ingest"]["image_transition"] = (
        transition.read_outcome(log)
    )
    return manifest, receipt, artifact, log


def test_exact_committed_checkpoint_receipt_revalidated_before_final_use(tmp_path):
    import cre_colliers_image_transition as transition

    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    assert transition.verified_checkpoint(tmp_path, manifest) == receipt
    log.write_text(log.read_text() + "changed after checkpoint\n")
    assert transition.verified_checkpoint(tmp_path, manifest) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong_artifact",
        "wrong_generation",
        "wrong_target",
        "wrong_attempt",
        "nonzero",
        "unknown",
        "recovered",
        "unfinished",
        "not_ingested",
        "duplicate",
        "malformed",
        "symlink",
        "hardlink",
        "oversized",
    ],
)
def test_uncertain_or_unbound_checkpoint_never_qualifies(tmp_path, mutation):
    import cre_colliers_image_transition as transition

    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    cp = manifest["sources"]["colliers-main"]
    if mutation == "missing":
        cp["ingest"].pop("image_transition")
    elif mutation.startswith("wrong_"):
        key = {
            "wrong_artifact": "artifact_sha256",
            "wrong_generation": "generation_id",
            "wrong_target": "database_target_sha256",
            "wrong_attempt": "attempt_id",
        }[mutation]
        receipt["binding"][key] = "d" * 64
        log.write_text(transition.PREFIX + json.dumps(receipt) + "\n")
        cp["ingest"]["image_transition"] = transition.read_outcome(log)
    elif mutation == "nonzero":
        cp["ingest"]["rc"] = 1
    elif mutation == "unknown":
        cp["ingest"]["rc"] = None
    elif mutation == "recovered":
        cp["ingest_recovery"] = {"outcome": "exact_commit"}
    elif mutation == "unfinished":
        cp["ingest"]["finished_at"] = None
    elif mutation == "not_ingested":
        cp["state"] = "ingest_recovery_required"
    elif mutation == "duplicate":
        log.write_text(log.read_text() * 2)
    elif mutation == "malformed":
        log.write_text(transition.PREFIX + "{broken}\n")
    elif mutation == "symlink":
        original = tmp_path / "original"
        log.rename(original)
        log.symlink_to(original)
    elif mutation == "hardlink":
        (tmp_path / "alias").hardlink_to(log)
    elif mutation == "oversized":
        log.write_text(transition.PREFIX + " " * 9000 + "\n")
    assert transition.verified_checkpoint(tmp_path, manifest) is None


def test_real_checkpoint_ingest_captures_only_known_zero_result(tmp_path, monkeypatch):
    import cre_colliers_image_transition as transition

    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    # The command boundary alone is substituted; the real ingest checkpoint
    # builds argv, writes state and parses the actual bounded outcome carrier.
    monkeypatch.setattr(refresh, "save_manifest", lambda *args: None)
    monkeypatch.setattr(refresh.secrets, "token_hex", lambda size: "c" * 32)
    (tmp_path / "logs").mkdir()

    def command(argv, output, **kwargs):
        assert "--checkpoint-ingest-attempt" in argv
        assert argv[argv.index("--checkpoint-ingest-attempt") + 1] == "c" * 32
        output.write_text(
            transition.PREFIX + json.dumps(receipt) + "\ningest complete\n"
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    refresh.ingest_source(tmp_path, manifest, "colliers-main", artifact, None)
    assert transition.verified_checkpoint(tmp_path, manifest) == receipt


def test_actual_final_validation_reuses_bound_outcome_but_keeps_raw_report(
    tmp_path, monkeypatch
):
    from test_cre_preflight_validation_profile import declared_full

    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    before, after = declared_full(), declared_full()
    before["queries"]["child_counts"] = reports()[0]["queries"]["child_counts"]
    after["queries"]["child_counts"] = reports()[1]["queries"]["child_counts"]
    (tmp_path / "pre-validation.json").write_text(json.dumps(before))
    manifest["preflight"]["validation_path"] = "pre-validation.json"
    manifest["config"] = {"sources": ["colliers-main"]}

    def command(argv, output, **kwargs):
        result_path = Path(argv[argv.index("--out") + 1])
        result_path.write_text(json.dumps(after))
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(refresh, "save_manifest", lambda *args: None)
    monkeypatch.setattr(
        refresh,
        "verify_validation_readback",
        lambda *args, **kwargs: {"ok": True, "failed_sources": []},
    )
    refresh.run_final_validation(tmp_path, manifest, None)
    assert manifest["validation"]["quality_no_regression"] is True
    assert manifest["validation"]["image_transition"] == receipt
    assert json.loads((tmp_path / "validation.json").read_text()) == after
    manifest["sources"]["colliers-main"]["ingest"]["rc"] = None
    with pytest.raises(refresh.GlobalStageError, match="final validation"):
        refresh.run_final_validation(tmp_path, manifest, None)
    assert manifest["validation"]["quality_no_regression"] is False


@pytest.mark.parametrize(
    "mutation",
    ["artifact_bytes", "artifact_hardlink", "staged_count", "artifact_changed"],
)
def test_actual_artifact_identity_is_rechecked_at_final_comparison(tmp_path, mutation):
    import cre_colliers_image_transition as transition

    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    cp = manifest["sources"]["colliers-main"]
    if mutation == "artifact_bytes":
        cp["artifact"]["bytes"] += 1
    elif mutation == "artifact_hardlink":
        (tmp_path / "other-artifact").hardlink_to(artifact)
    elif mutation == "staged_count":
        cp["artifact"]["staged_unique"] += 1
    elif mutation == "artifact_changed":
        artifact.write_text('{"synthetic":false}')
    assert transition.verified_checkpoint(tmp_path, manifest) is None


def test_checkpoint_nonzero_after_receipt_still_requires_recovery(
    tmp_path, monkeypatch
):
    import cre_colliers_image_transition as transition

    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    monkeypatch.setattr(refresh, "save_manifest", lambda *args: None)
    monkeypatch.setattr(refresh, "poll_hook", lambda: lambda: None)
    (tmp_path / "logs").mkdir()

    def command(argv, output, **kwargs):
        output.write_text(transition.PREFIX + json.dumps(receipt) + "\n")
        return 1

    monkeypatch.setattr(refresh, "run_command", command)
    with pytest.raises(refresh.GlobalStageError, match="manual exact-generation"):
        refresh.ingest_source(tmp_path, manifest, "colliers-main", artifact, None)
    assert manifest["sources"]["colliers-main"]["state"] == "ingest_recovery_required"
    assert transition.verified_checkpoint(tmp_path, manifest) is None


def test_duplicate_json_keys_are_ambiguous_even_with_one_outcome_line(tmp_path):
    import cre_colliers_image_transition as transition

    log = tmp_path / "log"
    log.write_text(transition.PREFIX + '{"schema":"old","schema":"new"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        transition.read_outcome(log)
