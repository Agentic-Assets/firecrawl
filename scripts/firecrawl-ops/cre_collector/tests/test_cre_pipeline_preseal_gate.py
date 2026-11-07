"""Real pipeline/unit gates with closed collector and database command boundaries."""

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_notification_summary as notification_tests
from test_cre_checkpoint_refresh import (
    ATTEMPT,
    freshness_generation_row,
    lifecycle_schema_contract_validation,
    strict_artifact,
)
from test_notification_summary import pipeline_evidence, read_pipeline

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES

evidence = notification_tests.evidence

SHA = "a" * 40
TARGET = "d" * 64
SOURCES = ("cbre", "svn")


@pytest.fixture
def closed_pipeline(tmp_path, monkeypatch, request):
    """Keep real artifact/gate/unit/audit/readback code; replace external commands."""
    root = tmp_path / "2026-07-29T120000Z"
    root.mkdir()
    sources = getattr(request, "param", SOURCES)
    manifest = refresh.new_manifest(
        root,
        git_sha=SHA,
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=1,
        pipeline=True,
        source_workers=1,
        database_target={"algorithm": "sha256", "value": TARGET},
    )
    manifest["started_at"] = ATTEMPT
    events = []
    gates = {key: ["ok"] for key in sources}
    if "cbre" in gates:
        gates["cbre"] = ["hold"]
    gate_calls = {key: 0 for key in sources}
    mutations = {}
    commands = []
    real_command = refresh.run_command
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *args: None)
    def start(run, owner, source, **kwargs):
        path = run / "sources" / (source + ".json.tmp")
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                strict_artifact(
                    source,
                    detail_scope="detail_page" if source == "essex-realty" else "authoritative_inventory_feed",
                    generation=run.name,
                    generation_started_at=ATTEMPT,
                )
            )
        )
        path.chmod(0o600)
        checkpoint = owner["sources"][source]
        attempt = {"number": 1, "started_at": ATTEMPT, "rc": 0}
        checkpoint["attempts"].append(attempt)
        checkpoint["state"] = "collecting"
        events.append(("collect", source))
        return SimpleNamespace(
            source=source,
            process=SimpleNamespace(poll=lambda: 0),
            tmp_artifact=path,
            attempt=attempt,
        )

    def publish(run, owner, item, **kwargs):
        path = run / "sources" / (item.source + ".json")
        os.replace(item.tmp_artifact, path)
        stat = path.stat()
        checkpoint = owner["sources"][item.source]
        checkpoint["candidate"] = {
            "path": str(path.relative_to(run)),
            "bytes": stat.st_size,
            "attempt_started_at": ATTEMPT,
            "file_identity": [
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            ],
        }
        checkpoint["state"] = "candidate"
        checkpoint["attempts"][-1]["finished_at"] = "2026-07-29T12:01:00+00:00"
        return True

    def command(argv, log, **kwargs):
        script = Path(argv[1]).name
        if script == "cre_pipeline_artifact_worker.py":
            return real_command(argv, log, **kwargs)
        commands.append(list(argv))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("closed command boundary\n")
        if script == "cre_essex_identity.py":
            from cre_essex_identity import projection_digest, stage_essex_identity

            if mutations.get("identity_error"):
                return 1
            rows = stage_essex_identity(json.loads(Path(argv[argv.index("--artifact") + 1]).read_bytes()))
            report = {
                "schema": "cre-essex-preseal-identity-v1", "source": "essex-realty",
                "artifact_sha256": argv[argv.index("--artifact-sha256") + 1],
                "generation_id": argv[argv.index("--generation") + 1],
                "producer_sha": argv[argv.index("--producer") + 1],
                "database_target_sha256": argv[argv.index("--target-sha256") + 1],
                "staged_count": len(rows), "staged_sha256": projection_digest(rows),
                "conflicts": mutations.get("identity_conflicts", 2),
                "verdict": "hold" if mutations.get("identity_conflicts", 2) else "clear",
            }
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(report))
            events.append(("identity", "essex-realty"))
            return 0
        if script == "cre_gate.py":
            assert argv[argv.index("--expected-db-target-sha256") + 1] == TARGET
            assert "--update-baseline" not in argv
            inputs = [
                Path(argv[i + 1]) for i, flag in enumerate(argv) if flag == "--in"
            ]
            per_source = {}
            for path in inputs:
                key = path.stem
                offset = gate_calls[key]
                verdict = gates[key][min(offset, len(gates[key]) - 1)]
                gate_calls[key] += 1
                events.append(("gate", key))
                per_source[key] = {
                    "current_active": 2,
                    "baseline_median": None if verdict == "first_seen" else 100,
                    "effective_floor": 100,
                    "floor_policy": None,
                    "verdict": verdict,
                    "reason": None if verdict == "ok" else "synthetic known refusal",
                    "mark_missing_safe": verdict == "ok",
                }
            holds = [
                key for key, info in per_source.items() if info["verdict"] == "hold"
            ]
            report = {
                "generated_for_artifact": str(inputs[0])
                if len(inputs) == 1
                else list(map(str, inputs)),
                "per_source": per_source,
                "per_brokerage": {},
                "summary": {
                    "mode": "live",
                    "torow_errors": 0,
                    "baseline_updated": False,
                    "baseline_update_sources": [],
                    "hold_sources": holds,
                    "mark_missing_safe_brokerages": [],
                },
            }
            if "gate_report" in mutations:
                mutations["gate_report"](report)
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(report))
            if "artifact_after_gate" in mutations:
                mutations["artifact_after_gate"](inputs[0])
            return mutations.get("gate_rc", 2 if holds else 0)
        if script == "cre_ingest.py":
            source = Path(argv[argv.index("--in") + 1]).stem
            if "--dry-run" not in argv:
                events.append(("write", source))
                return mutations.get("ingest_rc", 0)
            events.append(("dryrun", source))
            return mutations.get("dryrun_rc", 0)
        assert script == "cre_validate.py", argv
        output = Path(argv[argv.index("--out") + 1])
        if "--profile" in argv:
            report = lifecycle_schema_contract_validation()
        else:
            report = {
                "ok": True,
                "validation_profile": FULL_VALIDATION_PROFILE,
                "audit_scope": VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
                "queries": {key: [] for key in QUERIES},
                "historical_primary_child_conflicts": [],
            }
            written = {source for event, source in events if event == "write"}
            report["queries"]["freshness_generations"] = [
                freshness_generation_row(source, generation_id=root.name)
                for source in written
            ]
            report["queries"]["inventory_generation_fingerprints"] = [
                {
                    "source_key": source,
                    "row_count": "2" if source in written else "0",
                    "active_row_count": str(2 * len(written)),
                    "classified_row_count": str(2 * len(written)),
                    "unclassified_row_count": "0",
                    "max_row_updated_at": "2026-07-29T12:02:00Z",
                    "max_observation_at": "2026-07-29T12:01:00Z",
                }
                for source in refresh.SOURCE_KEYS
            ]
            if "validation" in mutations:
                mutations["validation"](report)
            events.append(("full_audit", tuple(written)))
        output.write_text(json.dumps(report))
        return 0

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", publish)
    monkeypatch.setattr(
        refresh,
        "_terminate_cohort_processes",
        lambda items: events.append(("cleanup", len(items))),
    )
    monkeypatch.setattr(refresh, "run_command", command)

    def run(*, attempts=1, workers=1):
        return pipeline.run_bounded_pipeline(
            root,
            manifest,
            sources,
            page_cap=400,
            concurrency=1,
            attempts_this_run=attempts,
            env_file=None,
            source_workers=workers,
        )

    return SimpleNamespace(
        root=root,
        manifest=manifest,
        gates=gates,
        mutations=mutations,
        events=events,
        commands=commands,
        run=run,
        gate_calls=gate_calls,
    )


@pytest.mark.parametrize("closed_pipeline", [("essex-realty", "svn")], indirect=True)
def test_essex_history_hold_keeps_healthy_sibling_running(closed_pipeline, monkeypatch):
    case = closed_pipeline
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _env: {"value": TARGET})
    assert case.run(attempts=3) == ["essex-realty"]
    assert case.manifest["sources"]["essex-realty"]["state"] == "gate_blocked"
    assert case.events.count(("identity", "essex-realty")) == 1
    assert case.events.count(("collect", "essex-realty")) == 1
    assert ("write", "essex-realty") not in case.events
    assert ("dryrun", "essex-realty") not in case.events
    assert ("write", "svn") in case.events
    assert case.manifest["sources"]["svn"]["readback"]["ok"] is True
    assert case.manifest["pipeline"]["units"][0]["state"] == "verified"


@pytest.mark.parametrize("closed_pipeline", [("essex-realty", "svn")], indirect=True)
def test_essex_clear_runs_one_identity_probe_and_continues(closed_pipeline, monkeypatch):
    case = closed_pipeline
    case.mutations["identity_conflicts"] = 0
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _env: {"value": TARGET})
    assert case.run() == []
    assert case.events.count(("identity", "essex-realty")) == 1
    assert ("write", "essex-realty") in case.events
    assert ("write", "svn") in case.events


@pytest.mark.parametrize("closed_pipeline", [("essex-realty", "svn")], indirect=True)
def test_essex_identity_query_error_is_global(closed_pipeline, monkeypatch):
    case = closed_pipeline
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _env: {"value": TARGET})
    case.mutations["identity_error"] = True
    with pytest.raises(refresh.GlobalStageError, match="identity probe failed"):
        case.run()
    assert ("write", "essex-realty") not in case.events
    assert case.manifest["sources"]["essex-realty"].get("state") != "gate_blocked"


def test_preseal_hold_writes_nothing_while_other_unit_gets_full_audit_and_readback(
    closed_pipeline,
):
    case = closed_pipeline
    assert case.run(attempts=3) == ["cbre"]
    held = case.manifest["sources"]["cbre"]
    assert held["state"] == "gate_blocked"
    assert ("write", "cbre") not in case.events
    assert ("dryrun", "cbre") not in case.events
    assert case.events.count(("collect", "cbre")) == 1
    assert ("write", "svn") in case.events
    assert case.manifest["sources"]["svn"]["readback"]["ok"] is True
    units = case.manifest["pipeline"]["units"]
    assert len(units) == 1 and units[0]["sources"] == ["svn"]
    assert units[0]["state"] == "verified"
    assert units[0]["unit_id"] == "unit-0001"
    report = json.loads((case.root / units[0]["validation_path"]).read_text())
    assert set(report["queries"]) == set(QUERIES) and len(report["queries"]) == 16
    assert case.manifest["pipeline"]["status"] == "partial"
    assert (case.root / "sources/cbre.json").is_file()
    gate = held["gate"]
    assert (
        gate["sha256"]
        == hashlib.sha256((case.root / gate["path"]).read_bytes()).hexdigest()
    )
    assert gate["preseal_binding"] == {
        "collector_git_sha": SHA,
        "database_target_sha256": TARGET,
        "generation_id": case.root.name,
        "artifact_sha256": held["artifact"]["sha256"],
    }


@pytest.mark.parametrize("current", [1365, 1367])
@pytest.mark.parametrize("closed_pipeline", [("cbre", "svn"), ("svn", "cbre")], indirect=True)
def test_count_shortfall_uses_partial_gates_and_keeps_peer(closed_pipeline, current):
    import cre_checkpoint_series as series
    from cre_gate import verdict_for

    case = closed_pipeline
    verdict, reason, missing_safe, median = verdict_for(
        current, False, None, {"median": 1953}, 100, 0.30
    )
    assert verdict == "hold"

    def count_hold(report):
        info = report["per_source"].get("cbre")
        if info is not None:
            info.update(current_active=current, baseline_median=median,
                        verdict=verdict, reason=reason, mark_missing_safe=missing_safe)

    case.mutations["gate_report"] = count_hold
    assert case.run() == []
    assert ("write", "cbre") in case.events
    assert ("write", "svn") in case.events
    assert case.gate_calls["cbre"] >= 2
    result = case.manifest["pipeline"]["source_results"]["cbre"]
    unit = next(unit for unit in case.manifest["pipeline"]["units"] if unit["unit_id"] == result["unit_id"])
    assert unit["aggregate_gate"]["baseline_advisory_holds"] == ["cbre"]
    assert result["partial"] is True and result["whole_source_coverage"] is False
    assert result["baseline_hold_reason"] == reason
    assert case.manifest["pipeline"]["status"] == "partial"
    assert series.count_hold_pipeline_member(case.manifest, "cbre")
    assert not series.count_hold_pipeline_member(case.manifest, "svn")
    tampered = copy.deepcopy(case.manifest)
    tampered["pipeline"]["source_results"]["cbre"]["baseline_hold_reason"] = "different"
    assert not series.count_hold_pipeline_member(tampered, "cbre")
    tampered = copy.deepcopy(case.manifest)
    next(u for u in tampered["pipeline"]["units"] if u["unit_id"] == result["unit_id"])["aggregate_gate"]["baseline_advisory_holds"] = []
    assert not series.count_hold_pipeline_member(tampered, "cbre")
    assert case.manifest["config"]["admit_baseline_hold_additively"] is False
    source_gate = json.loads((case.root / case.manifest["sources"]["cbre"]["gate"]["path"]).read_text())
    info = source_gate["per_source"]["cbre"]
    assert info["raw_verdict"] == "hold"
    assert info["raw_reason"] == reason
    assert info["verdict"] == "ok_additive_coverage_hold"
    assert info["mark_missing_safe"] is False
    assert source_gate["scope"]["whole_source_coverage"] is False
    assert case.manifest["scope"]["whole_source_coverage"] is False
    assert not any("--mark-missing" in command or "--activate-status" in command for command in case.commands)


def test_count_shortfall_threshold_and_source_error_precedence():
    from cre_gate import verdict_for

    assert verdict_for(1368, False, None, {"median": 1953}, 100, 0.30)[0] == "ok"
    verdict, reason, _, _ = verdict_for(1365, True, "offline", {"median": 1953}, 100, 0.30)
    assert verdict == "hold"
    assert not refresh.subset_gate_can_admit({"verdict": verdict, "reason": reason})


@pytest.mark.parametrize("verdicts", [("ok", "hold"), ("hold", "ok")])
def test_count_shortfall_survives_source_aggregate_baseline_drift(closed_pipeline, verdicts):
    import cre_checkpoint_series as series

    case = closed_pipeline
    case.gates["cbre"] = list(verdicts)
    reason = "current_active 1365 below 70% of baseline median 1953 (threshold 1367)"

    def count_hold(report):
        info = report["per_source"].get("cbre")
        if info is not None and info["verdict"] == "hold":
            info.update(current_active=1365, baseline_median=1953, reason=reason)

    case.mutations["gate_report"] = count_hold
    assert case.run() == []
    result = case.manifest["pipeline"]["source_results"]["cbre"]
    assert result["baseline_hold_source_reason"] == (reason if verdicts[0] == "hold" else None)
    assert result["baseline_hold_aggregate_reason"] == (reason if verdicts[1] == "hold" else None)
    assert result["baseline_hold_reason"] == reason
    assert series.count_hold_pipeline_member(case.manifest, "cbre")
    assert case.manifest["pipeline"]["status"] == "partial"
    assert refresh.has_admitted_count_hold(case.manifest)
    assert ("write", "svn") in case.events
    unit = next(unit for unit in case.manifest["pipeline"]["units"] if "cbre" in unit["sources"])
    report = json.loads((case.root / unit["aggregate_gate"]["path"]).read_text())
    assert report["per_source"]["cbre"]["mark_missing_safe"] is False
    assert "cbre" not in report["summary"]["mark_missing_safe_brokerages"]


def test_count_shortfall_handled_pipeline_resumes_without_replay(tmp_path, monkeypatch):
    import cre_checkpoint_series as series

    config = series.series_config(
        sources=["cbre", "svn"], page_cap=400, concurrency=1, attempts_per_source=1,
        max_resume_age_hours=24, max_host_cpu_percent=90, cpu_sustain_seconds=30,
        cpu_sample_seconds=2, nice=15, pipeline=True,
    )
    parent = series.new_manifest(tmp_path, git_sha=SHA, config=config)
    for key, row in parent["sources"].items():
        row.update(state="qualified_partial_subset" if key == "cbre" else "complete",
                   checkpoint_run="runs/2026-07-29T120000Z", attempts=[{}])
    reason = "current_active 1365 below 70% of baseline median 1953 (threshold 1367)"
    child = {
        "status": "additive_scope_complete_coverage_hold",
        "config": {"additive": True, "mark_missing": False, "status_activation": False},
        "sources": {"cbre": {"gate": {"verdict": "ok_additive_coverage_hold",
            "raw_verdict": "hold", "raw_reason": reason, "mark_missing_safe": False,
            "admission_scope": "additive_coverage_hold"}}},
        "pipeline": {"source_results": {"cbre": {"unit_id": "unit-0001",
            "qualification": "additive_coverage_hold", "partial": True,
            "whole_source_coverage": False, "baseline_hold_reason": reason,
            "baseline_hold_source_reason": reason, "baseline_hold_aggregate_reason": reason}},
            "units": [{"unit_id": "unit-0001", "sources": ["cbre"],
                "aggregate_gate": {"baseline_advisory_holds": ["cbre"],
                    "baseline_advisory_reasons": {"cbre": reason},
                    "mark_missing_safe_brokerages": [], "non_ok_sources": []}}]},
    }
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **kw: (tmp_path / "manifest.json", child))
    monkeypatch.setattr(series, "pipeline_source_qualified", lambda *a, **kw: True)
    monkeypatch.setattr(series, "_pipeline_has_unresolved", lambda *a: False)
    monkeypatch.setattr(series, "document_incomplete", lambda *a: False)
    monkeypatch.setattr(series.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not replay"))
    assert series.run_pipeline_series(tmp_path, parent, env_file=None) == 0
    assert parent["status"] == "qualified_partial_subset"
    assert all(len(row["attempts"]) == 1 for row in parent["sources"].values())


def test_count_shortfall_with_conversion_errors_never_enters_a_write_unit(closed_pipeline):
    case = closed_pipeline

    def corrupt_conversion(report):
        if "cbre" in report["per_source"]:
            report["per_source"]["cbre"]["reason"] = "current_active 1365 below 70% of baseline median 1953 (threshold 1367)"
            report["summary"]["torow_errors"] = 1

    case.mutations["gate_report"] = corrupt_conversion
    with pytest.raises(refresh.GlobalStageError, match="conversion errors"):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize("field,value", [
    ("additive", False), ("mark_missing", True), ("status_activation", True),
    ("additive", None), ("mark_missing", None), ("status_activation", None),
])
def test_count_shortfall_requires_explicit_safe_write_policy(closed_pipeline, field, value):
    case = closed_pipeline
    case.manifest["config"][field] = value
    assert not refresh.gate_verdict_is_admitted(case.manifest, "ok_additive_coverage_hold", source="cbre")
    assert not refresh.additive_coverage_hold_enabled(case.manifest["config"])


def test_all_preseal_holds_leave_no_database_units_or_writes(closed_pipeline):
    case = closed_pipeline
    case.gates["svn"] = ["first_seen"]
    assert case.run() == ["cbre", "svn"]
    assert case.manifest["sources"]["svn"]["state"] == "baseline_seed_required"
    assert case.manifest["pipeline"]["units"] == []
    assert case.manifest["pipeline"]["active_unit"] is None
    assert case.manifest["pipeline"]["status"] == "partial"
    assert not list((case.root / "pipeline").glob("*/before.json"))
    assert not list((case.root / "pipeline").glob("*/validation.json"))
    assert not list((case.root / "pipeline").glob("unit-*"))
    assert not any(
        event[0] in {"write", "dryrun", "full_audit"} for event in case.events
    )


@pytest.mark.parametrize("outcome", ["mixed", "all_held", "later_global"])
def test_real_coordinator_membership_remains_readable_as_partial_summary(
    closed_pipeline, evidence, outcome
):
    """Real coordinator layout plus the reader's independently bound private proof."""
    case = closed_pipeline
    primary = "svn"
    if outcome == "all_held":
        case.gates["svn"] = ["first_seen"]
    elif outcome == "later_global":
        primary = "cbre"
        case.gates.update(cbre=["ok"], svn=["unknown"])
    if outcome == "later_global":
        with pytest.raises(refresh.GlobalStageError):
            case.run()
    else:
        case.run()
    produced = case.manifest["pipeline"]
    assert all(unit["sources"] for unit in produced["units"])
    assert not list((case.root / "pipeline").glob("unit-0002"))
    setup = pipeline_evidence(
        evidence,
        primary_source=primary,
        second_source="cbre" if primary == "svn" else "svn",
    )
    child, parent = setup["child"], setup["parent"]
    # Keep the strict reader fixture's separately hash-bound audit/readback.
    # Only coordinator membership/state is transported between test generations.
    audit = child["pipeline"]["units"][0]
    child["pipeline"]["units"] = [
        {
            **unit,
            "validation_path": audit["validation_path"],
            "validation_sha256": audit["validation_sha256"],
        }
        for unit in produced["units"]
    ]
    child["pipeline"]["status"] = produced["status"]
    child["pipeline"]["commit_outcome_unknown"] = produced.get(
        "commit_outcome_unknown", False
    )
    child["pipeline"]["active_unit"] = produced["active_unit"]
    for source, checkpoint in case.manifest["sources"].items():
        if produced["source_results"][source]["state"] != "verified":
            child["sources"][source]["state"] = checkpoint["state"]
            child["pipeline"]["source_results"][source] = produced["source_results"][
                source
            ]
            parent["sources"][source]["state"] = "failed_source"
            parent["sources"][source].pop("pipeline_result", None)
    evidence["save"]()
    result = read_pipeline(evidence, [primary, setup["source2"]])
    assert result["verified"] is True and result["complete"] is False
    expected = [] if outcome == "all_held" else [primary]
    assert [row["source_key"] for row in result["per_source_summaries"]] == expected
    assert set(result["failed_source_keys"]) == set(SOURCES) - set(expected)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: report.update(generated_for_artifact="/foreign.json"),
        lambda report: report["per_source"].update(
            foreign=copy.deepcopy(report["per_source"]["cbre"])
        ),
        lambda report: report["per_source"]["cbre"].update(verdict="unknown"),
        lambda report: report["summary"].update(mode="dry_run"),
        lambda report: report["summary"].update(torow_errors=1),
        lambda report: report["summary"].update(baseline_updated=True),
        lambda report: report["summary"].update(baseline_update_sources=["cbre"]),
    ],
)
def test_uncertain_preseal_gate_report_stops_every_lane(closed_pipeline, mutate):
    case = closed_pipeline
    case.mutations["gate_report"] = mutate
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert not any(event[0] == "write" for event in case.events)
    assert case.manifest["pipeline"]["status"] == "reconciliation_required"


@pytest.mark.parametrize("verdict", ["hold", "first_seen"])
@pytest.mark.parametrize(
    "field",
    [
        "current_active",
        "baseline_median",
        "effective_floor",
        "floor_policy",
        "verdict",
        "reason",
        "mark_missing_safe",
    ],
)
def test_incomplete_standard_refusal_stops_globally(closed_pipeline, verdict, field):
    case = closed_pipeline
    case.gates["cbre"] = [verdict]
    case.mutations["gate_report"] = lambda report: (
        report["per_source"].get("cbre", {}).pop(field, None)
    )
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_active", True),
        ("current_active", -1),
        ("baseline_median", False),
        ("effective_floor", True),
        ("floor_policy", []),
        ("reason", None),
    ],
)
def test_malformed_standard_refusal_stops_globally(closed_pipeline, field, value):
    case = closed_pipeline
    case.mutations["gate_report"] = lambda report: (
        report["per_source"].get("cbre", {}).update({field: value})
    )
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize(
    "verdict,rc", [("hold", 0), ("hold", 1), ("hold", True), ("first_seen", 2)]
)
def test_refusal_requires_exact_expected_command_result(closed_pipeline, verdict, rc):
    case = closed_pipeline
    case.gates["cbre"] = [verdict]
    case.mutations["gate_rc"] = rc
    with pytest.raises(refresh.GlobalStageError):
        case.run()


def test_artifact_change_during_preseal_gate_stops_every_lane(closed_pipeline):
    case = closed_pipeline
    case.mutations["artifact_after_gate"] = lambda path: path.write_text(
        path.read_text() + " "
    )
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


def test_report_change_after_preseal_digest_stops_every_lane(
    closed_pipeline, monkeypatch
):
    case = closed_pipeline
    original = refresh._load_json
    reads = 0

    def load(path):
        nonlocal reads
        value = original(path)
        if path.parent.name == "gates" and path.name == "cbre.json":
            reads += 1
            if reads == 2:
                path.write_text(path.read_text() + " ")
        return value

    monkeypatch.setattr(refresh, "_load_json", load)
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert reads == 2
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize(("verdict", "expected"), [("hold", "gate_blocked"), ("first_seen", "baseline_seed_required")])
def test_hold_during_sibling_preparation_skips_source_before_sealing(closed_pipeline, monkeypatch, verdict, expected):
    case = closed_pipeline
    case.gates["cbre"] = ["ok"]
    real_prepare = pipeline.prepare_candidate

    def prepare(run_dir, manifest, source):
        result = real_prepare(run_dir, manifest, source)
        case.events.append(("prepared", source))
        if source == "svn":
            case.gates["cbre"] = [verdict]
        return result

    monkeypatch.setattr(pipeline, "prepare_candidate", prepare)
    assert case.run(workers=2) == ["cbre"]
    assert case.events.index(("prepared", "svn")) < case.events.index(("gate", "cbre"))
    assert case.manifest["sources"]["cbre"]["state"] == expected
    assert ("write", "cbre") not in case.events
    assert ("write", "svn") in case.events
    assert case.manifest["sources"]["svn"]["readback"]["ok"] is True
    units = case.manifest["pipeline"]["units"]
    assert len(units) == 1 and units[0]["sources"] == ["svn"]
    assert case.events.count(("gate", "cbre")) == 1


def test_later_aggregate_hold_still_stops_before_any_write(closed_pipeline):
    case = closed_pipeline
    case.gates["cbre"] = ["ok", "hold"]
    with pytest.raises(refresh.RefreshError, match="aggregate coverage gate"):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


def test_malformed_final_gate_refusal_remains_global_before_any_write(closed_pipeline):
    case = closed_pipeline
    case.gates["cbre"] = ["hold"]

    def corrupt(report):
        report["per_source"]["cbre"]["reason"] = None

    case.mutations["gate_report"] = corrupt
    with pytest.raises(refresh.GlobalStageError, match="unrecognized pre-seal source gate refusal"):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize("fault", ["dryrun_rc", "ingest_rc"])
def test_database_or_dryrun_failure_is_never_demoted(closed_pipeline, fault):
    case = closed_pipeline
    case.gates["cbre"] = ["ok"]
    case.mutations[fault] = 1
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert case.manifest["pipeline"]["status"] == "reconciliation_required"


@pytest.mark.parametrize("fault", ["missing_query", "wrong_generation"])
def test_eligible_unit_still_rejects_incomplete_audit_or_foreign_readback(
    closed_pipeline, fault
):
    case = closed_pipeline
    if fault == "missing_query":
        case.mutations["validation"] = lambda report: report["queries"].pop(
            "duplicates"
        )
    else:
        case.mutations["validation"] = lambda report: report["queries"][
            "freshness_generations"
        ][0].update(generation_id="foreign")
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert ("write", "cbre") not in case.events
    assert (
        case.manifest["pipeline"]["source_results"].get("svn", {}).get("state")
        != "verified"
    )


def test_retained_terminal_hold_releases_pending_budget_without_deleting_evidence(
    closed_pipeline, monkeypatch
):
    case = closed_pipeline
    budget = replace(pipeline.PipelineBudget(), soft_bytes=1, output_reservation=1)
    monkeypatch.setattr(pipeline, "PipelineBudget", lambda: budget)
    assert case.run() == ["cbre"]
    assert ("write", "svn") in case.events
    assert (case.root / "sources/cbre.json").stat().st_size > budget.soft_bytes
    assert (case.root / "gates/cbre.json").is_file()


def test_retained_terminal_hold_does_not_bypass_actual_free_disk_guard(
    closed_pipeline, monkeypatch
):
    case = closed_pipeline
    budget = replace(pipeline.PipelineBudget(), soft_bytes=1, output_reservation=1)
    monkeypatch.setattr(pipeline, "PipelineBudget", lambda: budget)

    def disk(_path):
        held = case.manifest["sources"]["cbre"].get("state") == "gate_blocked"
        return SimpleNamespace(free=budget.min_free_bytes if held else 10 * 1024**3)

    monkeypatch.setattr(pipeline.shutil, "disk_usage", disk)
    with pytest.raises(refresh.GlobalStageError, match="disk/output admission"):
        case.run()
    assert (case.root / "sources/cbre.json").is_file()
    assert ("write", "svn") not in case.events


@pytest.mark.parametrize("forged_raw_hold", [False, True])
def test_standard_ok_with_hold_exit_code_is_uncertain(closed_pipeline, forged_raw_hold):
    case = closed_pipeline
    case.gates["cbre"] = ["ok"]
    case.mutations["gate_rc"] = 2
    if forged_raw_hold:
        case.mutations["gate_report"] = lambda report: report["per_source"][
            "cbre"
        ].update(raw_verdict="hold")
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize("field", ["run_id", "collector_git_sha", "database_target"])
def test_manifest_identity_change_during_gate_stops_every_lane(closed_pipeline, field):
    case = closed_pipeline
    target = case.manifest["preflight"] if field == "database_target" else case.manifest
    case.mutations["artifact_after_gate"] = lambda _path: target.update(
        {
            field: {"algorithm": "sha256", "value": "e" * 64}
            if field == "database_target"
            else "foreign"
        }
    )
    with pytest.raises(refresh.GlobalStageError):
        case.run()
    assert not any(event[0] == "write" for event in case.events)


@pytest.mark.parametrize(
    "source,subset", [("interra-realty", False), ("daum-commercial", True)]
)
@pytest.mark.parametrize("proof_refused", [False, True])
def test_preseal_accepts_maintained_admission_rewrite_of_raw_hold(
    tmp_path, monkeypatch, source, subset, proof_refused
):
    root = tmp_path / "run"
    (root / "sources").mkdir(parents=True)
    manifest = refresh.new_manifest(
        root,
        git_sha=SHA,
        git_dirty=False,
        sources=(source,),
        page_cap=400,
        concurrency=1,
        pipeline=True,
        database_target={"algorithm": "sha256", "value": TARGET},
        admit_current_inventory_sources=[] if subset else [source],
        admit_daum_verified_subset=subset,
        daum_subset_policy="tenure-and-empty-title-v2" if subset else "tenure-only-v1",
    )
    artifact = root / "sources" / (source + ".json")
    # DAUM's source gate inspects the already-validated artifact for fallback
    # witnesses, so the closed fixture must remain valid JSON.
    artifact.write_text(json.dumps({"sources": []}) if subset else "closed already-validated artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest["sources"][source]["artifact"] = {
        "path": str(artifact.relative_to(root)),
        "sha256": digest,
    }
    report = {
        "generated_for_artifact": str(artifact),
        "per_source": {source: {"verdict": "hold", "mark_missing_safe": False}},
        "summary": {
            "mode": "live",
            "torow_errors": 0,
            "baseline_updated": False,
            "baseline_update_sources": [],
        },
    }
    monkeypatch.setattr(refresh, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *args: None)

    def gate_command(argv, _log, **kwargs):
        assert "--expected-db-target-sha256" in argv and TARGET in argv
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(report))
        return 2

    def admission_worker(_root, action, arguments):
        assert action == "admit"
        if proof_refused:
            raise refresh.GlobalStageError("current-inventory admission refused")
        rewritten = copy.deepcopy(arguments["report"])
        rewritten["per_source"][source].update(
            verdict=refresh.current_inventory.DAUM_SUBSET_VERDICT
            if subset
            else refresh.current_inventory.VERDICT,
            raw_verdict="hold",
            reason="closed admitted proof",
        )
        path = root / "rewritten-gate.json"
        path.write_text(json.dumps(rewritten))
        return {
            "report_path": str(path),
            "sha256": refresh.sha256_file(path),
            "scope": {
                "kind": "collector_registry_verified_subset"
                if subset
                else "collector_registry_additive_current_inventory",
                "whole_source_coverage": False,
                "source_keys": manifest["scope"]["source_keys"],
            },
        }

    monkeypatch.setattr(refresh, "run_command", gate_command)
    monkeypatch.setattr(refresh, "run_artifact_compute", admission_worker)
    monkeypatch.setattr(refresh, "artifact_compute_enabled", lambda: True)
    if subset:
        import daum.identity_sql as daum_identity

        def identity_probe(path, artifact_sha, generation, producer, target, env_file):
            assert (path, artifact_sha, generation, producer, target, env_file) == (
                artifact, digest, manifest["run_id"], SHA, TARGET, None
            )
            return {"synthetic": "identity-probe"}

        monkeypatch.setattr(daum_identity, "run_cohort_probe", identity_probe)
    if proof_refused:
        with pytest.raises(
            refresh.GlobalStageError, match="current-inventory admission refused"
        ):
            pipeline.preseal_source_gate(root, manifest, source, None)
        if subset:
            assert manifest["sources"][source]["daum_identity_probe"] == {"synthetic": "identity-probe"}
        return
    assert pipeline.preseal_source_gate(root, manifest, source, None) is True
    if subset:
        assert manifest["sources"][source]["daum_identity_probe"] == {"synthetic": "identity-probe"}
    gate = manifest["sources"][source]["gate"]
    assert gate["rc"] == 2 and gate["raw_verdict"] == "hold"
    assert gate["verdict"] != "hold"
    assert manifest["sources"][source]["state"] == "gated"
