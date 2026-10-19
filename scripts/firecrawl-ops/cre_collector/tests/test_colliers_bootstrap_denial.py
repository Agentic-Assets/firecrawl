"""Offline typed Colliers denial through the actual CLI and existing coordinators."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import cre_checkpoint_refresh as refresh
import pytest

COLLECTOR = Path(__file__).resolve().parents[1]
CODE = "COLLIERS_MAIN_BOOTSTRAP_HTTP_403"
PRELOAD = r"""
import fs from 'node:fs';
const calls=[];
const originalTimeout=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>originalTimeout(fn,[15000,30000].includes(ms)?0:ms,...args);
globalThis.fetch=async(input)=>{
  const u=new URL(String(input));
  if(u.pathname==='/v2/scrape'){
    calls.push('sitemap');
    return new Response(JSON.stringify({success:true,data:{rawHtml:
      '<urlset><url><loc>https://www.colliers.com/en/properties/fixture/usa1168531</loc></url></urlset>'}}));
  }
  if(u.pathname==='/browser-batch-fetch'){
    calls.push('browser');
    return new Response(JSON.stringify({error:'Bootstrap returned HTTP 403',
      bootstrapStatus:process.env.COLLIERS_DENIAL_STRING==='1'?'403':403}),{status:502});
  }
  throw Error('offline unexpected transport');
};
process.on('exit',()=>fs.writeFileSync(process.env.COLLIERS_DENIAL_CALLS,JSON.stringify(calls),{flag:'wx',mode:0o600}));
"""


@pytest.mark.parametrize("typed", [True, False])
def test_actual_singleton_cli_denial_stops_second_tenure_without_artifact(
    tmp_path, typed
):
    node = shutil.which("node")
    assert node and (COLLECTOR / "node_modules/tsx").is_dir()
    preload = tmp_path / "offline.mjs"
    preload.write_text(PRELOAD)
    output = tmp_path / "artifact.json"
    calls = tmp_path / "calls.json"
    result = subprocess.run(
        [
            node,
            "--import",
            "tsx",
            "--import",
            str(preload),
            "collect.ts",
            "--source=colliers-main",
            "--transaction=both",
            "--max-items=0",
            f"--out={output}",
        ],
        cwd=COLLECTOR,
        env=dict(
            os.environ,
            COLLIERS_MAIN_COVEO_ENABLE="1",
            COLLIERS_DENIAL_CALLS=str(calls),
            COLLIERS_DENIAL_STRING="0" if typed else "1",
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == (79 if typed else 1), result.stderr
    assert not output.exists()
    assert result.stdout == ""
    assert json.loads(calls.read_text()) == ["sitemap"] + ["browser"] * (
        1 if typed else 3
    )
    if not typed:
        assert "colliers_main_terminal_bootstrap_denial" not in result.stderr
        return
    marker = next(
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.startswith('{"event"')
    )
    assert marker == {
        "event": "colliers_main_terminal_bootstrap_denial",
        "code": CODE,
        "http_status": 403,
    }


def manifest(tmp_path, sources=("colliers-main",), **kwargs):
    return refresh.new_manifest(
        tmp_path,
        git_sha="synthetic",
        git_dirty=False,
        sources=sources,
        page_cap=400,
        concurrency=2,
        **kwargs,
    )


def test_serial_terminal_denial_stops_chunks_attempts_and_resume(tmp_path, monkeypatch):
    current = manifest(tmp_path)
    calls = []
    monkeypatch.setattr(refresh, "collector_runtime_dependency_error", lambda: None)
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: calls.append(a) or 79)
    for _ in range(2):
        assert (
            refresh.collect_source(
                tmp_path,
                current,
                "colliers-main",
                transactions=("sale", "lease"),
                page_cap=400,
                concurrency=2,
                attempts_this_run=3,
            )
            is None
        )
    checkpoint = current["sources"]["colliers-main"]
    assert len(calls) == 1
    assert len(checkpoint["attempts"]) == 1
    assert len(checkpoint["attempts"][0]["chunks"]) == 1
    assert checkpoint["state"] == "collect_failed"
    assert checkpoint["artifact"] is None
    assert checkpoint["terminal_collection_failure"] == {
        "code": CODE,
        "http_status": 403,
        "attempt_number": 1,
    }
    assert not refresh._cohort_needs_collection(
        tmp_path, current, "colliers-main", attempts_before=0, attempts_this_run=3
    )


@pytest.mark.parametrize(
    "source,raw,expected",
    [("colliers-main", 79, 83), ("cbre", 79, 79), ("colliers-main", 1, 1)],
)
def test_worker_maps_only_colliers_reserved_exit_including_chunk_return(
    tmp_path, monkeypatch, source, raw, expected
):
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: raw)
    rc = refresh.run_cohort_collection_worker(
        source,
        tmp_path / "artifact.json",
        run_dir=tmp_path,
        attempt_log=tmp_path / "attempt.log",
        attempt_number=1,
        generation_started_at=current_time(),
        page_cap=400,
        concurrency=2,
        transactions=("sale", "lease"),
    )
    assert rc == expected


def current_time():
    return "2026-10-19T14:47:00+00:00"


@pytest.mark.parametrize("source,expected", [("colliers-main", 83), ("cbre", 80)])
def test_worker_cli_preserves_settled_denial_marker(
    tmp_path, monkeypatch, source, expected
):
    monkeypatch.setattr(refresh, "run_cohort_collection_worker", lambda *a, **k: 83)
    argv = refresh.build_cohort_collect_worker_argv(
        source,
        tmp_path / "out.json",
        run_dir=tmp_path,
        attempt_log=tmp_path / "worker.log",
        attempt_number=1,
        generation_started_at=current_time(),
        page_cap=400,
        concurrency=2,
        transactions=("sale", "lease"),
    )
    assert refresh.main(argv[2:]) == expected
    assert 83 in refresh.COHORT_SETTLED_EXIT_CODES


def test_terminal_cohort_frees_exclusive_lane_and_allows_peer_collection(
    tmp_path, monkeypatch
):
    current = manifest(tmp_path, ("colliers-main", "hanley"), source_workers=2)
    started = []
    real_finalize = refresh._finalize_cohort_collection
    monkeypatch.setattr(
        refresh, "_require_completed_group_settlement", lambda _group: None
    )
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(refresh, "_terminate_cohort_processes", lambda _active: None)

    def start(_run, _manifest, source, **_kwargs):
        started.append(source)
        attempt = {
            "number": len(current["sources"][source]["attempts"]) + 1,
            "started_at": current["started_at"],
            "finished_at": None,
            "temporary_artifact": f"{source}.tmp",
        }
        current["sources"][source]["attempts"].append(attempt)
        current["sources"][source]["state"] = "collecting"

        class Process:
            returncode = 83 if source == "colliers-main" else 0

            def poll(self):
                return self.returncode

        return refresh.CohortCollectionProcess(
            source=source,
            process=Process(),
            log_handle=io.StringIO(),
            tmp_artifact=tmp_path / f"{source}.tmp",
            attempt=attempt,
            attempt_started_at=current["started_at"],
            owned_group=object(),
        )

    def finalize(run, current, item):
        if item.source == "colliers-main":
            return real_finalize(run, current, item)
        current["sources"][item.source]["state"] = "validated"
        current["sources"][item.source]["artifact"] = {"path": "hanley.json"}
        item.log_handle.close()
        return True

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", finalize)
    monkeypatch.setattr(
        refresh,
        "_manifest_checkpoint_artifact_valid",
        lambda _run, m, s: (
            (tmp_path / "hanley.json", {}) if m["sources"][s].get("artifact") else None
        ),
    )
    monkeypatch.setattr(
        refresh, "ingest_source", lambda *a, **k: pytest.fail("no ingestion allowed")
    )
    assert refresh.prepare_sources_cohort(
        tmp_path,
        current,
        ("colliers-main", "hanley"),
        page_cap=400,
        concurrency=2,
        attempts_this_run=3,
        env_file=None,
        source_workers=2,
    ) == ["colliers-main"]
    assert started == ["colliers-main", "hanley"]
    assert (
        current["sources"]["colliers-main"]["terminal_collection_failure"]["code"]
        == CODE
    )
    assert current["sources"]["colliers-main"]["artifact"] is None
    assert current["sources"]["hanley"]["state"] == "validated"


@pytest.mark.parametrize(
    "source,rc", [("cbre", 79), ("cbre", 83), ("colliers-main", 1)]
)
def test_untyped_or_other_source_exit_retains_retry_eligibility(
    tmp_path, monkeypatch, source, rc
):
    current = manifest(tmp_path, (source,))
    monkeypatch.setattr(refresh, "collector_runtime_dependency_error", lambda: None)
    monkeypatch.setattr(refresh, "run_command", lambda *a, **k: rc)
    assert (
        refresh.collect_source(
            tmp_path,
            current,
            source,
            transactions=("sale", "lease"),
            page_cap=400,
            concurrency=2,
            attempts_this_run=2,
        )
        is None
    )
    checkpoint = current["sources"][source]
    assert len(checkpoint["attempts"]) == 2
    assert "terminal_collection_failure" not in checkpoint


@pytest.mark.parametrize(
    "source,code",
    [
        ("cbre", CODE),
        ("cbre", None),
        ("colliers-main", []),
        ("avison-young", CODE),
        ("colliers-main", refresh.AVISON_YOUNG_TERMINAL_FAILURE_CODE),
    ],
)
def test_terminal_marker_is_source_bound(tmp_path, source, code):
    current = manifest(tmp_path, (source,))
    current["sources"][source]["terminal_collection_failure"] = {"code": code}
    assert refresh._cohort_needs_collection(
        tmp_path, current, source, attempts_before=0, attempts_this_run=3
    )
