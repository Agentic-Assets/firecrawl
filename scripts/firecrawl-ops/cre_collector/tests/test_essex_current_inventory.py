"""Actual offline Essex producer through both admission gates and staging."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cre_checkpoint_refresh as cp
import cre_current_inventory_admission as admission
import cre_ingest as ingest

SOURCE = "essex-realty"
ROOT = Path(cp.__file__).parent


def produce(root, total=2, closed=1, second_pass="same"):
    start = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    generation = "2026-10-15T172001Z-123456abcdef"
    preload = root / "transport.mts"
    preload.write_text(
        r"""
import {readFileSync} from 'node:fs';
const base='https://essexrealtygroup.com/properties/';
const slugs=Array.from({length:__TOTAL__},(_,i)=>`Current-${i}`);
const archived=Array.from({length:__CLOSED__},(_,i)=>`Closed-${i}`);
const card=(slug,status)=>`<a class="property-card" href="${base}${slug}/"><span class="property-card__label">${status}</span><span class="property-info__address--address-1">Synthetic ${slug}</span></a>`;
const current=`<a class="allposts">ALL [${slugs.length}]</a>`+slugs.map((slug,i)=>card(slug,i%2?'UNDER CONTRACT':'ON MARKET')).join('');
const closed=`<a class="allposts">ALL [${archived.length}]</a>`+archived.map(slug=>card(slug,'CLOSED')).join('');
const sitemap='<urlset>'+[...slugs,...archived].map(slug=>`<url><loc>${base}${slug}/</loc></url>`).join('')+'</urlset>';
const detail=readFileSync('tests/fixtures/essex-detail.html','utf8');
const calls=[];
const mode='__SECOND_PASS__';
const reads=new Map();
const changed=(html,url)=>{
 const count=(reads.get(url)??0)+1;reads.set(url,count);
 if(count!==2)return html;
 if(mode==='reorder'){
  if(url.endsWith('.xml'))return '<urlset>'+[...slugs,...archived].reverse().map(slug=>`<url><loc>${base}${slug}/</loc></url>`).join('')+'</urlset>';
  const cards=html.match(/<a class="property-card"[\s\S]*?<\/a>/g)??[];
  return html.slice(0,html.indexOf('<a class="property-card"'))+cards.reverse().join('');
 }
 if(url===base){
  if(mode==='name')return html.replace('Synthetic Current-0','Changed Current-0');
  if(mode==='lifecycle')return html.replace('ON MARKET','UNDER CONTRACT');
  if(mode==='location')return html.replace('Synthetic Current-0</span>','Synthetic Current-0</span><span class="property-info__address--address-2">Changed location</span></a>');
  if(mode==='units')return html.replace('Synthetic Current-0</span>','Synthetic Current-0</span><span class="property-info__units">12 Units</span></a>');
 }
 return html;
};
// Only remove the source's pacing wait in this offline fake transport process.
// Its 60-second abort deadline and all real producer/observation code remain.
const realTimer=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>realTimer(fn,ms>0&&ms<=10000?0:ms,...args);
globalThis.fetch=async(input)=>{
 const url=String(input);calls.push(url);
 if(url===base)return new Response(changed(current,url));
 if(url===base+'?sale_deal_status_id=3')return new Response(changed(closed,url));
 if(url==='https://essexrealtygroup.com/properties-sitemap.xml')return new Response(changed(sitemap,url));
 const index=slugs.findIndex(slug=>url===base+slug+'/');
 if(index<0)throw Error('Unexpected offline URL');
 return new Response(detail.replaceAll('EXISTING_PROPERTY',slugs[index]).replace('1090741',String(1001+index)).replace('ON MARKET',index%2?'UNDER CONTRACT':'ON MARKET'));
};
process.on('exit',()=>console.error('OFFLINE_CALLS '+JSON.stringify(calls)));
""".replace("__TOTAL__", str(total))
        .replace("__CLOSED__", str(closed))
        .replace("__SECOND_PASS__", second_pass)
    )
    path = root / "artifact.json"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env.update(
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_REQUIRE_FRESH_PROPERTY_DETAILS="1",
        CRE_REFRESH_GENERATION=generation,
        CRE_REFRESH_STARTED_AT=start,
    )
    child = subprocess.run(
        [
            "node",
            "--import",
            "tsx",
            "--import",
            str(preload),
            "collect.ts",
            "--source",
            SOURCE,
            "--transaction",
            "both",
            "--max-items",
            "0",
            "--out",
            str(path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if second_pass in {"name", "lifecycle", "location", "units"}:
        assert child.returncode == 1 and "passes drifted" in child.stderr, child.stderr
        return child
    assert child.returncode == 0, child.stderr
    return json.loads(path.read_text()), child.stderr, start, generation


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    return produce(tmp_path_factory.mktemp("essex-producer"))


def refresh(path, artifact, manifest):
    path.write_text(json.dumps(artifact))
    manifest["sources"][SOURCE]["artifact"] = {
        "sha256": cp.sha256_file(path),
        "bytes": path.stat().st_size,
        "path": path.name,
        "started_at": manifest["started_at"],
        "attempt_started_at": manifest["started_at"],
    }


def setup(tmp_path, produced):
    artifact, _, start, generation = copy.deepcopy(produced)
    run = tmp_path / generation
    run.mkdir()
    path = run / "artifact.json"
    manifest = cp.new_manifest(
        run,
        git_sha=cp.git_identity()[0],
        git_dirty=False,
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        database_target={"algorithm": "sha256", "value": "b" * 64},
        admit_current_inventory_source=SOURCE,
    )
    manifest["started_at"] = start
    refresh(path, artifact, manifest)
    report = {
        "generated_for_artifact": str(path),
        "per_source": {
            SOURCE: {
                "current_active": artifact["totalListings"],
                "baseline_median": None,
                "effective_floor": 100,
                "verdict": "first_seen",
                "reason": "no baseline row; cannot gate (first sight)",
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
    return run, path, artifact, manifest, report


def admit(path, manifest, report, phase="source"):
    return admission.admit(
        path,
        manifest,
        report,
        current_code_sha=cp.git_identity()[0],
        current_git_dirty=False,
        phase=phase,
    )


def test_actual_producer_retains_two_reconciliations_and_only_current_details(produced):
    artifact, stderr, _, _ = produced
    sale, lease = artifact["sources"]
    proof = sale["currentInventoryProof"]
    assert proof["schema"] == "essex-strict-detail-v1"
    assert len(proof["passes"]) == 2
    assert proof["passes"][0]["fingerprint"] == proof["passes"][1]["fingerprint"]
    assert len(proof["cohort"]) == len(proof["details"]) == 2
    assert len(proof["passes"][0]["closed"]["rows"]) == 1
    assert lease["currentInventoryProof"]["kind"] == "known_empty_transaction"
    assert lease["currentInventoryProof"]["reason"] == "sale_only_source"
    assert lease["listingsCollected"] == lease["totalAvailableOnSource"] == 0
    calls = json.loads(stderr.split("OFFLINE_CALLS ")[-1].splitlines()[0])
    assert (
        len(calls) == 8
    )  # six archive/sitemap reads, two current details, no closed detail
    assert calls.count("https://essexrealtygroup.com/properties/") == 2
    assert all("/Closed-" not in url for url in calls)


def test_absent_baseline_sub100_source_aggregate_worker_and_staging(
    tmp_path, produced, monkeypatch
):
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    binding = admit(path, manifest, report)
    assert binding["staged_count"] == 2
    assert report["per_source"][SOURCE]["raw_verdict"] == "first_seen"
    assert report["per_source"][SOURCE]["baseline_median"] is None
    assert report["per_source"][SOURCE]["mark_missing_safe"] is False
    manifest["sources"][SOURCE]["gate"] = {
        "current_inventory_proof": copy.deepcopy(binding)
    }
    assert admit(path, manifest, report, "aggregate") == binding
    rows = [
        ingest.to_row(row, {}, artifact["runMeta"]["finishedAt"])
        for row in artifact["listings"]
    ]
    assert [row["external_id"] for row in rows] == [
        "properties/Current-0",
        "properties/Current-1",
    ]
    assert [row["raw_data"]["essexPropertyId"] for row in rows] == ["1001", "1002"]
    monkeypatch.setattr(
        cp, "git_identity", lambda: (manifest["collector_git_sha"], False)
    )
    import cre_pipeline_artifact_worker as worker

    result = worker.compute(
        "admit",
        {
            "path": str(path),
            "run_dir": str(run),
            "source": SOURCE,
            "manifest": manifest,
            "report": report,
            "phase": "aggregate",
        },
        run / "response.json",
    )
    saved = json.loads(Path(result["report_path"]).read_text())
    assert saved["per_source"][SOURCE]["current_inventory_proof"] == binding
    assert len(json.dumps(result).encode()) < 64 * 1024
    assert result["scope"]["whole_source_coverage"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "baseline",
        "raw_hold",
        "source",
        "generation",
        "target",
        "code",
        "closed_overlap",
        "sitemap_missing",
        "pass_drift",
        "stale_detail",
        "missing_detail",
        "native_collision",
        "key_collision",
        "closed_inflation",
        "lifecycle",
        "transaction",
        "lease_nonempty",
        "body_hash",
        "query_url",
    ],
)
def test_proof_faults_refused(tmp_path, produced, fault):
    _, path, artifact, manifest, report = setup(tmp_path, produced)
    sale, lease = artifact["sources"]
    proof = sale["currentInventoryProof"]
    if fault == "baseline":
        report["per_source"][SOURCE]["baseline_median"] = 10
    elif fault == "raw_hold":
        report["per_source"][SOURCE]["verdict"] = "below_floor"
    elif fault == "source":
        proof["sourceKey"] = "interra-realty"
    elif fault == "generation":
        proof["generationId"] = "other"
    elif fault == "target":
        manifest["preflight"]["database_target"]["value"] = "bad"
    elif fault == "code":
        manifest["collector_git_sha"] = "a" * 40
    elif fault == "closed_overlap":
        proof["passes"][0]["closed"]["rows"][0][0] = proof["cohort"][0][1]
    elif fault == "sitemap_missing":
        proof["passes"][0]["sitemap"]["urls"].pop()
    elif fault == "pass_drift":
        proof["passes"][1]["fingerprint"] = "d" * 64
    elif fault == "stale_detail":
        proof["details"][0][2] = "2020-01-01T00:00:00Z"
    elif fault == "missing_detail":
        proof["details"].pop()
    elif fault == "native_collision":
        proof["cohort"][1][0] = proof["cohort"][0][0]
    elif fault == "key_collision":
        proof["cohort"][1][2] = proof["cohort"][0][2]
    elif fault == "closed_inflation":
        artifact["listings"][0]["id"] = "properties/Closed-0"
    elif fault == "lifecycle":
        artifact["listings"][0]["lifecycle"] = "closed"
    elif fault == "transaction":
        artifact["listings"][0]["transactionType"] = "Lease"
    elif fault == "lease_nonempty":
        lease["listingsCollected"] = 1
    elif fault == "body_hash":
        artifact["listings"][0]["essexDetailResponseSha256"] = "bad"
    elif fault == "query_url":
        proof["cohort"][0][1] += "?view=one"
    refresh(path, artifact, manifest)
    with pytest.raises((ValueError, KeyError, TypeError)):
        admit(path, manifest, report)


@pytest.fixture
def summary_evidence(tmp_path, monkeypatch):
    from test_notification_summary import evidence

    return evidence.__wrapped__(tmp_path, monkeypatch)


def test_actual_producer_admission_worker_reaches_unchanged_strict_summary(
    tmp_path, produced, summary_evidence, monkeypatch
):
    import cre_checkpoint_series as series
    import cre_pipeline_artifact_worker as worker
    import test_notification_summary as support

    run, path, artifact, manifest, report = setup(tmp_path, produced)
    manifest["collector_git_sha"] = support.SHA
    manifest["preflight"]["database_target"]["value"] = "d" * 64
    monkeypatch.setattr(cp, "git_identity", lambda: (support.SHA, False))
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    result = worker.compute(
        "admit",
        {
            "path": str(path),
            "run_dir": str(run),
            "source": SOURCE,
            "manifest": manifest,
            "report": report,
            "phase": "aggregate",
        },
        run / "response.json",
    )
    assert (
        json.loads(Path(result["report_path"]).read_text())["per_source"][SOURCE][
            "current_inventory_proof"
        ]
        == binding
    )
    ev = summary_evidence
    parent, child = ev["values"]["parent"], ev["values"]["child"]
    cfg = series.series_config(
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        attempts_per_source=1,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=30,
        cpu_sample_seconds=2,
        nice=15,
        admit_current_inventory_source=SOURCE,
    )
    parent["config"] = cfg
    parent["sources"][SOURCE] = parent["sources"].pop(support.SOURCE)
    child["sources"][SOURCE] = child["sources"].pop(support.SOURCE)
    child["config"] = series._expected_child_config(SOURCE, cfg)
    status = "additive_scope_complete_current_inventory"
    parent["sources"][SOURCE]["checkpoint_status"] = child["status"] = status
    child["scope"] = {
        "kind": "collector_registry_additive_current_inventory",
        "whole_source_coverage": False,
    }
    ev["artifact"].clear()
    ev["artifact"].update(artifact)
    old = ev["files"]["artifact"]
    old.unlink()
    ev["files"]["artifact"] = old.with_name(SOURCE + ".json")
    raw = json.dumps(artifact, separators=(",", ":")).encode()
    saved = child["sources"][SOURCE]
    saved["artifact"].update(
        path=f"sources/{SOURCE}.json",
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
        staged_unique=2,
    )
    saved["readback"].update(expected_staged_unique=2, latest_inventory_batch_active=2)
    saved["gate"] = {"current_inventory_proof": binding}
    ev["save"]()
    result = support.read(ev, source=SOURCE)
    assert result["verified_listings"] == 2
    assert {
        key: result["transaction_breakdown"][key] for key in ("sale", "lease", "both")
    } == {"sale": 2, "lease": 0, "both": 0}


def test_large_closed_archive_does_not_inflate_compact_worker_counts(tmp_path):
    produced = produce(tmp_path, total=38, closed=731)
    destination = tmp_path / "admission"
    destination.mkdir()
    run, path, artifact, manifest, report = setup(destination, produced)
    binding = admit(path, manifest, report)
    assert binding["emitted_count"] == binding["staged_count"] == 38
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    request, response = run / "request.json", run / "response.json"
    arguments = {
        "path": str(path),
        "run_dir": str(run),
        "source": SOURCE,
        "manifest": manifest,
        "report": report,
        "phase": "aggregate",
    }
    request.write_text(
        json.dumps({"request_id": "bounded", "action": "admit", "arguments": arguments})
    )
    assert request.stat().st_size < 8 * 1024**2
    bootstrap = "import sys,cre_checkpoint_refresh as cp,cre_pipeline_artifact_worker as w; code=sys.argv.pop(1); cp.git_identity=lambda:(code,False); raise SystemExit(w.main())"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap,
            manifest["collector_git_sha"],
            "--request",
            str(request),
            "--response",
            str(response),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    saved = json.loads(response.read_text())
    assert saved["ok"] is True, saved
    assert response.stat().st_size < 64 * 1024
    assert artifact["totalListings"] == 38


@pytest.mark.parametrize(
    "fault", ["closed_overlap", "missing_union", "lifecycle", "endpoint"]
)
def test_matching_receipts_still_require_independent_reconciliation(
    tmp_path, produced, fault
):
    _, path, artifact, manifest, report = setup(tmp_path, produced)
    for entry in artifact["sources"]:
        for snapshot in entry["currentInventoryProof"]["passes"]:
            if fault == "closed_overlap":
                snapshot["closed"]["rows"][0][0] = snapshot["current"]["rows"][0][0]
            elif fault == "missing_union":
                snapshot["sitemap"]["urls"].pop()
            elif fault == "lifecycle":
                snapshot["current"]["rows"][0][1] = "closed"
            elif fault == "endpoint":
                snapshot["current"]["endpoint"] += "?unsupported="
            payload = [
                snapshot["current"]["rows"],
                snapshot["closed"]["rows"],
                snapshot["sitemap"]["urls"],
            ]
            snapshot["fingerprint"] = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def test_singleton_configuration_and_prior_target_binding_refuse_drift(
    tmp_path, produced
):
    admission.validate_option(SOURCE, [SOURCE], ("sale", "lease"))
    admission.validate_options([SOURCE], [SOURCE], ("sale", "lease"), pipeline=True)
    with pytest.raises(ValueError):
        admission.validate_options(
            [SOURCE], [SOURCE, "hanley"], ("sale", "lease"), pipeline=True
        )
    with pytest.raises(ValueError):
        admission.validate_option(SOURCE, [SOURCE], ("sale",))
    _, path, _, manifest, report = setup(tmp_path, produced)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    manifest["preflight"]["database_target"]["value"] = "c" * 64
    with pytest.raises(ValueError):
        admit(path, manifest, report, "aggregate")


def test_baseline_backed_below_floor_is_never_an_essex_proof_exception(
    tmp_path, produced
):
    _, path, _, manifest, report = setup(tmp_path, produced)
    report["per_source"][SOURCE].update(
        baseline_median=100, verdict="below_floor", reason="below floor"
    )
    with pytest.raises(ValueError):
        admit(path, manifest, report)


@pytest.mark.parametrize("value", [".", "..", "%2e", ".%2E", "%2E.", "%2e%2E"])
def test_python_proof_rejects_url_parser_dot_aliases(value):
    from cre_essex_current_inventory import _path_key

    with pytest.raises(ValueError):
        _path_key(f"https://essexrealtygroup.com/properties/{value}/")


def test_reordered_actual_producer_collections_reach_both_admissions(tmp_path):
    produced = produce(tmp_path, closed=2, second_pass="reorder")
    artifact, _, _, _ = produced
    assert artifact["sources"][0].get("currentInventoryProof"), artifact["sources"][0]
    run = tmp_path / "admission"
    run.mkdir()
    _, path, _, manifest, report = setup(run, produced)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    assert admit(path, manifest, report, "aggregate") == binding
    assert binding["staged_count"] == 2


@pytest.mark.parametrize("field", ["name", "lifecycle", "location", "units"])
def test_actual_producer_semantic_inventory_change_remains_refused(
    tmp_path, produced, field
):
    produce(tmp_path, second_pass=field)
    run = tmp_path / "admission"
    run.mkdir()
    _, path, artifact, manifest, report = setup(run, produced)
    for entry in artifact["sources"]:
        snapshot = entry["currentInventoryProof"]["passes"][1]
        row = snapshot["current"]["rows"][0]
        index, value = {
            "name": (2, "Changed name"),
            "lifecycle": (1, "under_contract"),
            "location": (3, "Changed location"),
            "units": (4, 12),
        }[field]
        row[index] = value
        payload = [
            snapshot["current"]["rows"],
            snapshot["closed"]["rows"],
            snapshot["sitemap"]["urls"],
        ]
        snapshot["fingerprint"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def test_verifier_canonicalizes_receipt_collections_without_losing_fields(
    tmp_path, produced
):
    _, path, artifact, manifest, report = setup(tmp_path, produced)
    for entry in artifact["sources"]:
        snapshot = entry["currentInventoryProof"]["passes"][1]
        snapshot["current"]["rows"].reverse()
        snapshot["closed"]["rows"].reverse()
        snapshot["sitemap"]["urls"].reverse()
    refresh(path, artifact, manifest)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    assert admit(path, manifest, report, "aggregate") == binding
