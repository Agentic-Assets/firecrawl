"""Real failed collector and owned foreground worker with fake-only fetches."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
import pytest


@pytest.mark.parametrize("denied", [False, True])
def test_evidence_only_collector_failure_reaches_partial_pipeline(tmp_path, monkeypatch, denied, drift=False, journal=False, access_stop=False, database=None, interruption=None, worker_failure=False):
    collector = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    target = repo / "scripts" / "firecrawl-ops" / "cre_collector"
    target.parent.mkdir(parents=True)
    shutil.copytree(collector, target, ignore=shutil.ignore_patterns(
        "node_modules", "out", "__pycache__", ".pytest_cache", "*.pyc"))
    (target / "node_modules").symlink_to(collector / "node_modules", target_is_directory=True)
    if database:
        # The private synthetic worker logs its exception, without changing
        # admission behavior or exposing errors in the production entrypoint.
        worker = target / "cre_pipeline_artifact_worker.py"
        code = worker.read_text()
        old_handler = '    except Exception:\n        result["error"] = "artifact computation failed"'
        new_handler = '    except Exception:\n        import traceback\n        traceback.print_exc()\n        result["error"] = "artifact computation failed"'
        assert code.count(old_handler) == 1
        worker.write_text(code.replace(old_handler, new_handler))
    (repo / ".gitignore").write_text("node_modules\nout\n")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.test", "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, timeout=10).stdout.strip()
    mock = tmp_path / "fake-fetch.mjs"
    mock.write_text('''
import {appendFileSync} from "node:fs";
globalThis.fetch = async (input) => {
  const u = new URL(String(input));
  if (u.hostname !== "my.rcm1.com" && !u.hostname.endsWith("colliers.com")) throw Error("unexpected network target");
  if (u.pathname.includes("GetListingsHtml") || u.pathname.includes("GetMapData")) {
    const listing = u.pathname.includes("GetListingsHtml");
    const start = Number(u.searchParams.get("Start"));
    if (start !== 1) {
      if (process.env.SYNTHETIC_JOURNAL === "1" && start === 101) {
        const html = Array.from({length:100},(_,i)=>`<li class="item"><span class="headline">Fixture</span><a href="/handler/modern.aspx?pv=id${i+101}">View</a></li>`).join("");
        const locations = Array.from({length:100},(_,i)=>({ProjectId:`id${i+101}`}));
        return Response.json(listing ? {success:true,total:201,totalAvail:201,numProjects:100,html}
                                      : {success:true,total:201,totalAvail:201,projectLocations:locations});
      }
      if (process.env.SYNTHETIC_DENIAL === "1") return new Response("denied",{status:403});

      if (process.env.SYNTHETIC_DRIFT === "1" && start === 101 && Number(u.searchParams.get("PageSize")) === 1) {
        const html = '<li class="item"><span class="headline">Changed</span><a href="/handler/modern.aspx?pv=changed">View</a></li>';
        return Response.json(listing ? {success:true,total:103,totalAvail:103,numProjects:1,html}
                                      : {success:true,total:103,totalAvail:103,projectLocations:[{ProjectId:"changed"}]});
      }
      return Response.json({success:false,error:listing ? "Failure retrieving listing HTML." : "Failure retrieving map."});
    }
    const html = Array.from({length:100},(_,i)=>`<li class="item"><span class="headline">Fixture</span><a href="/handler/modern.aspx?pv=id${i+1}">View</a></li>`).join("");
    const locations = Array.from({length:100},(_,i)=>({ProjectId:`id${i+1}`}));
    const total = process.env.SYNTHETIC_JOURNAL === "1" ? 201 : process.env.SYNTHETIC_DRIFT === "1" ? 102 : 101;
    return Response.json(listing ? {success:true,total,totalAvail:total,numProjects:100,html}
                                  : {success:true,total,totalAvail:total,projectLocations:locations});
  }
  if (u.pathname.toLowerCase().includes("slp")) {
    const token = u.searchParams.get("pv");
    appendFileSync(process.env.SYNTHETIC_DETAIL_SENTINEL, token + "\\n");
    if ((process.env.SYNTHETIC_ACCESS_STOP === "1" && token === "id101") ||
        (process.env.SYNTHETIC_ACCESS_STOP === "after_one" && token === "id102"))
      return new Response("denied", {status:403});
    return Response.json({ProjectSummary:{ProjectId:token}});

  }
  if (u.hostname.endsWith("colliers.com")) return new Response("<html>fixture</html>");
  throw Error("unexpected synthetic route " + u.pathname);
};
''')
    run_dir = tmp_path / "generation"
    run_dir.mkdir()
    lock = refresh.SharedLock(target / "out" / "daily" / ".cre.lock", recovery_required=True)
    with lock:
        try:
            target_binding = database["target"] if database else {"algorithm": "sha256", "value": "b" * 64}
            manifest = refresh.new_manifest(run_dir, git_sha=sha, git_dirty=False, sources=("colliers",),
                page_cap=3, concurrency=1, pipeline=True, database_target=target_binding)
            manifest["preflight"]["shared_lock"] = str(lock.path)
            sentinel = tmp_path / "detail-called"
            env = {**os.environ, "NODE_OPTIONS": "--import=" + mock.as_uri(),
                   "SYNTHETIC_DENIAL": "1" if denied else "0", "SYNTHETIC_DRIFT": "1" if drift else "0",
                   "SYNTHETIC_DETAIL_SENTINEL": str(sentinel), "SYNTHETIC_JOURNAL": "1" if journal else "0",
                   "SYNTHETIC_ACCESS_STOP": "after_one" if access_stop == "after_one" else "1" if access_stop else "0"}
            if database:
                env["PSQL_BIN"] = str(database["psql"])
            monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock.path)
            monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
            monkeypatch.setattr(refresh, "safe_process_env", lambda *_: dict(env))
            monkeypatch.setattr(refresh, "COLLECTOR_DIR", target)
            if worker_failure:
                actual_runner = refresh.run_command
                def fail_second_worker(argv, log, **kwargs):
                    if Path(log).name == "colliers-partial-worker-2.log":
                        return 1
                    return actual_runner(argv, log, **kwargs)
                monkeypatch.setattr(refresh, "run_command", fail_second_worker)

            def fake_gate(_run, state, source, _env):
                checkpoint = state["sources"][source]
                data = json.loads((_run / checkpoint["artifact"]["path"]).read_text())
                checkpoint["gate"] = {"current_inventory_proof": {
                    "emitted_count": len(data["listings"]), "held_count": data["held"],
                    "cursor": data["cursor"], "next_cursor": data["proposedCursor"],
                    "artifact_sha256": checkpoint["artifact"]["sha256"],
                    "database_target_sha256": "b" * 64}}
                return True

            def fake_database(_run, state, unit, _env, _previous):
                checkpoint = state["sources"]["colliers"]
                if interruption and len(state["pipeline"]["units"]) == 2:
                    if interruption == "unknown_write":
                        checkpoint["state"] = "ingesting"
                        raise RuntimeError("synthetic unknown write")
                    if interruption == "unknown_settlement":
                        raise refresh.ProcessCleanupUnknown("synthetic unsettled worker")
                    raise KeyboardInterrupt("synthetic interruption")
                checkpoint["readback"] = {"ok": True}
                if journal:
                    from cre_partial_cursor import advance_cursor
                    binding = checkpoint["gate"]["current_inventory_proof"]
                    attempt = f"{len(state['pipeline']['units']):032x}"
                    proof = {"ok": True, "artifact_sha256": binding["artifact_sha256"],
                             "generation_id": state["run_id"], "attempt_id": attempt,
                             "database_target_sha256": "b" * 64, "outcome": "exact_commit"}
                    advance_cursor(lock.path, "colliers", "b" * 64, binding["cursor"], binding["next_cursor"],
                        generation=state["run_id"], artifact_sha256=binding["artifact_sha256"],
                        attempt_id=attempt, outcome="exact_commit", proof=proof, owner_check=lock._require_authority)
                    checkpoint["partial_cursor_settlement"] = {"state": "settled", "attempt_id": attempt,
                        "artifact_sha256": binding["artifact_sha256"]}
                unit.update(validation_path="synthetic", validation_sha256="c" * 64,
                            aggregate_gate={"baseline_advisory_reasons": {}})
                return {"path": "synthetic", "sha256": "c" * 64, "sequence": 1}

            if database:
                import cre_ingest as ingest
                # The compute worker must resolve Git identity from the same
                # synthetic repository bound into this fixture's manifest.
                monkeypatch.setattr(pipeline, "__file__", str(target / "cre_refresh_pipeline.py"))
                monkeypatch.setattr(ingest, "find_psql", lambda: str(database["psql"]))
            if not database:
                monkeypatch.setattr(pipeline, "preseal_source_gate", fake_gate)
                monkeypatch.setattr(pipeline, "run_database_unit", fake_database)
            if not journal:
                # The dispatch-only cases do not synthesize a DB cursor proof.
                monkeypatch.setattr("cre_colliers_partial_checkpoint.archive_verified_pipeline_window",
                                    lambda *_: False)
            if interruption:
                probes = []
                monkeypatch.setattr(pipeline, "reconcile_pipeline_ingests",
                    lambda *args, **kwargs: probes.append(kwargs.get("probe_allowed")))
                expected_error = (RuntimeError if interruption == "unknown_write" else
                                  refresh.ProcessCleanupUnknown if interruption == "unknown_settlement" else KeyboardInterrupt)
                with pytest.raises(expected_error):
                    pipeline.run_bounded_pipeline(run_dir, manifest, ("colliers",), page_cap=3,
                        concurrency=1, attempts_this_run=1, env_file=None, source_workers=1,
                        owner_check=lock._require_authority)
                checkpoint = manifest["sources"]["colliers"]
                assert len(checkpoint["partial_windows"]) == 1
                assert checkpoint["partial_windows"][0]["state"] == "verified"
                assert manifest["pipeline"]["status"] == "reconciliation_required"
                assert manifest["pipeline"]["commit_outcome_unknown"] is (interruption == "unknown_write")
                if interruption == "unknown_write":
                    assert checkpoint["state"] == "ingesting"
                    assert "partial_window_terminal" not in checkpoint
                elif interruption == "unknown_settlement":
                    assert probes == [False]
                    assert "partial_window_terminal" not in checkpoint
                else:
                    assert probes == [False]
                    assert checkpoint["partial_window_terminal"]
                return
            failures = pipeline.run_bounded_pipeline(run_dir, manifest, ("colliers",), page_cap=3,
                concurrency=1, attempts_this_run=1, env_file=str(database["env_file"]) if database else None, source_workers=1,
                owner_check=lock._require_authority)
            checkpoint = manifest["sources"]["colliers"]
            if database:
                database["manifest"] = manifest
            assert checkpoint["attempts"][0]["rc"] == refresh.COHORT_SETTLED_FAILURE_EXIT
            rejected = json.loads((run_dir / checkpoint["attempts"][0]["rejected_artifact"]).read_text())
            assert rejected["listings"] == [] and rejected["runMeta"]["evidenceOnly"] is True
            if denied:
                assert failures == ["colliers"] and not sentinel.exists()
                assert checkpoint["colliers_partial_worker"]["state"] == "failed"
            else:
                assert failures == [] and sentinel.read_text().splitlines()[0] == "id1"
                assert checkpoint["colliers_partial_worker"]["state"] == "settled"
                assert checkpoint["artifact"]["colliers_partial"] is True
                assert manifest["pipeline"]["source_results"]["colliers"]["partial"] is True
                if journal:
                    windows = checkpoint["partial_windows"]
                    expected = (["verified"] if worker_failure else
                                ["verified", "verified"] if access_stop == "after_one" else
                                ["verified", "settled_no_write"] if access_stop else
                                ["verified", "verified", "settled_no_write"])
                    assert [entry["state"] for entry in windows] == expected
                    result = manifest["pipeline"]["source_results"]["colliers"]
                    if worker_failure:
                        assert result["terminal_stop"] == "worker_failed"
                        assert checkpoint["partial_window_terminal"]["colliers_partial_worker"]["rc"] == 1
                        assert sentinel.read_text().splitlines()[-1] == "id100"
                        return
                    assert windows[1]["first_position"] == windows[0]["first_position"]
                    assert windows[1]["cursor"] == windows[0]["next_cursor"]
                    if access_stop:
                        assert result["terminal_stop"] == "source_stopped"
                        if access_stop == "after_one":
                            assert sentinel.read_text().splitlines()[-1] == "id102"
                            assert windows[1]["next_cursor"] > windows[1]["cursor"]
                        else:
                            assert sentinel.read_text().splitlines()[-1] == "id101"
                            assert windows[1]["cursor"] == windows[1]["next_cursor"]
                    else:
                        assert windows[2]["cursor"] == windows[1]["next_cursor"]
                        assert windows[0]["artifact_sha256"] != windows[1]["artifact_sha256"]
                if drift:
                    artifact = json.loads((run_dir / checkpoint["artifact"]["path"]).read_text())
                    assert artifact["sourceOutcome"] == "source_error"
                    assert artifact["coverage"] == "unverified"
                    assert artifact["proposedCursor"] == 101
                    assert len(artifact["listings"]) == 100
        finally:
            lock.clear_recovery_requirement()



def test_real_source_count_drift_reaches_partial_pipeline(tmp_path, monkeypatch):
    test_evidence_only_collector_failure_reaches_partial_pipeline(tmp_path, monkeypatch, False, drift=True)


def test_real_pipeline_journals_a_full_rotation_without_refetch(tmp_path, monkeypatch):
    test_evidence_only_collector_failure_reaches_partial_pipeline(tmp_path, monkeypatch, False, journal=True)


def test_real_pipeline_keeps_committed_window_on_settled_access_stop(tmp_path, monkeypatch):
    test_evidence_only_collector_failure_reaches_partial_pipeline(tmp_path, monkeypatch, False, journal=True, access_stop=True)


@pytest.mark.parametrize("interruption", ["unknown_write", "unknown_settlement", "interrupt"])
def test_later_window_interruption_fences_recovery(tmp_path, monkeypatch, interruption):
    test_evidence_only_collector_failure_reaches_partial_pipeline(
        tmp_path, monkeypatch, False, journal=True, interruption=interruption)


def test_later_worker_failure_reports_retained_verified_window(tmp_path, monkeypatch):
    test_evidence_only_collector_failure_reaches_partial_pipeline(
        tmp_path, monkeypatch, False, journal=True, worker_failure=True)


def test_source_stop_after_new_detail_reports_both_verified_windows(tmp_path, monkeypatch):
    test_evidence_only_collector_failure_reaches_partial_pipeline(
        tmp_path, monkeypatch, False, journal=True, access_stop="after_one")
