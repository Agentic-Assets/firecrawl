"""Real pipeline/unit gates with closed collector and database command boundaries."""

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES
from test_cre_checkpoint_refresh import (
    ATTEMPT,
    freshness_generation_row,
    lifecycle_schema_contract_validation,
    strict_artifact,
)
import test_notification_summary as notification_tests
from test_notification_summary import pipeline_evidence, read_pipeline

evidence = notification_tests.evidence

SHA = "a" * 40
TARGET = "d" * 64
SOURCES = ("cbre", "svn")


@pytest.fixture
def closed_pipeline(tmp_path, monkeypatch):
    """Keep real artifact/gate/unit/audit/readback code; replace external commands."""
    root = tmp_path / "2026-07-29T120000Z"
    root.mkdir()
    manifest = refresh.new_manifest(
        root,
        git_sha=SHA,
        git_dirty=False,
        sources=SOURCES,
        page_cap=400,
        concurrency=1,
        pipeline=True,
        source_workers=1,
        database_target={"algorithm": "sha256", "value": TARGET},
    )
    manifest["started_at"] = ATTEMPT
    events = []
    gates = {"cbre": ["hold"], "svn": ["ok"]}
    gate_calls = {key: 0 for key in SOURCES}
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
                    "baseline_median": 100,
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

    def run(*, attempts=1):
        return pipeline.run_bounded_pipeline(
            root,
            manifest,
            SOURCES,
            page_cap=400,
            concurrency=1,
            attempts_this_run=attempts,
            env_file=None,
            source_workers=1,
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
    assert set(report["queries"]) == set(QUERIES) and len(report["queries"]) == 14
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


def test_preseal_pass_then_sealed_gate_refusal_still_stops_globally(closed_pipeline):
    case = closed_pipeline
    case.gates["cbre"] = ["ok", "hold"]
    with pytest.raises(refresh.GlobalStageError, match="sealed source gate refused"):
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
    artifact.write_text("closed already-validated artifact")
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
    if proof_refused:
        with pytest.raises(
            refresh.GlobalStageError, match="current-inventory admission refused"
        ):
            pipeline.preseal_source_gate(root, manifest, source, None)
        return
    assert pipeline.preseal_source_gate(root, manifest, source, None) is True
    gate = manifest["sources"][source]["gate"]
    assert gate["rc"] == 2 and gate["raw_verdict"] == "hold"
    assert gate["verdict"] != "hold"
    assert manifest["sources"][source]["state"] == "gated"
