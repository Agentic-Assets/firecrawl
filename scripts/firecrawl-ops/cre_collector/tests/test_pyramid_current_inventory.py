"""Offline producer-to-admission contracts for Pyramid strict details."""

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

SOURCE = "pyramid-brokerage"
TARGET = "b" * 64
ROOT = Path(cp.__file__).parent


@pytest.fixture
def summary_evidence(tmp_path, monkeypatch):
    from test_notification_summary import evidence

    return evidence.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    return produce(tmp_path_factory.mktemp("pyramid-producer"))


def produce(
    root,
    total=100,
    *,
    lease_html=None,
    clock_step_ms=0,
    clock_before_ms=0,
    expected_error=None,
):
    start = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    run_id = "2026-10-15T172001Z-123456abcdef"
    preload = root / "transport.mts"
    preload.write_text(r"""let inventory=0,details=0;
const rows=Array.from({length:100},(_,i)=>({id:i+1,status:"publish",type:"pbc-listings",link:`https://www.pyramidbrokerage.com/listings/property-${i+1}/`,"sale-type":[i<50?246:249],"property-use":[1],city:[2],state:[3],title:{rendered:"Synthetic property"},_embedded:{"wp:term":[[{id:i<50?246:249,taxonomy:"sale-type",name:i<50?"Sale":"Lease",slug:i<50?"sale":"lease"}],[{taxonomy:"property-use",name:"Industrial"}],[{taxonomy:"city",name:"Albany"}],[{taxonomy:"state",name:"NY"}]]}}));
globalThis.fetch=async(input)=>{const url=new URL(String(input));if(url.hostname!=="www.pyramidbrokerage.com")throw Error("Unexpected offline endpoint");if(url.pathname==="/wp-json/wp/v2/pbc-listings"){inventory++;return Response.json(rows,{headers:{"X-WP-Total":"100","X-WP-TotalPages":"1"}})}const id=Number(url.pathname.match(/property-(\d+)/)?.[1]);if(!id)throw Error("Unexpected detail");details++;return new Response(`<html><head><title>Property</title><link rel="shortlink" href="https://www.pyramidbrokerage.com/?p=${id}"><link rel="canonical" href="${rows[id-1].link}"></head><body><main id="content"><section class="property-details"><ul><li><span class="name">Sale/Lease:</span><span class="value">${id<=50?"Sale":"Lease"}</span></li></ul></section><div class="property-content">Current property</div><div class="featured-image"><img src="https://cdn.photos.sparkplatform.com/cir/property.jpg"></div></main></body></html>`)};
process.on("exit",()=>console.error(`OFFLINE_COUNTS ${inventory} ${details}`));""")
    transport = preload.read_text().replace("length:100", f"length:{total}")
    transport = transport.replace("i<50", f"i<{total // 2}").replace(
        "id<=50", f"id<={total // 2}"
    )
    transport = transport.replace(
        'Response.json(rows,{headers:{"X-WP-Total":"100","X-WP-TotalPages":"1"}})',
        f'Response.json(rows.slice((Number(url.searchParams.get("page"))-1)*100,Number(url.searchParams.get("page"))*100),{{headers:{{"X-WP-Total":"{total}","X-WP-TotalPages":"{(total + 99) // 100}"}}}})',
    )
    if lease_html is not None:
        transport = transport.replace(
            "details++;return new Response(",
            "details++;if(id>50)return new Response("
            + json.dumps(lease_html)
            + '.replaceAll("119152",String(id)).replaceAll('
            + json.dumps(
                "https://www.pyramidbrokerage.com/listings/99-garnsey-road-pittsford-ny-r6580/"
            )
            + ",rows[id-1].link));return new Response(",
        )
    if clock_step_ms or clock_before_ms:
        transport += """
const ActualDate=globalThis.Date;let clockOffset=0;
class AdjustedClock extends ActualDate {
 constructor(...args){if(args.length)super(...args);else super(ActualDate.now()+clockOffset);}
 static now(){return ActualDate.now()+clockOffset;}
}
globalThis.Date=AdjustedClock;
const fakeTransport=globalThis.fetch;
globalThis.fetch=async(input,options)=>{
 if(STEP && new URL(String(input)).pathname.endsWith('/property-73/'))clockOffset=STEP;
 return fakeTransport(input,options);
};
""".replace("STEP", str(clock_step_ms)).replace(
            "clockOffset=0", f"clockOffset={clock_before_ms}"
        )
    preload.write_text(transport)
    path = root / "artifact.json"
    env = {
        k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ
    }
    env.update(
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_REQUIRE_FRESH_PROPERTY_DETAILS="1",
        CRE_REFRESH_GENERATION=run_id,
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
    if expected_error is not None:
        assert child.returncode == 1, child.stderr
        assert expected_error in child.stderr
        assert not path.exists()
        return None, child.stderr, start, run_id
    assert child.returncode == 0, child.stderr
    return json.loads(path.read_text()), child.stderr, start, run_id


def setup(tmp_path, produced):
    artifact, stderr, start, run_id = copy.deepcopy(produced)
    run = tmp_path / run_id
    run.mkdir()
    path = run / "artifact.json"
    code, _dirty = cp.git_identity()
    manifest = cp.new_manifest(
        run,
        git_sha=code,
        git_dirty=False,
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        database_target={"algorithm": "sha256", "value": TARGET},
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


def refresh(path, artifact, manifest):
    path.write_text(json.dumps(artifact))
    manifest["sources"][SOURCE]["artifact"] = {
        "sha256": cp.sha256_file(path),
        "bytes": path.stat().st_size,
        "path": path.name,
        "started_at": manifest["started_at"],
        "attempt_started_at": manifest["started_at"],
    }


def admit(path, manifest, report, phase="source"):
    return admission.admit(
        path,
        manifest,
        report,
        current_code_sha=cp.git_identity()[0],
        current_git_dirty=False,
        phase=phase,
    )


def test_actual_collect_shares_complete_inventory_and_all_strict_details(produced):
    artifact, stderr, _, _ = produced
    assert "OFFLINE_COUNTS 2 100" in stderr
    sale, lease = artifact["sources"]
    assert (
        sale["currentInventoryProof"]["cohort"]
        == lease["currentInventoryProof"]["cohort"]
    )
    assert len(sale["currentInventoryProof"]["details"]) == 50
    assert len(lease["currentInventoryProof"]["details"]) == 50
    assert all(
        r["freshnessProvenance"]["detailScope"] == "detail_page"
        for r in artifact["listings"]
    )


def test_source_aggregate_and_real_worker_compact_binding(
    tmp_path, produced, monkeypatch
):
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    binding = admit(path, manifest, report)
    assert binding["staged_count"] == 100
    assert len(json.dumps(binding)) < 4096
    manifest["sources"][SOURCE]["gate"] = {
        "current_inventory_proof": copy.deepcopy(binding)
    }
    assert admit(path, manifest, report, "aggregate") == binding
    monkeypatch.setattr(
        cp, "git_identity", lambda: (manifest["collector_git_sha"], False)
    )
    import cre_pipeline_artifact_worker as worker

    response = run / "response.json"
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
        response,
    )
    saved = json.loads(Path(result["report_path"]).read_text())
    assert saved["per_source"][SOURCE]["current_inventory_proof"] == binding
    assert result["scope"]["whole_source_coverage"] is False
    assert len(json.dumps(result).encode()) < 64 * 1024
    assert artifact["totalListings"] == 100

    def command(argv, _log, **_kwargs):
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(exist_ok=True)
        fresh = copy.deepcopy(report)
        fresh["per_source"][SOURCE].update(
            verdict="first_seen", reason="no baseline row; cannot gate (first sight)"
        )
        output.write_text(json.dumps(fresh))
        return 0

    monkeypatch.setattr(cp, "run_command", command)
    cp.gate_source(run, manifest, SOURCE, path, None)
    cp.run_aggregate_gate(run, manifest, None)
    assert manifest["aggregate_gate"]["non_ok_sources"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "missing_proof",
        "mixed_cohort",
        "equal_count_substitution",
        "missing_detail",
        "wrong_post",
        "wrong_url",
        "wrong_tx",
        "stale_detail",
        "detail_digest",
        "inventory_digest",
        "missing_taxonomy",
        "monitor",
        "cap",
        "preserve_children",
        "generation",
        "generation_start",
        "artifact_hash",
        "code",
        "policy",
        "baseline",
        "floor",
        "conversion",
        "count",
        "target_aggregate",
        "aggregate_substitution",
    ],
)
def test_strict_proof_faults_refused(tmp_path, produced, fault):
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    entry = artifact["sources"][0]
    proof = entry["currentInventoryProof"]
    row = artifact["listings"][0]
    gate = report["per_source"][SOURCE]
    if fault == "missing_proof":
        entry.pop("currentInventoryProof")
    elif fault == "mixed_cohort":
        artifact["sources"][1]["currentInventoryProof"]["cohort"][0][1] += "other"
    elif fault == "equal_count_substitution":
        proof["cohort"][0][0] = 9999
    elif fault == "missing_detail":
        proof["details"].pop()
    elif fault == "wrong_post":
        row["wordpressRecord"]["id"] = 9999
    elif fault == "wrong_url":
        row["canonicalUrl"] += "other"
    elif fault == "wrong_tx":
        row["transactionType"] = "Lease"
    elif fault == "stale_detail":
        row["detailObservedAt"] = "2020-01-01T00:00:00Z"
    elif fault == "detail_digest":
        proof["details"][0][3] = "wrong"
    elif fault == "inventory_digest":
        proof["passes"][0]["fingerprint"] = "d" * 64
    elif fault == "missing_taxonomy":
        row.pop("state")
    elif fault == "monitor":
        artifact["runMeta"]["mode"] = "monitor"
    elif fault == "cap":
        artifact["runMeta"]["maxItemsPerSource"] = 100
    elif fault == "preserve_children":
        row["preserveChildCollections"] = True
    elif fault == "generation":
        proof["generationId"] = "other"
    elif fault == "generation_start":
        proof["generationStartedAt"] = "2020-01-01T00:00:00Z"
    elif fault == "code":
        manifest["collector_git_sha"] = "d" * 40
    elif fault == "policy":
        manifest["config"]["current_inventory_admission"]["source_policy_sha256"] = (
            "d" * 64
        )
    elif fault == "baseline":
        gate.update(verdict="ok", baseline_median=100)
    elif fault == "floor":
        gate["effective_floor"] = 1
    elif fault == "conversion":
        report["summary"]["torow_errors"] = 1
    elif fault == "count":
        gate["current_active"] = 99
    if fault in {"target_aggregate", "aggregate_substitution"}:
        binding = admit(path, manifest, report)
        manifest["sources"][SOURCE]["gate"] = {
            "current_inventory_proof": copy.deepcopy(binding)
        }
        if fault == "target_aggregate":
            manifest["preflight"]["database_target"]["value"] = "d" * 64
        else:
            gate["current_inventory_proof"]["staged_count"] = 99
        with pytest.raises(ValueError):
            admit(path, manifest, report, "aggregate")
        return
    refresh(path, artifact, manifest)
    if fault == "artifact_hash":
        manifest["sources"][SOURCE]["artifact"]["sha256"] = "d" * 64
    with pytest.raises((ValueError, KeyError)):
        admit(path, manifest, report)


def test_below_floor_unique_cohort_refused(tmp_path, produced):
    small = tmp_path / "below-floor"
    small.mkdir()
    run, path, artifact, manifest, report = setup(tmp_path, produce(small, total=99))
    assert artifact["totalListings"] == 99
    assert len(artifact["sources"][0]["currentInventoryProof"]["cohort"]) == 99
    with pytest.raises(ValueError):
        admit(path, manifest, report)


@pytest.mark.parametrize(
    "fault",
    [
        "page_ids",
        "page_count",
        "page_endpoint",
        "blank_unknown_parameter",
        "pass_clock",
        "detail_clock",
        "proof_type",
        "missing_pass",
        "mixed_sources",
    ],
)
def test_malformed_receipts_refuse(tmp_path, produced, fault):
    _, path, artifact, manifest, report = setup(tmp_path, produced)
    proof = artifact["sources"][0]["currentInventoryProof"]
    if fault == "page_ids":
        proof["passes"][0]["pages"][0][6][0] = True
    elif fault == "page_count":
        proof["passes"][0]["pages"][0][5] = True
    elif fault == "page_endpoint":
        proof["passes"][0]["pages"][0][1] += "&unknown=1"
    elif fault == "blank_unknown_parameter":
        for entry in artifact["sources"]:
            for snapshot in entry["currentInventoryProof"]["passes"]:
                snapshot["pages"][0][1] += "&unsupported="
    elif fault == "pass_clock":
        proof["passes"][0]["pages"][0][2] = "2020-01-01T00:00:00Z"
    elif fault == "detail_clock":
        proof["details"][0][2] = "2099-01-01T00:00:00Z"
    elif fault == "proof_type":
        artifact["sources"][0]["currentInventoryProof"] = []
    elif fault == "missing_pass":
        proof["passes"].pop()
    elif fault == "mixed_sources":
        manifest["config"]["sources"].append("hanley")
    refresh(path, artifact, manifest)
    with pytest.raises((ValueError, KeyError, TypeError)):
        admit(path, manifest, report)


def test_2069_member_actual_producer_and_worker_protocol_stay_bounded(
    tmp_path, monkeypatch
):
    large = tmp_path / "large"
    large.mkdir()
    produced = produce(large, total=2069, lease_html=absent_tenure_html())
    assert "OFFLINE_COUNTS 42 2069" in produced[1]
    run, path, _, manifest, report = setup(tmp_path, produced)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    args = {
        "path": str(path),
        "run_dir": str(run),
        "source": SOURCE,
        "manifest": manifest,
        "report": report,
        "phase": "aggregate",
    }
    request, response = run / "request.json", run / "response.json"
    request.write_text(
        json.dumps({"request_id": "bounded", "action": "admit", "arguments": args})
    )
    assert request.stat().st_size < 8 * 1024**2
    request_bytes = request.stat().st_size
    # Real worker subprocess/protocol, synthetic clean-code boundary only.
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
    assert saved["request_id"] == "bounded"
    assert response.stat().st_size < 64 * 1024
    gate = json.loads(Path(saved["value"]["report_path"]).read_text())["per_source"][
        SOURCE
    ]
    assert gate["current_inventory_proof"] == binding
    assert binding["staged_count"] == 2069
    print(
        json.dumps(
            {
                "cohort": 2069,
                "logical_requests": 2111,
                "artifact_bytes": path.stat().st_size,
                "worker_request_bytes": request_bytes,
                "worker_response_bytes": response.stat().st_size,
                "binding_bytes": len(json.dumps(binding).encode()),
            }
        )
    )
    # The unchanged worker refuses oversize protocol input before computation.
    request.write_text(" " * (8 * 1024**2 + 1))
    failed = subprocess.run(
        [
            sys.executable,
            "cre_pipeline_artifact_worker.py",
            "--request",
            str(request),
            "--response",
            str(run / "oversize-response.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failed.returncode != 0
    assert not (run / "oversize-response.json").exists()


def test_actual_producer_admission_worker_artifact_reaches_strict_summary(
    tmp_path, absent_produced, summary_evidence, monkeypatch
):
    import cre_checkpoint_series as series
    import test_notification_summary as support
    import cre_pipeline_artifact_worker as worker

    run, path, artifact, manifest, report = setup(tmp_path, absent_produced)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    monkeypatch.setattr(
        cp, "git_identity", lambda: (manifest["collector_git_sha"], False)
    )
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
        staged_unique=100,
    )
    saved["readback"].update(
        expected_staged_unique=100, latest_inventory_batch_active=100
    )
    saved["gate"] = {"current_inventory_proof": binding}
    ev["save"]()
    result = support.read(ev, source=SOURCE)
    assert result["verified_listings"] == 100
    assert {
        k: result["transaction_breakdown"][k] for k in ("sale", "lease", "both")
    } == {"sale": 50, "lease": 50, "both": 0}


def test_primary_sublease_survives_actual_producer_proof_and_staging(tmp_path):
    from cre_ingest import to_row

    primary = (ROOT / "tests/fixtures/pyramid-sublease-detail.html").read_text()
    result = produce(tmp_path, lease_html=primary)
    artifact = result[0]
    assert len(artifact["listings"]) == 100
    leases = [r for r in artifact["listings"] if r["transactionMode"] == "lease"]
    assert len(leases) == 50
    assert all(r["detailFacts"]["Sale/Lease"] == "Sublease" for r in leases)
    assert all(r["transactionType"] == "Lease" for r in leases)
    assert all(
        to_row(r, {}, artifact["runMeta"]["finishedAt"])["transaction_type"] == "lease"
        for r in leases
    )
    admission_root = tmp_path / "admission"
    admission_root.mkdir()
    run, path, artifact, manifest, report = setup(admission_root, result)
    binding = admit(path, manifest, report)
    assert binding["staged_count"] == 100
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    aggregate = admit(path, manifest, report, phase="aggregate")
    assert aggregate["staged_count"] == 100


@pytest.mark.parametrize("fact", ["Both", "wholesale", "prelease"])
def test_unknown_primary_transaction_cannot_default_or_enter_proof(tmp_path, fact):
    primary = (
        (ROOT / "tests/fixtures/pyramid-sublease-detail.html")
        .read_text()
        .replace("Sublease", fact)
    )
    result = produce(tmp_path, lease_html=primary)
    artifact = result[0]
    assert len(artifact["listings"]) == 50
    failed = next(s for s in artifact["sources"] if s["transaction"] == "lease")
    assert "Pyramid detail 51" in failed["error"]
    assert "unsupported Sale/Lease fact" in failed["error"]
    assert fact in failed["error"]
    admission_root = tmp_path / "admission"
    admission_root.mkdir()
    _, path, _, manifest, report = setup(admission_root, result)
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def absent_tenure_html():
    record = json.loads(
        (ROOT / "tests/fixtures/pyramid-354272-record.json").read_text()
    )
    html = (ROOT / "tests/fixtures/pyramid-354272-detail.html").read_text()
    return html.replace(str(record["id"]), "119152").replace(
        record["link"],
        "https://www.pyramidbrokerage.com/listings/99-garnsey-road-pittsford-ny-r6580/",
    )


def test_absent_html_tenure_requires_record_owned_literal_evidence(tmp_path):
    html = absent_tenure_html()
    produced = produce(tmp_path, lease_html=html)
    artifact = produced[0]
    assert artifact["totalListings"] == 100
    (tmp_path / "admission").mkdir()
    run, path, artifact, manifest, report = setup(tmp_path / "admission", produced)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    assert admit(path, manifest, report, "aggregate") == binding
    lease = next(
        row for row in artifact["listings"] if row["transactionMode"] == "lease"
    )
    assert lease["detailFacts"] == {"Price": "Contact Agent"}
    assert lease["detailTransactionFacts"] == {"count": 0}
    assert lease["transactionEvidence"]["kind"] == "wordpress_sale_type"
    assert lease["transactionType"] == "Lease"


@pytest.fixture(scope="module")
def absent_produced(tmp_path_factory):
    return produce(
        tmp_path_factory.mktemp("pyramid-absent"), lease_html=absent_tenure_html()
    )


@pytest.mark.parametrize(
    "fault",
    [
        "record_link",
        "record_id",
        "status",
        "type",
        "numeric_only",
        "term_id",
        "term_taxonomy",
        "term_name",
        "term_slug",
        "duplicate_term",
        "multiple_ids",
        "missing_capture",
        "blank",
        "unknown",
        "duplicate",
        "present",
        "page_hash",
        "page_time",
        "page_number",
        "wrong_kind",
        "wrong_tx",
        "missing_evidence",
    ],
)
def test_absent_tenure_proof_independently_refuses_mutation(
    tmp_path, absent_produced, fault
):
    _, path, artifact, manifest, report = setup(tmp_path, absent_produced)
    listing = next(r for r in artifact["listings"] if r["transactionMode"] == "lease")
    record = listing["wordpressRecord"]
    evidence = listing["transactionEvidence"]
    if fault == "record_link":
        record["link"] += "other/"
    elif fault == "record_id":
        record["id"] = 1
    elif fault == "status":
        record["status"] = "draft"
    elif fault == "type":
        record["type"] = "page"
    elif fault == "numeric_only":
        record.pop("saleTypeTerms")
    elif fault.startswith("term_"):
        record["saleTypeTerms"][0][fault[5:]] = {
            "id": 246,
            "taxonomy": "filter",
            "name": "Both",
            "slug": "sublease",
        }[fault[5:]]
    elif fault == "duplicate_term":
        record["saleTypeTerms"] *= 2
    elif fault == "multiple_ids":
        record["saleType"] = [249, 246]
    elif fault == "missing_capture":
        listing.pop("detailTransactionFacts")
    elif fault in ("blank", "unknown", "present"):
        value = {"blank": "", "unknown": "Both", "present": "Lease"}[fault]
        listing["detailFacts"]["Sale/Lease"] = value
        listing["detailTransactionFacts"] = {"count": 1, "values": [value]}
    elif fault == "duplicate":
        listing["detailTransactionFacts"] = {"count": 2, "values": ["Lease", "Lease"]}
    elif fault == "page_hash":
        evidence["inventoryPage"][2] = "b" * 64
    elif fault == "page_time":
        evidence["inventoryPage"][1] = manifest["started_at"]
    elif fault == "page_number":
        evidence["inventoryPage"][0] = 2
    elif fault == "wrong_kind":
        evidence["kind"] = "html_property_fact"
    elif fault == "wrong_tx":
        evidence["transaction"] = "sale"
    elif fault == "missing_evidence":
        listing.pop("transactionEvidence")
    refresh(path, artifact, manifest)
    for phase in ("source", "aggregate"):
        with pytest.raises((ValueError, KeyError, TypeError)):
            admit(path, manifest, report, phase)


@pytest.mark.parametrize("step_ms", [-1000, 1000])
def test_actual_producer_observation_clock_orders_small_wall_steps(tmp_path, step_ms):
    produced = produce(tmp_path, clock_step_ms=step_ms)
    (tmp_path / "admission").mkdir()
    _, path, artifact, manifest, report = setup(tmp_path / "admission", produced)
    assert artifact["totalListings"] == 100
    assert "OFFLINE_COUNTS 2 100" in produced[1]
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    assert admit(path, manifest, report, "aggregate") == binding


@pytest.mark.parametrize("step_ms", [-310000, 310000])
def test_actual_producer_refuses_large_wall_drift(tmp_path, step_ms):
    artifact, stderr, _, _ = produce(
        tmp_path, clock_step_ms=step_ms, expected_error="Observation clock wall drift"
    )
    assert artifact is None
    assert "OFFLINE_COUNTS 2" in stderr


def test_pre_anchor_step_cannot_repair_a_different_process_generation(tmp_path):
    produced = produce(tmp_path, clock_before_ms=-60000)
    assert produced[0]["totalListings"] == 100
    (tmp_path / "admission").mkdir()
    _, path, _, manifest, report = setup(tmp_path / "admission", produced)
    with pytest.raises(ValueError):
        admit(path, manifest, report)
