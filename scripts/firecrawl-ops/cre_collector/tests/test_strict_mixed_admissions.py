"""Fresh strict source proofs stay distinct in one original mixed owner."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cre_checkpoint_refresh as cp
import cre_current_inventory_admission as admission
import cre_notification_summary as summary
import test_current_inventory_admission as interra
import test_essex_current_inventory as essex
import test_pyramid_current_inventory as pyramid

SOURCES = [essex.SOURCE, pyramid.SOURCE, interra.SOURCE]


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    root = tmp_path_factory.mktemp("strict-mixed-producers")
    start = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    outputs = {}
    for module in (essex, pyramid):
        folder = root / module.SOURCE
        folder.mkdir()
        outputs[module.SOURCE] = module.produce(folder, start=start)[0]
    folder = root / interra.SOURCE
    folder.mkdir()
    _, _, artifact, _, _ = interra.setup(
        folder, generation="2026-10-15T172001Z-123456abcdef", started_at=start
    )
    outputs[interra.SOURCE] = artifact
    return start, outputs


@pytest.fixture
def mixed(tmp_path, artifacts):
    start, outputs = artifacts
    run = tmp_path / "2026-10-15T172001Z-123456abcdef"
    run.mkdir()
    # Deliberately build the exact entries directly: verifier RED must not be
    # hidden behind the separate configuration-map refusal.
    manifest = cp.new_manifest(
        run,
        git_sha=cp.git_identity()[0],
        git_dirty=False,
        sources=SOURCES,
        page_cap=400,
        concurrency=1,
        pipeline=True,
        source_workers=2,
        database_target={"algorithm": "sha256", "value": "b" * 64},
    )
    manifest["started_at"] = start
    manifest["config"]["current_inventory_admissions"] = {
        source: admission.configuration(source) for source in SOURCES
    }
    report = {
        "generated_for_artifact": [],
        "per_source": {},
        "summary": {
            "mode": "live",
            "torow_errors": 0,
            "baseline_updated": False,
            "baseline_update_sources": [],
            "hold_sources": [],
            "mark_missing_safe_brokerages": [],
        },
    }
    for source, artifact in outputs.items():
        path = run / (source + ".json")
        path.write_text(json.dumps(artifact))
        manifest["sources"][source]["artifact"] = {
            "path": path.name,
            "sha256": cp.sha256_file(path),
            "bytes": path.stat().st_size,
            "started_at": start,
            "attempt_started_at": start,
        }
        report["generated_for_artifact"].append(str(path))
        report["per_source"][source] = {
            "current_active": artifact["totalListings"],
            "baseline_median": None,
            "effective_floor": 100,
            "verdict": "first_seen",
            "reason": "no baseline row; cannot gate (first sight)",
            "mark_missing_safe": False,
        }
    return run, manifest, report


def admit(run, manifest, report, source, phase="source"):
    return admission.admit(
        run / manifest["sources"][source]["artifact"]["path"],
        manifest,
        report,
        source=source,
        current_code_sha=cp.git_identity()[0],
        current_git_dirty=False,
        phase=phase,
    )


def test_mixed_selection_uses_only_exact_explicit_entries():
    assert set(admission.configuration_map(SOURCES, SOURCES)) == set(SOURCES)
    with pytest.raises(ValueError):
        admission.validate_options(SOURCES, SOURCES, ["sale", "lease"])


def test_real_three_source_aggregate_and_prewrite_keep_flat_owner(mixed):
    run, manifest, report = mixed
    config = copy.deepcopy(manifest["config"])
    bindings = {}
    for source in SOURCES:
        singleton_report = copy.deepcopy(report)
        singleton_report["per_source"] = {source: report["per_source"][source]}
        singleton_report["generated_for_artifact"] = str(run / (source + ".json"))
        bindings[source] = admit(run, manifest, singleton_report, source)
        manifest["sources"][source]["gate"] = copy.deepcopy(
            singleton_report["per_source"][source]
        )
    for source in SOURCES:
        assert admit(run, manifest, report, source, "aggregate") == bindings[source]
        assert admit(run, manifest, report, source, "aggregate") == bindings[source]
    assert manifest["config"] == config
    assert {
        s: b.get("staged_count", b.get("emitted_count")) for s, b in bindings.items()
    } == {
        essex.SOURCE: 2,
        pyramid.SOURCE: 100,
        interra.SOURCE: 2,
    }
    assert (
        summary._pipeline_admissions(config, SOURCES)
        == config["current_inventory_admissions"]
    )


@pytest.mark.parametrize("source", SOURCES[:2])
@pytest.mark.parametrize("phase", ["source", "aggregate"])
def test_actual_artifact_worker_preserves_mixed_strict_proof(
    mixed, monkeypatch, source, phase
):
    import cre_pipeline_artifact_worker as worker

    run, manifest, report = mixed
    config = copy.deepcopy(manifest["config"])
    monkeypatch.setattr(
        cp, "git_identity", lambda: (manifest["collector_git_sha"], False)
    )
    if phase == "aggregate":
        admit(run, manifest, report, source)
        manifest["sources"][source]["gate"] = copy.deepcopy(
            report["per_source"][source]
        )
    result = worker.compute(
        "admit",
        {
            "path": str(run / (source + ".json")),
            "run_dir": str(run),
            "source": source,
            "manifest": manifest,
            "report": report,
            "phase": phase,
        },
        run / "worker-response.json",
    )
    saved = json.loads(Path(result["report_path"]).read_text())
    binding = saved["per_source"][source]["current_inventory_proof"]
    assert binding["staged_count"] == (2 if source == essex.SOURCE else 100)
    assert (
        binding["artifact_sha256"] == manifest["sources"][source]["artifact"]["sha256"]
    )
    assert set(saved["per_source"]) == set(SOURCES)
    assert manifest["config"] == config


@pytest.mark.parametrize("source", SOURCES[:2])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_entry",
        "sibling_entry",
        "unselected",
        "duplicate",
        "serial",
        "target",
        "code",
        "artifact",
        "generation",
        "baseline",
        "hold",
        "missing_detail",
        "missing_member_path",
        "foreign_artifact_path",
    ],
)
def test_mixed_strict_proof_cannot_borrow_scope_or_weaken_guards(mixed, source, fault):
    run, manifest, report = mixed
    admit(run, manifest, report, source)
    manifest["sources"][source]["gate"] = copy.deepcopy(report["per_source"][source])
    config = manifest["config"]
    if fault == "missing_entry":
        config["current_inventory_admissions"].pop(source)
    elif fault == "sibling_entry":
        config["current_inventory_admissions"][source] = admission.configuration(
            interra.SOURCE
        )
    elif fault == "unselected":
        config["sources"] = [interra.SOURCE]
    elif fault == "duplicate":
        config["sources"] += [source]
    elif fault == "serial":
        config.pop("current_inventory_admissions")
        config.update(
            pipeline=False, current_inventory_admission=admission.configuration(source)
        )
    elif fault == "target":
        manifest["preflight"]["database_target"]["value"] = "c" * 64
    elif fault == "code":
        manifest["collector_git_sha"] = "0" * 40
    elif fault == "artifact":
        (run / (source + ".json")).write_text("{}")
    elif fault == "generation":
        manifest["run_id"] = "foreign-generation"
    elif fault == "baseline":
        report["per_source"][source]["baseline_median"] = 100
    elif fault == "hold":
        report["per_source"][source].update(verdict="hold", raw_verdict="hold")
    elif fault == "missing_detail":
        path = run / (source + ".json")
        artifact = json.loads(path.read_text())
        artifact["sources"][0]["currentInventoryProof"]["details"].pop()
        path.write_text(json.dumps(artifact))
        manifest["sources"][source]["artifact"].update(
            sha256=cp.sha256_file(path),
            bytes=path.stat().st_size,
        )
    elif fault == "missing_member_path":
        report["generated_for_artifact"].pop()
    elif fault == "foreign_artifact_path":
        report["generated_for_artifact"][0] = str(run / "foreign.json")
    with pytest.raises((ValueError, KeyError)):
        admit(run, manifest, report, source, "aggregate")


@pytest.mark.parametrize("borrowed_entry", [False, True])
def test_actual_summary_reader_binds_all_three_mixed_members(
    tmp_path, monkeypatch, borrowed_entry
):
    import test_notification_summary as support

    evidence = support.evidence.__wrapped__(tmp_path, monkeypatch)
    owner = support.pipeline_evidence(evidence, essex.SOURCE, pyramid.SOURCE)
    support.qualify_pipeline_second_source(evidence, owner)
    for side in ("parent", "child"):
        owner[side]["config"]["sources"].append(interra.SOURCE)
    owner["source2"] = interra.SOURCE
    support.qualify_pipeline_second_source(evidence, owner)
    entries = admission.configuration_map(SOURCES, SOURCES)
    if borrowed_entry:
        entries[essex.SOURCE] = entries[pyramid.SOURCE]
    for side in ("parent", "child"):
        owner[side]["config"]["current_inventory_admissions"] = entries
    evidence["save"]()
    if borrowed_entry:
        with pytest.raises(support.REFUSED):
            support.read_pipeline(evidence, SOURCES)
    else:
        result = support.read_pipeline(evidence, SOURCES)
        assert result["verified"] and result["complete"]
        assert {
            row["source_key"]: row["verified_listings"]
            for row in result["per_source_summaries"]
        } == dict.fromkeys(SOURCES, 3)
        assert (
            len({owner["child"]["sources"][s]["artifact"]["sha256"] for s in SOURCES})
            == 3
        )
