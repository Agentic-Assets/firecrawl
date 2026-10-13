"""Profile/consumer parity against synthetic snapshots; no DB or network."""

import copy
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cre_checkpoint_refresh as refresh
import cre_validate as validate
import pytest

REQUIRED = {
    "lifecycle_schema_contract",
    "source_counts",
    "duplicates",
    "bad_child_urls",
    "primary_child_conflicts",
    "orphans",
    "quality_by_source",
    "child_counts",
}


def snapshot():
    rows = {name: [] for name in validate.QUERIES}
    rows.update(
        {
            "lifecycle_schema_contract": [
                {"contract_item": item, "status": "ok"}
                for item in validate.LIFECYCLE_SCHEMA_CONTRACT_ITEMS
            ],
            "source_counts": [
                {"source_key": "svn", "active": "100"},
                {"source_key": "cbre", "active": "200"},
            ],
            "duplicates": [
                {
                    "check_name": "duplicate_external_id_groups",
                    "source_key": "svn",
                    "groups": "0",
                    "rows": "0",
                }
            ],
            "bad_child_urls": [{"check_name": "image_bad_url", "count": "0"}],
            "primary_child_conflicts": [{"child_type": "images", "listings": "0"}],
            "orphans": [{"child_type": "images", "orphan_rows": "0"}],
            "quality_by_source": [{"source_key": "svn", "bad_source_url": "0"}],
            "child_counts": [
                {"source_key": "svn", "child_type": "images", "count": "100"}
            ],
            "search_smoke": [{"smoke": "office_sale", "rows": "10"}],
        }
    )
    return {"ok": True, "queries": rows}


def reduced(full=None):
    full = snapshot() if full is None else full
    return {
        "ok": True,
        "validation_profile": validate.CHECKPOINT_PREFLIGHT_PROFILE,
        "audit_scope": validate.VALIDATION_AUDIT_SCOPES[
            validate.CHECKPOINT_PREFLIGHT_PROFILE
        ],
        "queries": {name: copy.deepcopy(full["queries"][name]) for name in REQUIRED},
    }


def declared_full():
    report = snapshot()
    report.update(
        {
            "validation_profile": "full",
            "audit_scope": validate.VALIDATION_AUDIT_SCOPES["full"],
            "historical_primary_child_conflicts": [],
        }
    )
    return report


@pytest.mark.parametrize("missing", sorted(validate.QUERIES))
def test_declared_full_consumer_rejects_every_missing_operational_family(missing):
    report = declared_full()
    del report["queries"][missing]
    with pytest.raises(ValueError, match="incomplete"):
        validate.require_full_validation_report(report)


@pytest.mark.parametrize(
    "fault",
    [
        "unexpected_family",
        "reduced_scope",
        "missing_historical",
        "malformed_historical",
    ],
)
def test_declared_full_cannot_masquerade_with_incomplete_or_unexpected_scope(fault):
    report = declared_full()
    if fault == "unexpected_family":
        report["queries"]["unrecognized"] = []
    elif fault == "reduced_scope":
        report["audit_scope"] = validate.VALIDATION_AUDIT_SCOPES["checkpoint-preflight"]
    elif fault == "missing_historical":
        del report["historical_primary_child_conflicts"]
    else:
        report["historical_primary_child_conflicts"] = [None]
    with pytest.raises(ValueError):
        validate.require_full_validation_report(report)


def test_declared_full_accepts_only_optional_exact_artifact_probe_family():
    report = declared_full()
    validate.require_full_validation_report(report)
    report["queries"]["artifact_run_jobs"] = [{"matching_jobs": "1"}]
    validate.require_full_validation_report(report)


def test_profile_is_exact_unchanged_subset_and_full_default_stays_full():
    full = validate.queries_for_profile()
    preflight = validate.queries_for_profile(validate.CHECKPOINT_PREFLIGHT_PROFILE)
    assert set(preflight) == REQUIRED == set(validate.CHECKPOINT_PREFLIGHT_QUERY_NAMES)
    assert list(preflight) == [name for name in validate.QUERIES if name in REQUIRED]
    for name, sql in preflight.items():
        assert sql == full[name] == validate.QUERIES[name]
    assert set(full) == set(validate.QUERIES) | {"historical_primary_child_conflicts"}
    assert {
        "freshness_generations",
        "inventory_generation_fingerprints",
        "enrichment_queue_health",
        "search_smoke",
    }.isdisjoint(preflight)
    key = "ingest:v1:" + "a" * 64
    assert "artifact_run_jobs" in validate.queries_for_profile(
        expected_artifact_run_key=key
    )
    with pytest.raises(ValueError, match="full"):
        validate.queries_for_profile(
            validate.CHECKPOINT_PREFLIGHT_PROFILE, expected_artifact_run_key=key
        )


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_every_missing_before_family_fails_closed(missing):
    report = reduced()
    del report["queries"][missing]
    with pytest.raises(ValueError, match="missing required"):
        validate.require_checkpoint_preflight_report(report)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_metadata",
        "scope",
        "unknown_profile",
        "null_profile",
        "scope_only",
        "extra_family",
        "row_type",
        "unavailable",
    ],
)
def test_malformed_or_misdeclared_profile_is_refused(fault):
    report = reduced()
    if fault == "missing_metadata":
        del report["validation_profile"]
        del report["audit_scope"]
    elif fault == "scope":
        report["audit_scope"] = validate.VALIDATION_AUDIT_SCOPES[
            validate.FULL_VALIDATION_PROFILE
        ]
    elif fault == "unknown_profile":
        report["validation_profile"] = []
    elif fault == "null_profile":
        report["validation_profile"] = None
    elif fault == "scope_only":
        del report["validation_profile"]
    elif fault == "extra_family":
        report["queries"]["totals"] = []
    elif fault == "row_type":
        report["queries"]["duplicates"] = [None]
    else:
        report["ok"] = False
    with pytest.raises(ValueError):
        validate.require_checkpoint_preflight_report(
            report, expected_profile=validate.CHECKPOINT_PREFLIGHT_PROFILE
        )
    if fault != "missing_metadata":
        assert refresh.compare_validation_quality(report, snapshot())["ok"] is False


def test_legacy_full_snapshot_remains_compatible_but_cannot_fulfill_new_profile_request():
    report = snapshot()
    assert validate.require_checkpoint_preflight_report(report) == "full"
    validate.require_full_validation_report(report)
    with pytest.raises(ValueError, match="requested profile"):
        validate.require_checkpoint_preflight_report(
            report, expected_profile=validate.CHECKPOINT_PREFLIGHT_PROFILE
        )


@pytest.mark.parametrize(
    "changed_family,field,value",
    [
        ("duplicates", "groups", "1"),
        ("bad_child_urls", "count", "1"),
        ("primary_child_conflicts", "listings", "1"),
        ("orphans", "orphan_rows", "1"),
        ("quality_by_source", "bad_source_url", "1"),
        ("child_counts", "count", "50"),
        ("search_smoke", "rows", "0"),
    ],
)
def test_every_before_consumer_has_same_metrics_and_failure_result_from_one_snapshot(
    changed_family, field, value
):
    full = snapshot()
    before = reduced(full)
    assert refresh.require_lifecycle_schema_contract(
        full
    ) == refresh.require_lifecycle_schema_contract(before)
    old_scope = {"config": {"sources": ["svn"]}, "scope": {}}
    reduced_scope = copy.deepcopy(old_scope)
    refresh.record_scope_from_validation(old_scope, full)
    refresh.record_scope_from_validation(reduced_scope, before)
    assert old_scope == reduced_scope
    assert old_scope["scope"]["unsupported_active_rows_before"] == 200
    after = copy.deepcopy(full)
    after["queries"][changed_family][0][field] = value
    original = refresh.compare_validation_quality(full, after)
    assert original == refresh.compare_validation_quality(before, after)
    assert original["ok"] is False


def test_reduced_report_cannot_be_full_absolute_or_after_regression_evidence():
    before = reduced()
    with pytest.raises(ValueError, match="full"):
        validate.require_full_validation_report(before)
    assert refresh.verify_absolute_validation_quality(before)["ok"] is False
    assert refresh.compare_validation_quality(snapshot(), before)["ok"] is False


@pytest.mark.parametrize("profile", ["full", "checkpoint-preflight"])
def test_profile_executes_one_repeatable_read_readonly_batch_without_sql_changes(
    monkeypatch, profile
):
    queries = validate.queries_for_profile(profile)
    calls = []

    def psql(_psql, _url, script, *, variables):
        calls.append(script)
        results, timings = [], []
        for ordinal, name in enumerate(queries):
            frame = validate._query_frame_id(ordinal, name)
            results.extend(
                [
                    validate.QUERY_RESULT_BEGIN_PREFIX + frame,
                    "metric\tvalue",
                    f"{name}\t7",
                    validate.QUERY_RESULT_END_PREFIX + frame,
                ]
            )
            timings.extend(
                [
                    validate.QUERY_TIMING_BEGIN_PREFIX + frame,
                    "Time: 1.000 ms",
                    validate.QUERY_TIMING_END_PREFIX + frame,
                ]
            )
        Path(variables["CRE_VALIDATION_RESULTS"]).write_text("\n".join(results) + "\n")
        return SimpleNamespace(
            returncode=0, stdout="\n".join(timings) + "\n", stderr=""
        )

    monkeypatch.setattr(validate, "_run_psql_script", psql)
    rows, timings, _stderr = validate.run_queries_with_timings(
        "synthetic-psql", "postgres://NO-CONNECTION", queries
    )
    assert len(calls) == 1
    script = calls[0]
    assert (
        script.count("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;")
        == 1
    )
    assert script.count("ROLLBACK;") == 1
    assert "COMMIT;" not in script
    assert set(rows) == set(timings) == set(queries)
    for name, sql in queries.items():
        assert script.count(sql) == 1
        assert rows[name] == [{"metric": name, "value": "7"}]


@pytest.mark.parametrize("profile", [None, "checkpoint-preflight"])
def test_validator_cli_defaults_full_and_labels_reduced_without_full_diagnostics(
    monkeypatch, capsys, profile
):
    calls = []
    monkeypatch.setattr(
        validate,
        "load_db_url",
        lambda _: ("postgres://SENTINEL", "/private/example.env"),
    )
    monkeypatch.setattr(validate, "find_psql", lambda: "synthetic-psql")

    def run(_psql, _url, queries):
        calls.append(queries)
        return {name: [] for name in queries}, {}, ""

    monkeypatch.setattr(validate, "run_queries_with_timings", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cre_validate.py", "--format", "json"]
        + (["--profile", profile] if profile else []),
    )
    validate.main()
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert report["validation_profile"] == (profile or "full")
    if profile:
        assert set(report["queries"]) == REQUIRED
        assert "historical_primary_child_conflicts" not in report
        assert "not full validation" in validate.render_markdown(report)
        validate.require_checkpoint_preflight_report(report, expected_profile=profile)
    else:
        assert set(report["queries"]) == set(validate.QUERIES)
        assert "historical_primary_child_conflicts" in report


def test_validator_never_writes_incomplete_declared_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(
        validate,
        "load_db_url",
        lambda _: ("postgres://SENTINEL", "/private/example.env"),
    )
    monkeypatch.setattr(validate, "find_psql", lambda: "synthetic-psql")
    monkeypatch.setattr(
        validate, "run_queries_with_timings", lambda *_: ({"source_counts": []}, {}, "")
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["cre_validate.py", "--profile", "checkpoint-preflight", "--out", str(output)],
    )
    with pytest.raises(ValueError, match="incomplete"):
        validate.main()
    assert not output.exists()


def configure_checkpoint(monkeypatch, tmp_path, before):
    calls, collected = [], []
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda _: {"algorithm": "sha256", "value": "b" * 64},
    )
    monkeypatch.setattr(
        refresh, "checkpoint_lock_dir", lambda _: tmp_path / "private.lock"
    )
    monkeypatch.setattr(refresh, "capture_runtime_configuration", lambda _: None)
    monkeypatch.setattr(
        refresh, "observe_resource_usage", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setattr(refresh, "save_report", lambda _: True)
    monkeypatch.setattr(refresh.HostMemoryGuard, "preflight", lambda _: 1)
    monkeypatch.setattr(refresh.HostMemoryGuard, "start", lambda _: None)
    monkeypatch.setattr(refresh.HostMemoryGuard, "stop", lambda _: None)
    monkeypatch.setattr(refresh, "run_cpu_guard_preflight", lambda _: 10)
    monkeypatch.setattr(refresh.HostCpuGuard, "start", lambda _: None)
    monkeypatch.setattr(refresh.HostCpuGuard, "stop", lambda _: None)

    def command(argv, _log, **_kwargs):
        calls.append(argv)
        if "--out" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(before))
        return 0

    def collect(*_args, **_kwargs):
        collected.append(True)
        return []

    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(refresh, "prepare_sources_cohort", collect)
    monkeypatch.setattr(refresh, "run_aggregate_gate", lambda *_: None)
    monkeypatch.setattr(refresh, "ingest_admitted_sources", lambda *_: None)
    monkeypatch.setattr(refresh, "run_final_validation", lambda *_: None)
    argv = ["--out-root", str(tmp_path / "runs"), "--sources", "svn"]
    return argv, calls, collected


@pytest.mark.parametrize("missing", [None, "child_counts"])
def test_checkpoint_selects_and_requires_reduced_preflight_before_collection(
    monkeypatch, tmp_path, missing
):
    before = reduced()
    if missing:
        del before["queries"][missing]
    argv, calls, collected = configure_checkpoint(monkeypatch, tmp_path, before)
    if missing:
        with pytest.raises(refresh.GlobalStageError, match="profile is incomplete"):
            refresh.main(argv)
        assert not collected
    else:
        assert refresh.main(argv) == 0
        assert collected == [True]
        (path,) = (tmp_path / "runs").glob("*/manifest.json")
        assert (
            json.loads(path.read_text())["preflight"]["validation_profile"]
            == "checkpoint-preflight"
        )
    validate_call = calls[1]
    assert validate_call[validate_call.index("--profile") + 1] == "checkpoint-preflight"


def test_real_main_resume_reuses_hash_bound_legacy_full_snapshot_without_revalidation(
    monkeypatch, tmp_path
):
    argv, calls, collected = configure_checkpoint(monkeypatch, tmp_path, reduced())
    assert refresh.main(argv) == 0
    (manifest_path,) = (tmp_path / "runs").glob("*/manifest.json")
    run_dir = manifest_path.parent
    before_path = run_dir / "pre-validation.json"
    # Model the previously published full snapshot/manifest, with no profile
    # metadata. Actual main --resume must use its byte hash, not regenerate it.
    before_path.write_text(json.dumps(snapshot()))
    original_bytes = before_path.read_bytes()
    original_hash = refresh.sha256_file(before_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["preflight"]["validation_sha256"] = original_hash
    manifest["preflight"].pop("validation_profile", None)
    manifest["preflight"].pop("validation_audit_scope", None)
    manifest_path.write_text(json.dumps(manifest))
    calls.clear()
    collected.clear()
    assert refresh.main([*argv, "--resume", str(run_dir)]) == 0
    assert not any("cre_validate.py" in command for command in calls)
    assert collected == [True]
    assert before_path.read_bytes() == original_bytes
    resumed = json.loads(manifest_path.read_text())
    assert resumed["preflight"]["validation_sha256"] == original_hash
    assert resumed["preflight"]["validation_profile"] == "full"
    assert (
        resumed["preflight"]["validation_audit_scope"]
        == validate.VALIDATION_AUDIT_SCOPES["full"]
    )


def test_post_ingest_and_recovery_builder_remain_full(tmp_path):
    argv = refresh.build_validate_argv(tmp_path / "validation.json", None)
    assert "--profile" not in argv
    recovery = refresh.build_validate_argv(
        tmp_path / "recovery.json",
        None,
        expected_artifact_run_key="ingest:v1:" + "a" * 64,
    )
    assert "--expected-artifact-run-key" in recovery and "--profile" not in recovery
    with pytest.raises(ValueError, match="full"):
        refresh.build_validate_argv(
            tmp_path / "bad.json",
            None,
            expected_artifact_run_key="ingest:v1:" + "a" * 64,
            profile="checkpoint-preflight",
        )
