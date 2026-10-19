"""Canonical source/code binding, checkpoint settlement and raw-audit integration."""

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cre_checkpoint_refresh as refresh
import cre_ingest as ingest
import cre_transwestern_image_outcome as outcome
import cre_validate as validate
from test_cre_checkpoint_refresh import strict_artifact
from test_cre_preflight_validation_profile import declared_full
from test_transwestern_image_outcome import checkpoint_fixture as receipt_fixture
from test_transwestern_image_outcome import valid_outcome, report_row


def checkpoint_fixture(tmp_path):
    manifest, *rest = receipt_fixture(tmp_path)
    manifest["preflight"]["database_target"]["algorithm"] = "sha256"
    return manifest, *rest


def binding_input(tmp_path, monkeypatch):
    data = strict_artifact(source="transwestern", detail_scope="detail_page", preserve_children=False)
    path = tmp_path / "input.json"
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    args = SimpleNamespace(
        inputs=[str(path)], checkpoint_ingest_attempt="c" * 32,
        checkpoint_collector_git_sha="d" * 40, expected_db_target_sha256="b" * 64,
        require_strict_freshness=True, mark_missing=False, activate_status=False,
        daum_verified_subset_claims=None, matthews_targeted_claims=None,
    )
    monkeypatch.setattr(refresh, "git_identity", lambda: ("d" * 40, False))
    key, _ = ingest.artifact_run_identity_from_digests([hashlib.sha256(raw).hexdigest()])
    return args, data, raw, key


def test_canonical_binding_uses_actual_artifact_generation_and_clean_code(tmp_path, monkeypatch):
    args, data, raw, key = binding_input(tmp_path, monkeypatch)
    bound = ingest.transwestern_checkpoint_binding(args, data, raw, key)
    assert outcome.valid_binding(bound)
    assert bound["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert bound["generation_started_at"] == data["runMeta"]["freshness"]["generationStartedAt"]


@pytest.mark.parametrize("mutation", [
    "dirty", "code", "changed_bytes", "run_key", "mixed", "truncated", "capped",
    "missing_detail", "sale_only", "activate", "mark_missing", "wrong_attempt",
])
def test_canonical_binding_rejects_unqualified_input(tmp_path, monkeypatch, mutation):
    args, data, raw, key = binding_input(tmp_path, monkeypatch)
    if mutation == "dirty":
        monkeypatch.setattr(refresh, "git_identity", lambda: ("d" * 40, True))
    elif mutation == "code":
        args.checkpoint_collector_git_sha = "e" * 40
    elif mutation == "changed_bytes":
        Path(args.inputs[0]).write_bytes(raw + b" ")
    elif mutation == "run_key":
        key = "wrong"
    elif mutation == "mixed":
        data["sources"].append({"sourceKey": "svn"})
    elif mutation == "truncated":
        data["sources"][0]["truncated"] = True
    elif mutation == "capped":
        data["runMeta"]["maxItemsPerSource"] = 2
    elif mutation == "missing_detail":
        data["listings"][0].pop("detailObservedAt")
    elif mutation == "sale_only":
        data["runMeta"]["transactions"] = ["sale"]
    elif mutation == "activate":
        args.activate_status = True
    elif mutation == "mark_missing":
        args.mark_missing = True
    elif mutation == "wrong_attempt":
        args.checkpoint_ingest_attempt = "wrong"
    with pytest.raises(ValueError):
        ingest.transwestern_checkpoint_binding(args, data, raw, key)


def reports(receipt):
    before, after = declared_full(), declared_full()
    for report, count in ((before, receipt["raw_before"]), (after, receipt["raw_after"])):
        report["queries"]["child_counts"] = [
            {"source_key": "transwestern", "child_type": "images", "count": str(count)}
        ]
    after["transwestern_image_state"] = [report_row(receipt["raw_after"], receipt["image_state_sha256"])]
    return before, after


def test_raw_audit_waiver_requires_actual_role_digest_and_all_other_guards():
    receipt = valid_outcome()
    receipt.update(raw_before=100, protected_before=10, excluded_before=90)
    before, after = reports(receipt)
    original = copy.deepcopy((before, after))
    assert not refresh.compare_validation_quality(before, after)["ok"]
    assert refresh.compare_validation_quality(before, after, transwestern_image_transition=receipt)["ok"]
    assert (before, after) == original
    after["transwestern_image_state"][0]["image_state_sha256"] = "f" * 64
    assert not refresh.compare_validation_quality(before, after, transwestern_image_transition=receipt)["ok"]
    before, after = reports(receipt)
    for source, child in (("transwestern", "documents"), ("svn", "images")):
        before["queries"]["child_counts"].append({"source_key": source, "child_type": child, "count": "100"})
        after["queries"]["child_counts"].append({"source_key": source, "child_type": child, "count": "1"})
    assert len(refresh.compare_validation_quality(before, after, transwestern_image_transition=receipt)["failures"]) == 2


@pytest.mark.parametrize("rc,valid_receipt", [(0, True), (0, False), (3, True)])
def test_live_checkpoint_keeps_actual_exit_and_requires_receipt(tmp_path, monkeypatch, rc, valid_receipt):
    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    manifest["config"] = {}
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(refresh.secrets, "token_hex", lambda *_: "c" * 32)
    calls = []

    def command(argv, output, **_kwargs):
        calls.append(argv)
        assert argv[argv.index("--checkpoint-collector-git-sha") + 1] == "d" * 40
        output.write_text(outcome.PREFIX + json.dumps(receipt) + "\n" if valid_receipt else "no outcome\n")
        return rc

    monkeypatch.setattr(refresh, "run_command", command)
    if rc == 0 and valid_receipt:
        refresh.ingest_source(tmp_path, manifest, "transwestern", artifact, None)
        assert outcome.verified_checkpoint(tmp_path, manifest) == receipt
    else:
        with pytest.raises(refresh.GlobalStageError):
            refresh.ingest_source(tmp_path, manifest, "transwestern", artifact, None)
        assert manifest["sources"]["transwestern"]["state"] == "ingest_recovery_required"
    assert len(calls) == 1
    assert manifest["sources"]["transwestern"]["ingest"]["rc"] == rc


def test_final_validation_requests_role_readback_without_replacing_raw_families(tmp_path, monkeypatch):
    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    manifest["config"] = {"sources": ["transwestern"]}
    before, after = reports(receipt)
    (tmp_path / "pre.json").write_text(json.dumps(before))
    manifest["preflight"]["validation_path"] = "pre.json"

    def command(argv, output, **_kwargs):
        assert "--transwestern-image-state" in argv
        assert argv[argv.index("--expected-artifact-run-key") + 1] == receipt["binding"]["artifact_run_key"]
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(after))
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(refresh, "verify_validation_readback", lambda *_a, **_kw: {"ok": True, "failed_sources": []})
    refresh.run_final_validation(tmp_path, manifest, None)
    assert manifest["validation"]["transwestern_image_transition"] == receipt
    assert set(after["queries"]) == set(validate.QUERIES)
    after["transwestern_image_state"][0]["image_state_sha256"] = "f" * 64
    with pytest.raises(refresh.GlobalStageError, match="final validation"):
        refresh.run_final_validation(tmp_path, manifest, None)


def test_missing_attempt_never_downgrades_ingested_tw_to_ordinary_audit(tmp_path, monkeypatch):
    manifest, receipt, artifact, log = checkpoint_fixture(tmp_path)
    manifest["sources"]["transwestern"]["ingest"].pop("attempt_id")
    monkeypatch.setattr(refresh, "run_command", lambda *_a, **_kw: pytest.fail("must refuse before readback"))
    with pytest.raises(refresh.GlobalStageError, match="evidence is missing"):
        refresh.run_final_validation(tmp_path, manifest, None)


@pytest.mark.parametrize("state", ["ingesting", "ingest_recovery_required"])
def test_missing_ingest_record_never_downgrades_uncertain_tw_to_ordinary_audit(tmp_path, monkeypatch, state):
    manifest, *_ = checkpoint_fixture(tmp_path)
    manifest["sources"]["transwestern"].update(state=state, ingest={})
    monkeypatch.setattr(refresh, "run_command", lambda *_a, **_kw: pytest.fail("must refuse before readback"))
    with pytest.raises(refresh.GlobalStageError, match="evidence is missing"):
        refresh.run_final_validation(tmp_path, manifest, None)


def test_duplicate_image_count_rows_cannot_supply_contradictory_waiver():
    receipt = valid_outcome()
    receipt.update(raw_before=100, protected_before=10, excluded_before=90)
    before, after = reports(receipt)
    after["queries"]["child_counts"].append(
        {"source_key": "transwestern", "child_type": "images", "count": "0"}
    )
    result = refresh.compare_validation_quality(before, after, transwestern_image_transition=receipt)
    assert not result["ok"]
    assert "transwestern_image_transition" not in result


def test_other_database_units_do_not_reread_large_tw_artifact(tmp_path, monkeypatch):
    manifest, *_ = checkpoint_fixture(tmp_path)
    manifest["config"] = {"sources": ["transwestern", "svn"]}
    before = declared_full()
    (tmp_path / "pre.json").write_text(json.dumps(before))
    manifest["preflight"]["validation_path"] = "pre.json"
    monkeypatch.setattr(outcome, "verified_checkpoint", lambda *_: pytest.fail("unrelated unit must not hash TW artifact"))
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    monkeypatch.setattr(refresh, "verify_validation_readback", lambda *_a, **_kw: {"ok": True, "failed_sources": []})

    def command(argv, _log, **_kwargs):
        assert "--transwestern-image-state" not in argv
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(before))
        return 0

    monkeypatch.setattr(refresh, "run_command", command)
    refresh.run_final_validation(tmp_path, manifest, None, sources=["svn"])


@pytest.mark.parametrize("kwargs", [
    {}, {"expected_db_target_sha256": "b" * 64},
    {"expected_artifact_run_key": "fixture"},
    {"expected_db_target_sha256": "b" * 64, "expected_artifact_run_key": "fixture", "profile": "checkpoint-preflight"},
])
def test_image_state_readback_requires_bound_full_profile(tmp_path, kwargs):
    with pytest.raises(ValueError):
        refresh.build_validate_argv(tmp_path / "out.json", None, transwestern_image_state=True, **kwargs)


def test_validator_main_keeps_all_raw_queries_and_supplements_same_snapshot(tmp_path, monkeypatch):
    output = tmp_path / "validation.json"
    monkeypatch.setattr(sys, "argv", ["cre_validate.py", "--format", "json", "--out", str(output),
                                    "--expected-db-target-sha256", "b" * 64,
                                    "--expected-artifact-run-key", "ingest:v1:" + "a" * 64,
                                    "--transwestern-image-state"])
    monkeypatch.setattr(validate, "load_db_url", lambda *_: ("fixture-only", "fixture-env"))
    monkeypatch.setattr(validate, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(validate, "find_psql", lambda: "fixture-psql")
    calls = []

    def execute(_psql, _url, queries):
        calls.append(queries)
        assert all(queries[key] == sql for key, sql in validate.QUERIES.items())
        result = {key: [] for key in queries}
        result["transwestern_image_state"] = [report_row()]
        return result, {}, ""

    monkeypatch.setattr(validate, "run_queries_with_timings", execute)
    validate.main()
    report = json.loads(output.read_text())
    assert len(calls) == 1
    assert set(report["queries"]) == set(validate.QUERIES) | {"artifact_run_jobs"}
    assert report["transwestern_image_state"] == [report_row()]
    validate.require_full_validation_report(report)
