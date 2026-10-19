"""Real contained DAUM proof stays source-bound inside the original mixed owner."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cre_checkpoint_refresh as cp
import cre_current_inventory_admission as admission
import test_daum_current_inventory as producer
import test_essex_current_inventory as helpers

SOURCE = "daum-commercial"
OTHER = "transwestern"


@pytest.fixture(scope="module")
def producer_folder(tmp_path_factory):
    return tmp_path_factory.mktemp("daum-mixed-producer")


@pytest.fixture(scope="module")
def produced(producer_folder):
    return producer.produce(producer_folder)


@pytest.fixture
def mixed(tmp_path, monkeypatch, produced):
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    run, path, artifact, manifest, report = helpers.setup(tmp_path, produced)
    manifest["config"].pop("current_inventory_admission")
    manifest["config"].update(
        pipeline=True,
        sources=[SOURCE, OTHER],
        current_inventory_admissions={SOURCE: admission.configuration(SOURCE)},
    )
    sibling = run / "transwestern.json"
    sibling.write_text("{}")
    manifest["sources"][OTHER] = {"artifact": {"path": sibling.name}}
    return run, path, artifact, manifest, report


def admit(path, manifest, report, phase="source"):
    return admission.admit(
        path,
        manifest,
        report,
        source=SOURCE,
        current_code_sha=cp.git_identity()[0],
        current_git_dirty=False,
        phase=phase,
    )


def test_pipeline_selection_accepts_only_explicit_daum_entry():
    admission.validate_options(
        [SOURCE], [SOURCE, OTHER], ["sale", "lease"], pipeline=True
    )
    assert admission.configuration_map([SOURCE], [SOURCE, OTHER]) == {
        SOURCE: admission.configuration(SOURCE)
    }


def test_actual_mixed_owner_source_aggregate_staging_and_prewrite(mixed):
    _, path, _, manifest, report = mixed
    original = copy.deepcopy(manifest["config"])
    binding = admit(path, manifest, report)
    assert binding["staged_count"] == binding["emitted_count"] == 100
    manifest["sources"][SOURCE]["gate"] = copy.deepcopy(report["per_source"][SOURCE])
    sibling_gate = {"verdict": "ok", "reason": "ordinary baseline-backed source"}
    report["per_source"][OTHER] = copy.deepcopy(sibling_gate)
    report["generated_for_artifact"] = [
        str(path),
        str(path.parent / "transwestern.json"),
    ]
    assert admit(path, manifest, report, "aggregate") == binding
    # Same sealed report is checked again before write; no proof is retagged.
    assert admit(path, manifest, report, "aggregate") == binding
    assert report["per_source"][OTHER] == sibling_gate
    assert admission.admission_configuration(manifest["config"], OTHER) is None
    assert manifest["config"] == original


@pytest.mark.parametrize("phase", ["source", "aggregate"])
def test_mixed_owner_actual_artifact_worker_keeps_own_proof(mixed, monkeypatch, phase):
    import cre_pipeline_artifact_worker as worker

    run, path, _, manifest, report = mixed
    code = cp.git_identity()[0]
    monkeypatch.setattr(cp, "git_identity", lambda: (code, False))
    if phase == "aggregate":
        admit(path, manifest, report)
        manifest["sources"][SOURCE]["gate"] = copy.deepcopy(
            report["per_source"][SOURCE]
        )
    result = worker.compute(
        "admit",
        {
            "path": str(path),
            "run_dir": str(run),
            "source": SOURCE,
            "manifest": manifest,
            "report": report,
            "phase": phase,
        },
        run / "worker-response.json",
    )
    saved = json.loads(Path(result["report_path"]).read_text())
    binding = saved["per_source"][SOURCE]["current_inventory_proof"]
    assert binding["staged_count"] == binding["emitted_count"] == 100
    assert set(saved["per_source"]) == {SOURCE}


@pytest.mark.parametrize(
    "case",
    [
        "missing_entry",
        "borrowed_entry",
        "wrong_selected",
        "serial_mixed",
        "limited",
        "transactions",
        "missing_proof",
        "sibling_artifact",
        "raw_ok",
        "baseline",
        "hold",
        "floor",
        "target",
        "generation",
        "code",
        "source_binding",
        "prewrite_tamper",
    ],
)
def test_mixed_owner_cannot_borrow_or_weaken_daum_proof(mixed, case):
    _, path, artifact, manifest, report = mixed
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = copy.deepcopy(report["per_source"][SOURCE])
    config = manifest["config"]
    if case == "missing_entry":
        config["current_inventory_admissions"].clear()
    elif case == "borrowed_entry":
        config["current_inventory_admissions"][SOURCE] = admission.configuration(
            "interra-realty"
        )
    elif case == "wrong_selected":
        config["sources"] = [OTHER]
    elif case == "serial_mixed":
        config.pop("current_inventory_admissions")
        config.update(
            pipeline=False, current_inventory_admission=admission.configuration(SOURCE)
        )
    elif case == "limited":
        config["max_items"] = 10
    elif case == "transactions":
        config["transactions"] = ["sale"]
    elif case == "missing_proof":
        artifact["sources"][0].pop("currentInventoryProof")
        helpers.refresh(path, artifact, manifest)
    elif case == "sibling_artifact":
        report["generated_for_artifact"] = str(path.parent / "transwestern.json")
    elif case == "raw_ok":
        report["per_source"][SOURCE]["raw_verdict"] = "ok"
    elif case == "baseline":
        report["per_source"][SOURCE]["baseline_median"] = 100
    elif case == "hold":
        report["per_source"][SOURCE]["raw_verdict"] = "below_floor"
    elif case == "floor":
        artifact["sources"][0]["currentInventoryProof"]["total"] = 99
        helpers.refresh(path, artifact, manifest)
    elif case == "target":
        manifest["preflight"]["database_target"]["value"] = "c" * 64
    elif case == "generation":
        manifest["run_id"] = "foreign-generation"
    elif case == "code":
        manifest["collector_git_sha"] = "0" * 40
    elif case == "source_binding":
        manifest["sources"][SOURCE]["gate"] = {}
    elif case == "prewrite_tamper":
        report["per_source"][SOURCE]["current_inventory_proof"]["artifact_sha256"] = (
            "0" * 64
        )
    with pytest.raises((ValueError, KeyError, TypeError)):
        admit(path, manifest, report, "aggregate")
    assert binding["staged_count"] == 100


@pytest.mark.parametrize("source", ["pyramid-brokerage", "essex-realty"])
def test_other_strict_sources_allow_explicit_pipeline_but_not_serial_mixed(source):
    admission.validate_options(
        [source], [source, OTHER], ["sale", "lease"], pipeline=True
    )
    with pytest.raises(ValueError):
        admission.validate_options([SOURCE], [SOURCE, OTHER], ["sale", "lease"])


def test_resume_configuration_keeps_exact_original_selection(mixed):
    run, _, _, manifest, _ = mixed
    config = manifest["config"]
    assert admission.admission_configuration(config, SOURCE) == admission.configuration(
        SOURCE
    )
    manifest["pipeline"] = {"units": []}
    cp.save_manifest(run, manifest)
    with pytest.raises(cp.GlobalStageError, match="no mixed-stage replay"):
        cp.load_resume_manifest(
            run / "manifest.json",
            git_sha=manifest["collector_git_sha"],
            sources=config["sources"],
            page_cap=400,
            concurrency=1,
            pipeline=True,
            admit_current_inventory_sources=[SOURCE],
        )
    config["current_inventory_admissions"][OTHER] = admission.configuration(SOURCE)
    with pytest.raises(ValueError):
        admission.admission_configuration(config, SOURCE)


def test_ordinary_unit_finishes_while_real_contained_daum_collects(
    tmp_path, monkeypatch, produced, producer_folder
):
    """Real owned collection groups and artifact worker; synthetic serial DB lane."""
    import cre_refresh_pipeline as pipeline
    from cre_owned_processes import OwnedProcessGroup, settle_owned_groups
    from test_cre_checkpoint_refresh import strict_artifact

    root = tmp_path / "2026-10-15T120000Z-123456abcdef"
    root.mkdir()
    sources = (OTHER, SOURCE)
    manifest = cp.new_manifest(
        root,
        git_sha=cp.git_identity()[0],
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=1,
        pipeline=True,
        source_workers=2,
        admit_current_inventory_sources=[SOURCE],
        database_target={"algorithm": "sha256", "value": "b" * 64},
    )
    released = root / "ordinary-unit-complete"
    barrier = root / "barrier.mjs"
    barrier.write_text(
        "import {setTimeout as wait} from 'node:timers/promises';"
        "import {existsSync} from 'node:fs';"
        "import {performance} from 'node:perf_hooks';"
        f"const released={json.dumps(str(released))};"
        "const fetch=globalThis.fetch;let first=true;"
        "const started=performance.now();"
        "globalThis.fetch=async(...args)=>{if(first){while(!existsSync(released)){"
        "if(performance.now()-started>20000)throw Error('fixture release deadline');"
        "await wait(10);}first=false;}"
        "return fetch(...args);};"
    )
    active = {}
    owned_groups = []
    events = []
    db_active = 0
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *a: None)
    cp._clear_cpu_guard_trip()

    def start(run, owner, source, **kwargs):
        assert len([p for p in active.values() if p.poll() is None]) < 2
        attempt, temporary, log_path, started = cp._cohort_attempt(
            run, owner, source, collector_concurrency=1
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        if source == SOURCE:
            env.update(
                CRE_REQUIRE_FRESH_DETAILS="1",
                CRE_REQUIRE_FRESH_PROPERTY_DETAILS="1",
                CRE_REFRESH_GENERATION=owner["run_id"],
                CRE_REFRESH_STARTED_AT=owner["started_at"],
            )
            argv = [
                "node",
                "--import",
                str(producer.DENY),
                "--import",
                "tsx",
                "--import",
                str(producer_folder / "transport.mts"),
                "--import",
                str(barrier),
                "collect.ts",
                "--source",
                SOURCE,
                "--transaction",
                "both",
                "--max-items",
                "0",
                "--out",
                str(temporary),
            ]
        else:
            payload = strict_artifact(
                source,
                detail_scope="detail_page",
                generation=owner["run_id"],
                generation_started_at=owner["started_at"],
            )
            stamp = cp.utc_now()
            payload["runMeta"].update(startedAt=stamp, finishedAt=stamp)
            for row in payload["listings"]:
                row.update(inventoryObservedAt=stamp, detailObservedAt=stamp)
            argv = [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                str(temporary),
                json.dumps(payload),
            ]
        child = subprocess.Popen(
            argv,
            cwd=producer.ROOT,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        active[source] = child
        group = OwnedProcessGroup.from_spawn(child)
        owned_groups.append(group)
        events.append(("start", source))
        return cp.CohortCollectionProcess(
            source,
            child,
            log,
            temporary,
            attempt,
            started,
            group,
        )

    def database(run, owner, unit, env_file, previous):
        nonlocal db_active
        assert db_active == 0
        db_active += 1
        if unit["sources"] == [OTHER]:
            assert active[SOURCE].poll() is None
            events.append(("ordinary_validated_while_daum_collecting", OTHER))
        if SOURCE in unit["sources"]:
            artifact = run / owner["sources"][SOURCE]["artifact"]["path"]
            report = {
                "generated_for_artifact": str(artifact),
                "per_source": {
                    SOURCE: {
                        "verdict": "first_seen",
                        "reason": "no baseline row; cannot gate (first sight)",
                        "baseline_median": None,
                        "effective_floor": 100,
                        "current_active": 133,
                        "mark_missing_safe": False,
                    }
                },
                "summary": {
                    "mode": "live",
                    "torow_errors": 0,
                    "baseline_updated": False,
                    "baseline_update_sources": [],
                    "hold_sources": [],
                    "mark_missing_safe_brokerages": [],
                },
            }
            binding = admit(artifact, owner, report)
            owner["sources"][SOURCE]["gate"] = copy.deepcopy(
                report["per_source"][SOURCE]
            )
            assert admit(artifact, owner, report, "aggregate") == binding
        for source in unit["sources"]:
            owner["sources"][source]["readback"] = {"ok": True}
        unit.update(validation_path="synthetic-db-proof", validation_sha256="c" * 64)
        if unit["sources"] == [OTHER]:
            with released.open("x") as marker:
                marker.write("ordinary synthetic DB unit completed")
        db_active -= 1
        return None

    monkeypatch.setattr(cp, "_start_cohort_collection", start)
    monkeypatch.setattr(pipeline, "preseal_source_gate", lambda *args: True)
    monkeypatch.setattr(pipeline, "run_database_unit", database)
    try:
        assert (
            pipeline.run_bounded_pipeline(
                root,
                manifest,
                sources,
                page_cap=400,
                concurrency=1,
                attempts_this_run=1,
                env_file=None,
                source_workers=2,
            )
            == []
        )
        assert ("ordinary_validated_while_daum_collecting", OTHER) in events
        assert all(child.poll() == 0 for child in active.values())
        assert manifest["pipeline"]["status"] == "complete"
    finally:
        try:
            released.touch(exist_ok=True)
            settlement = settle_owned_groups(
                owned_groups, grace_seconds=1, kill_seconds=2
            )
            assert settlement.settled, settlement.errors
        finally:
            cp._clear_cpu_guard_trip()
