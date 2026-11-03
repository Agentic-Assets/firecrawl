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
    start=None,
    missing_city_id=None,
    legacy_subset=True,
    missing_fields_by_id=None,
):
    start = start or (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
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
    if missing_city_id is not None:
        missing_ids = missing_city_id if isinstance(missing_city_id, (list, tuple)) else [missing_city_id]
        omit_city = "".join(
            f"rows[{post - 1}]._embedded['wp:term'] = rows[{post - 1}]._embedded['wp:term'].filter(g => g[0].taxonomy !== 'city');\n"
            for post in missing_ids
        )
        transport = transport.replace("globalThis.fetch=async(input)=>", omit_city + "globalThis.fetch=async(input)=>")
    if missing_fields_by_id:
        taxonomy = {"city": "city", "state": "state", "assetType": "property-use"}
        omit = "".join(
            f"rows[{post - 1}]._embedded['wp:term'] = rows[{post - 1}]._embedded['wp:term'].filter(g => !{json.dumps([taxonomy[f] for f in fields])}.includes(g[0].taxonomy));\n"
            for post, fields in missing_fields_by_id.items()
        )
        transport = transport.replace("globalThis.fetch=async(input)=>", omit + "globalThis.fetch=async(input)=>")
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
    artifact = json.loads(path.read_text())
    if missing_city_id is not None and legacy_subset:
        # Preserve historical v1 admission regressions using a synthetic v1
        # projection of the v2 fixture, not as a claim about current producer output.
        by_post = {r["wordpressRecord"]["id"]: r for r in artifact["listings"]}
        held_posts = set()
        keys = ("id", "url", "canonicalUrl", "transactionMode", "transactionType",
                "inventoryObservedAt", "detailObservedAt", "freshnessProvenance",
                "wordpressRecord", "detailFacts", "detailTransactionFacts", "transactionEvidence",
                "state", "city", "assetType")
        for entry in artifact["sources"]:
            proof = entry["currentInventoryProof"]
            incomplete = proof.pop("incomplete", [])
            if not incomplete:
                continue
            proof["schema"] = "pyramid-verified-subset-v1"
            proof["held"] = [{"postId": item["postId"], "reason": "missing_city",
                              "listing": {k: by_post[item["postId"]][k] for k in keys if k in by_post[item["postId"]]}}
                             for item in incomplete]
            held_posts.update(item["postId"] for item in incomplete)
            entry["listingsCollected"] -= len(incomplete)
            for name in ("listings", "inventoryObserved", "detailObserved", "childPreservationRows"):
                entry["freshness"][name] -= len(incomplete)
        artifact["listings"] = [r for r in artifact["listings"] if r["wordpressRecord"]["id"] not in held_posts]
        artifact["totalListings"] = len(artifact["listings"])
        path.write_text(json.dumps(artifact))
    return artifact, child.stderr, start, run_id


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


def mock_clear_pyramid_probe(monkeypatch):
    import cre_pyramid_identity_probe as identity

    def probe(path, digest, generation, producer, target, _env):
        artifact = json.loads(Path(path).read_text())
        return {"schema": "pyramid-identity-probe-v1", "artifact_sha256": digest,
                "generation_id": generation, "producer_sha": producer,
                "database_target_sha256": target, "staged_sha256": identity.projection_digest(identity.stage(artifact)),
                "held": [], "transitions": []}
    monkeypatch.setattr(identity, "run_probe", probe)


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
    mock_clear_pyramid_probe(monkeypatch)
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


def test_legacy_subset_projection_preserves_complete_cohort(tmp_path):
    from cre_pyramid_current_inventory import subset_source_scope

    artifact, _stderr, _start, _run_id = produce(tmp_path, total=102, missing_city_id=75)
    assert artifact["totalListings"] == 101
    scope = subset_source_scope(artifact)
    assert len(scope["listings"]) == 101
    sale, lease = artifact["sources"]
    assert sale["currentInventoryProof"]["schema"] == "pyramid-strict-detail-v1"
    assert lease["currentInventoryProof"]["schema"] == "pyramid-verified-subset-v1"
    assert [row["postId"] for row in lease["currentInventoryProof"]["held"]] == [75]
    assert len(sale["currentInventoryProof"]["details"]) + len(lease["currentInventoryProof"]["details"]) == 102
    path = tmp_path / "artifact.json"
    with pytest.raises(cp.ArtifactValidationError):
        cp.validate_source_artifact(path, SOURCE, _start, expected_generation_id=_run_id,
                                    expected_generation_started_at=_start, require_strict_freshness=True)
    stats = cp.validate_source_artifact(path, SOURCE, _start, expected_generation_id=_run_id,
                                        expected_generation_started_at=_start, require_strict_freshness=True,
                                        pyramid_subset_allowed=True)
    assert stats["pyramid_subset"] is True and stats["staged_unique"] == 101
    malformed = copy.deepcopy(artifact)
    malformed["sources"][1]["truncated"] = False
    with pytest.raises(ValueError):
        subset_source_scope(malformed)


def test_partial_partition_source_and_aggregate_binding(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    binding = admit(path, manifest, report)
    assert (binding["inventory_count"], binding["emitted_count"], binding["held_count"]) == (102, 101, 1)
    assert binding["whole_source_coverage"] is False
    assert report["per_source"][SOURCE]["verdict"] == "ok_additive_verified_subset"
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": copy.deepcopy(binding)}
    assert admit(path, manifest, report, "aggregate") == binding
    from cre_pyramid_current_inventory import ingest_subset_context
    from cre_pyramid_subset_sql import guard_sql
    envelope = {"manifest": manifest, "source_report": copy.deepcopy(report),
                "aggregate_report": copy.deepcopy(report), "binding": binding, "attempt_id": "a" * 32}
    claims = ingest_subset_context(path, envelope, current_code_sha=manifest["collector_git_sha"],
        current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)
    assert len(claims["members"]) == 102
    assert sum(not member["emitted"] for member in claims["members"]) == 1
    assert "Pyramid subset would revive lifecycle" in guard_sql(claims, mark_missing=False,
                                                               status_activation=False, inventory_only=False)
    changed = copy.deepcopy(envelope)
    changed["binding"]["held_count"] = 0
    with pytest.raises(ValueError):
        ingest_subset_context(path, changed, current_code_sha=manifest["collector_git_sha"],
            current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)


def test_partial_worker_seals_bound_claims(tmp_path, monkeypatch):
    import cre_pipeline_artifact_worker as worker
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    run, path, _artifact, manifest, source_report = setup(tmp_path, produced)
    source_report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    manifest["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    monkeypatch.setattr(cp, "git_identity", lambda: (manifest["collector_git_sha"], False))
    cp._admit_current_inventory(run, manifest, source_report, path, "source", source=SOURCE, local=True)
    source_gate = run / "source-gate.json"
    source_gate.write_text(json.dumps(source_report))
    binding = source_report["per_source"][SOURCE]["current_inventory_proof"]
    manifest["sources"][SOURCE]["gate"] = {"path": source_gate.name, "current_inventory_proof": binding}
    manifest["sources"][SOURCE]["subset_ingest_attempt_id"] = "a" * 32
    aggregate_report = copy.deepcopy(source_report)
    result = worker.compute("admit", {"path": str(path), "run_dir": str(run), "source": SOURCE,
        "manifest": manifest, "report": aggregate_report, "phase": "aggregate"}, run / "worker.json")
    assert result["scope"]["kind"] == "collector_registry_verified_subset"
    manifest["sources"][SOURCE]["pyramid_verified_subset_claims"] = result["subset_claims"]
    argv = cp.subset_ingest_arguments(run, manifest, SOURCE)
    assert argv[:2] == ["--pyramid-verified-subset-claims", result["subset_claims"]["path"]]
    assert "--checkpoint-ingest-attempt" in argv and "--expected-db-target-sha256" in argv
    claims = json.loads(Path(result["subset_claims"]["path"]).read_text())
    from cre_pyramid_current_inventory import ingest_subset_context
    context = ingest_subset_context(path, claims, current_code_sha=manifest["collector_git_sha"],
        current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)
    assert len(context["members"]) == 102
    assert sum(member["emitted"] for member in context["members"]) == 101


def test_partial_child_classification_is_bound_and_cannot_be_misreported(tmp_path):
    import cre_checkpoint_series as series
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    _run, path, _artifact, manifest, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    manifest["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"verdict": "ok_additive_verified_subset",
        "mark_missing_safe": False, "current_inventory_proof": binding}
    assert series.pyramid_partial_child(manifest)
    broken = copy.deepcopy(manifest)
    broken["sources"][SOURCE]["artifact"]["staged_unique"] = 100
    assert not series.pyramid_partial_child(broken)
    for change in ("held_count", "artifact_sha256", "generation_id", "collector_git_sha", "database_target_sha256"):
        broken = copy.deepcopy(manifest)
        broken["sources"][SOURCE]["gate"]["current_inventory_proof"][change] = 0 if change == "held_count" else "other"
        assert not series.pyramid_partial_child(broken)


@pytest.mark.parametrize("fault", ["held_city", "held_state", "held_identity", "held_transaction", "held_hash", "duplicate_held", "missing_detail", "emitted_held_overlap"])
def test_partial_partition_rejects_forged_or_incomplete_evidence(tmp_path, fault):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    _run, path, artifact, manifest, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    proof = artifact["sources"][1]["currentInventoryProof"]
    held = proof["held"][0]
    if fault == "held_city": held["listing"]["city"] = "Invented"
    elif fault == "held_state": held["listing"].pop("state")
    elif fault == "held_identity": held["listing"]["wordpressRecord"]["id"] = 76
    elif fault == "held_transaction": held["listing"]["transactionType"] = "Sale"
    elif fault == "held_hash": held["listing"]["wordpressRecord"]["detailResponseSha256"] = "0" * 64
    elif fault == "duplicate_held": proof["held"].append(copy.deepcopy(held))
    elif fault == "missing_detail": proof["details"] = [row for row in proof["details"] if row[0] != 75]
    else:
        duplicate = copy.deepcopy(artifact["listings"][0])
        duplicate["wordpressRecord"]["id"] = 75
        artifact["listings"].append(duplicate)
        artifact["totalListings"] += 1
        artifact["sources"][0]["listingsCollected"] += 1
        report["per_source"][SOURCE]["current_active"] += 1
    refresh(path, artifact, manifest)
    with pytest.raises((ValueError, KeyError)):
        admit(path, manifest, report)


def test_serial_child_requires_bound_partial_scope(tmp_path, monkeypatch):
    import cre_checkpoint_series as series
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    _run, path, _artifact, manifest, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    manifest["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"verdict": "ok_additive_verified_subset",
        "mark_missing_safe": False, "current_inventory_proof": binding}
    manifest["status"] = "qualified_partial_subset"
    monkeypatch.setattr(series, "_expected_child_config", lambda _source, config: config)
    assert series._child_succeeded(manifest, manifest["config"])
    assert series._reconciled_state(manifest, manifest["config"]) == "qualified_partial_subset"
    for key in ("artifact_sha256", "generation_id", "database_target_sha256"):
        bad = copy.deepcopy(manifest)
        bad["sources"][SOURCE]["gate"]["current_inventory_proof"][key] = "wrong"
        assert not series._child_succeeded(bad, manifest["config"])
    bad = copy.deepcopy(manifest)
    bad["status"] = "additive_scope_complete_current_inventory"
    assert not series._child_succeeded(bad, manifest["config"])


def test_serial_current_admission_runs_gates_claimed_ingest_and_partial_status(tmp_path, monkeypatch):
    import cre_pipeline_artifact_worker as worker
    import cre_checkpoint_series as series
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    run, path, _artifact, manifest, raw_report = setup(tmp_path, produced)
    raw_report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    manifest["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    monkeypatch.setattr(cp, "git_identity", lambda: (manifest["collector_git_sha"], False))
    counter = iter(range(100))
    def compute(_run, action, arguments):
        return worker.compute(action, arguments, run / f"response-{next(counter)}.json")
    monkeypatch.setattr(cp, "run_artifact_compute", compute)
    operations = []
    def command(argv, log, **_kwargs):
        operations.append(argv)
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("synthetic offline command\n")
        if "cre_gate.py" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(raw_report))
            return 2
        assert "cre_ingest.py" in argv
        assert "--pyramid-verified-subset-claims" in argv
        assert "--checkpoint-ingest-attempt" in argv
        assert "--mark-missing" not in argv and "--activate-status" not in argv
        if "--dry-run" in argv:
            sql_dir = Path(argv[argv.index("--keep-artifacts") + 1])
            sql_dir.mkdir(parents=True, exist_ok=True)
            (sql_dir / "ingest.sql").write_text("synthetic SQL; no database connection")
        return 0
    monkeypatch.setattr(cp, "run_command", command)
    mock_clear_pyramid_probe(monkeypatch)
    cp.gate_source(run, manifest, SOURCE, path, None)
    assert manifest["sources"][SOURCE]["gate"]["verdict"] == "ok_additive_verified_subset"
    assert cp.dry_run_source(run, manifest, SOURCE, path)
    assert manifest["sources"][SOURCE]["dry_run"]["deferred_until"] == "sealed_subset_aggregate"
    cp.run_aggregate_gate(run, manifest, None)
    assert manifest["aggregate_gate"]["admission_scope"] == "pyramid_verified_subset_v1"
    cp.ingest_source(run, manifest, SOURCE, path, None)
    assert manifest["sources"][SOURCE]["ingest"]["mark_missing"] is False
    assert manifest["sources"][SOURCE]["ingest"]["status_activation"] is False
    assert len([argv for argv in operations if "cre_ingest.py" in argv]) == 2
    manifest["status"] = "qualified_partial_subset"
    assert series.pyramid_partial_child(manifest)
    assert manifest["sources"][SOURCE]["gate"]["current_inventory_proof"]["held_count"] == 1


def test_partial_notification_summary_is_scoped_and_honest(tmp_path):
    import cre_notification_summary as notification
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    _run, path, _artifact, manifest, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    manifest["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    binding = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"verdict": "ok_additive_verified_subset", "raw_verdict": "first_seen",
        "admission_scope": "pyramid_verified_subset_v1", "mark_missing_safe": False, "current_inventory_proof": binding}
    result = notification._subset_summary_fields(manifest, SOURCE, 101)
    assert result["partial"] is True and result["whole_source_coverage"] is False
    assert (result["inventory_count"], result["emitted_count"], result["held_count"]) == (102, 101, 1)
    assert result["whole_lease_coverage"] == "unknown"
    assert notification._subset_summary_fields(manifest, "hanley", 0) == {}
    with pytest.raises(notification.SummaryUnavailable):
        notification._subset_summary_fields(manifest, SOURCE, 100)


def test_partial_reconciliation_and_resume_preserves_counts_without_replay(tmp_path, monkeypatch):
    import cre_checkpoint_series as series
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    run, path, _artifact, child, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    child["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    binding = admit(path, child, report)
    child["sources"][SOURCE]["gate"] = {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False, "current_inventory_proof": binding}
    child.update(status="qualified_partial_subset", finished_at=cp.utc_now(), error=None,
                 scope={"kind": "collector_registry_verified_subset", "whole_source_coverage": False})
    cfg = series.series_config(sources=[SOURCE], page_cap=400, concurrency=1, attempts_per_source=1,
        max_resume_age_hours=24, max_host_cpu_percent=90, cpu_sustain_seconds=30,
        cpu_sample_seconds=2, nice=15, admit_current_inventory_source=SOURCE)
    child["config"] = series._expected_child_config(SOURCE, cfg)
    parent_dir = tmp_path / "series"
    parent_dir.mkdir()
    parent = series.new_manifest(parent_dir, git_sha=child["collector_git_sha"], config=cfg)
    parent["database_target"] = child["preflight"]["database_target"]
    parent["started_at"] = child["started_at"]
    checkpoint = parent["sources"][SOURCE]
    checkpoint.update(state="running", checkpoint_run=f"runs/{child['run_id']}",
                      attempts=[{"started_at": child["started_at"]}])
    child_dir = parent_dir / checkpoint["checkpoint_run"]
    child_dir.mkdir(parents=True)
    (child_dir / "manifest.json").write_text(json.dumps(child))
    series.reconcile_stale_running_sources(parent_dir, parent)
    assert checkpoint["state"] == "qualified_partial_subset"
    assert checkpoint["subset_counts"] == {"inventory": 102, "emitted": 101, "held": 1}
    monkeypatch.setattr(series.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not replay"))
    assert series.run_series(parent_dir, parent, env_file=None, retry_failed=False) == 0
    assert parent["status"] == "qualified_partial_subset"
    assert len(checkpoint["attempts"]) == 1


def test_pipeline_partial_reconciliation_and_resume_do_not_replay(tmp_path, monkeypatch):
    import cre_checkpoint_series as series
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75)
    _run, path, _artifact, child, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    child["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=101)
    binding = admit(path, child, report)
    child["sources"][SOURCE]["gate"] = {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False, "current_inventory_proof": binding}
    child.update(status="qualified_partial_subset", finished_at=cp.utc_now(), error=None)
    cfg = series.series_config(sources=[SOURCE, "hanley"], page_cap=400, concurrency=1, attempts_per_source=1,
        max_resume_age_hours=24, max_host_cpu_percent=90, cpu_sustain_seconds=30, cpu_sample_seconds=2,
        nice=15, pipeline=True, admit_current_inventory_source=[SOURCE])
    child["config"] = series._expected_child_config(",".join(cfg["sources"]), cfg)
    child["pipeline"] = {"source_results": {SOURCE: {"partial": True}, "hanley": {"partial": False}}}
    parent = series.new_manifest(tmp_path, git_sha=child["collector_git_sha"], config=cfg)
    parent.update(started_at=child["started_at"], database_target=child["preflight"]["database_target"], status="interrupted")
    for row in parent["sources"].values():
        row.update(state="running", checkpoint_run=f"runs/{child['run_id']}", attempts=[{"started_at": child["started_at"]}])
    monkeypatch.setattr(series, "_load_child_manifest", lambda *a, **kw: (tmp_path / "manifest.json", child))
    # The bound report verifier is independently exercised in the pipeline suite.
    monkeypatch.setattr(series, "pipeline_source_qualified", lambda *a, **kw: True)
    monkeypatch.setattr(series, "_pipeline_has_unresolved", lambda *a: False)
    monkeypatch.setattr(series, "document_incomplete", lambda *a: False)
    series.reconcile_stale_running_sources(tmp_path, parent)
    assert parent["sources"][SOURCE]["state"] == "qualified_partial_subset"
    assert parent["sources"][SOURCE]["subset_counts"] == {"inventory": 102, "emitted": 101, "held": 1}
    assert parent["sources"][SOURCE]["pipeline_result"] == {"partial": True}
    assert parent["sources"]["hanley"]["state"] == "complete"
    assert parent["sources"]["hanley"]["pipeline_result"] == {"partial": False}
    monkeypatch.setattr(series.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not replay"))
    assert series.run_pipeline_series(tmp_path, parent, env_file=None) == 0
    assert parent["status"] == "qualified_partial_subset"
    assert all(len(row["attempts"]) == 1 for row in parent["sources"].values())


def test_partial_99_emitted_proves_admission_recovery_and_notification(tmp_path):
    import cre_checkpoint_series as series
    import cre_notification_summary as notification
    fixture = tmp_path / "fixture"; fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=[75, 76, 77])
    _run, path, _artifact, child, report = setup(tmp_path, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    binding = admit(path, child, report)
    assert (binding["inventory_count"], binding["emitted_count"], binding["held_count"]) == (102, 99, 3)
    child["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=99)
    child["sources"][SOURCE]["gate"] = {"verdict": "ok_additive_verified_subset", "raw_verdict": "first_seen",
        "admission_scope": "pyramid_verified_subset_v1", "mark_missing_safe": False, "current_inventory_proof": binding}
    assert series.pyramid_partial_child(child)
    assert series.pyramid_subset_counts(child) == {"inventory": 102, "emitted": 99, "held": 3}
    assert notification._subset_summary_fields(child, SOURCE, 99)["held_count"] == 3


def test_incomplete_source_still_requires_inventory_floor_but_can_emit_all_incomplete(tmp_path):
    for total in (99, 100):
        fixture = tmp_path / f"fixture-{total}"; fixture.mkdir()
        produced = produce(fixture, total=total, missing_city_id=list(range(1, total + 1)), legacy_subset=False)
        admission_dir = tmp_path / f"admission-{total}"; admission_dir.mkdir()
        _run, path, _artifact, child, report = setup(admission_dir, produced)
        report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
        if total < 100:
            with pytest.raises((ValueError, KeyError)):
                admit(path, child, report)
        else:
            binding = admit(path, child, report)
            assert (binding["emitted_count"], binding["incomplete_count"], binding["held_count"]) == (100, 100, 0)


def test_partial_accepts_one_verified_record_in_complete_floor_inventory(tmp_path):
    fixture = tmp_path / "fixture"; fixture.mkdir()
    produced = produce(fixture, total=100, missing_city_id=list(range(2, 101)))
    admission_dir = tmp_path / "admission"; admission_dir.mkdir()
    _run, path, _artifact, child, report = setup(admission_dir, produced)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    binding = admit(path, child, report)
    assert (binding["inventory_count"], binding["emitted_count"], binding["held_count"]) == (100, 1, 99)


def test_incomplete_city_records_are_emitted_and_bound(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=[74, 75], legacy_subset=False)
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    assert artifact["totalListings"] == 102
    incomplete = [row for row in artifact["listings"] if not row.get("city")]
    assert len(incomplete) == 2
    assert all(row.get("missingFieldEvidence") == {"fields": ["city"], "method": "source_detail_and_taxonomy"} for row in incomplete)
    assert all(row.get("preserveChildCollections") is True for row in incomplete)
    stats = cp.validate_source_artifact(path, SOURCE, produced[2], expected_generation_id=produced[3],
                                        expected_generation_started_at=produced[2], require_strict_freshness=True,
                                        pyramid_subset_allowed=True)
    assert stats["staged_unique"] == 102
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    binding = admit(path, manifest, report)
    assert (binding["emitted_count"], binding["incomplete_count"], binding["held_count"]) == (102, 2, 0)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": copy.deepcopy(binding)}
    assert admit(path, manifest, report, "aggregate") == binding
    from cre_pyramid_current_inventory import ingest_subset_context
    from cre_pyramid_subset_sql import guard_sql
    envelope = {"manifest": manifest, "source_report": copy.deepcopy(report),
                "aggregate_report": copy.deepcopy(report), "binding": binding, "attempt_id": "a" * 32}
    claims = ingest_subset_context(path, envelope, current_code_sha=manifest["collector_git_sha"],
        current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)
    assert sum(m["incomplete"] for m in claims["members"]) == 2
    assert "source_detail_and_taxonomy" in guard_sql(claims, mark_missing=False,
                                                        status_activation=False, inventory_only=False)
    for field in ("transition_count", "missing_field_counts"):
        bad_claims = copy.deepcopy(claims)
        if field == "transition_count":
            bad_claims["binding"][field] = False
        else:
            bad_claims["binding"][field]["state"] = False
        with pytest.raises(ValueError):
            guard_sql(bad_claims, mark_missing=False, status_activation=False, inventory_only=False)
    altered = copy.deepcopy(envelope)
    altered["binding"]["transition_count"] = False
    with pytest.raises(ValueError):
        ingest_subset_context(path, altered, current_code_sha=manifest["collector_git_sha"],
            current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)


@pytest.mark.parametrize("mutation", ["city", "provenance", "unbound", "preservation"])
def test_incomplete_city_refuses_forged_evidence(tmp_path, mutation):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=75, legacy_subset=False)
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    row = next(row for row in artifact["listings"] if row["wordpressRecord"]["id"] == 75)
    if mutation == "city": row["city"] = "Guessed"
    if mutation == "provenance": row["missingFieldEvidence"] = {"fields": ["city"], "method": "geocoded"}
    if mutation == "unbound": artifact["sources"][1]["currentInventoryProof"]["incomplete"][0]["postId"] = 76
    if mutation == "preservation": row["preserveChildCollections"] = False
    refresh(path, artifact, manifest)
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    with pytest.raises((ValueError, KeyError, TypeError)):
        admit(path, manifest, report)


def test_identity_probe_holds_only_ambiguous_member_and_binds_both_gates(tmp_path):
    from cre_pyramid_identity_probe import projection_digest, stage
    from cre_pyramid_current_inventory import ingest_subset_context
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=[74, 75], legacy_subset=False)
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    held_key = artifact["listings"][0]["id"]
    manifest["sources"][SOURCE]["pyramid_identity_probe"] = {
        "schema": "pyramid-identity-probe-v1", "artifact_sha256": cp.sha256_file(path),
        "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
        "database_target_sha256": TARGET, "staged_sha256": projection_digest(stage(artifact)), "held": [held_key], "transitions": []}
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    binding = admit(path, manifest, report)
    assert (binding["inventory_count"], binding["emitted_count"], binding["held_count"], binding["incomplete_count"]) == (102, 101, 1, 2)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": copy.deepcopy(binding)}
    assert admit(path, manifest, report, "aggregate") == binding
    envelope = {"manifest": manifest, "source_report": copy.deepcopy(report),
                "aggregate_report": copy.deepcopy(report), "binding": binding, "attempt_id": "a" * 32}
    claims = ingest_subset_context(path, envelope, current_code_sha=manifest["collector_git_sha"],
        current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)
    assert [m["external_id"] for m in claims["members"] if not m["emitted"]] == [held_key]
    import cre_checkpoint_series as series
    import cre_notification_summary as notification
    checkpoint = manifest["sources"][SOURCE]
    checkpoint["artifact"].update(pyramid_subset=True, staged_unique=102)
    checkpoint["gate"].update(verdict="ok_additive_verified_subset", raw_verdict="first_seen",
                              admission_scope="pyramid_incomplete_v2", mark_missing_safe=False)
    assert series.pyramid_partial_child(manifest)
    assert series.pyramid_subset_counts(manifest) == {"inventory": 102, "emitted": 101, "held": 1, "incomplete": 2, "transitions": 0,
            "missing_state": 0, "missing_city": 2, "missing_asset_type": 0}
    summary = notification._subset_summary_fields(manifest, SOURCE, 101)
    assert (summary["emitted_count"], summary["held_count"], summary["incomplete_count"]) == (101, 1, 2)
    breakdown = notification._pyramid_emitted_breakdown(manifest, SOURCE, 101)
    assert breakdown["sale"] + breakdown["lease"] - breakdown["both"] == 101
    altered = copy.deepcopy(envelope)
    altered["manifest"]["sources"][SOURCE]["pyramid_identity_probe"]["held"] = []
    with pytest.raises((ValueError, KeyError, TypeError)):
        ingest_subset_context(path, altered, current_code_sha=manifest["collector_git_sha"],
            current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)


def test_complete_inventory_with_identity_hold_becomes_bound_partial(tmp_path, produced):
    from cre_pyramid_identity_probe import projection_digest, stage
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    held_key = artifact["listings"][0]["id"]
    manifest["sources"][SOURCE]["pyramid_identity_probe"] = {
        "schema": "pyramid-identity-probe-v1", "artifact_sha256": cp.sha256_file(path),
        "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
        "database_target_sha256": TARGET, "staged_sha256": projection_digest(stage(artifact)), "held": [held_key], "transitions": []}
    binding = admit(path, manifest, report)
    assert (binding["schema"], binding["emitted_count"], binding["held_count"], binding["incomplete_count"]) == ("pyramid-incomplete-v2", 99, 1, 0)
    assert binding["whole_source_coverage"] is False


def test_gate_source_runs_bound_identity_probe_before_admission(tmp_path, produced, monkeypatch):
    import cre_pyramid_identity_probe as identity
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    held_key = artifact["listings"][0]["id"]
    probe = {"schema": "pyramid-identity-probe-v1", "artifact_sha256": cp.sha256_file(path),
             "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
             "database_target_sha256": TARGET, "staged_sha256": identity.projection_digest(identity.stage(artifact)),
             "held": [held_key], "transitions": []}
    monkeypatch.setattr(identity, "run_probe", lambda *args: probe)
    monkeypatch.setattr(cp, "git_identity", lambda: (manifest["collector_git_sha"], False))
    monkeypatch.setattr(cp, "run_command", lambda *args, **kwargs: 0)
    (run / "gates").mkdir()
    (run / "gates" / f"{SOURCE}.json").write_text(json.dumps(report))
    original = cp._admit_current_inventory
    monkeypatch.setattr(cp, "_admit_current_inventory", lambda *a, **kw: original(*a, **kw, local=True))
    cp.gate_source(run, manifest, SOURCE, path, None)
    checkpoint = manifest["sources"][SOURCE]
    assert checkpoint["pyramid_identity_probe"] == probe
    assert checkpoint["artifact"]["pyramid_subset"] is True
    assert checkpoint["gate"]["current_inventory_proof"]["held_count"] == 1


def test_complete_inventory_with_native_transition_remains_partial_and_preserves(tmp_path, produced):
    from cre_pyramid_identity_probe import projection_digest, stage
    from cre_pyramid_current_inventory import ingest_subset_context
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    changed_key = artifact["listings"][0]["id"]
    manifest["sources"][SOURCE]["pyramid_identity_probe"] = {
        "schema": "pyramid-identity-probe-v1", "artifact_sha256": cp.sha256_file(path),
        "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
        "database_target_sha256": TARGET, "staged_sha256": projection_digest(stage(artifact)),
        "held": [], "transitions": [changed_key]}
    binding = admit(path, manifest, report)
    assert (binding["emitted_count"], binding["held_count"], binding["incomplete_count"], binding["transition_count"]) == (100, 0, 0, 1)
    assert binding["whole_source_coverage"] is False
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": copy.deepcopy(binding)}
    assert admit(path, manifest, report, "aggregate") == binding
    envelope = {"manifest": manifest, "source_report": copy.deepcopy(report), "aggregate_report": copy.deepcopy(report),
                "binding": binding, "attempt_id": "a" * 32}
    claims = ingest_subset_context(path, envelope, current_code_sha=manifest["collector_git_sha"],
        current_git_dirty=False, expected_database_target_sha256=TARGET, attempt_id="a" * 32)
    assert [m["external_id"] for m in claims["members"] if m["transition"]] == [changed_key]


def test_v2_serial_gate_worker_and_ingest_keep_bound_partition(tmp_path, monkeypatch):
    import cre_pyramid_identity_probe as identity
    import cre_pipeline_artifact_worker as worker
    fixture = tmp_path / "fixture"; fixture.mkdir()
    produced = produce(fixture, total=102, missing_city_id=[74, 75], legacy_subset=False)
    run, path, artifact, manifest, raw_report = setup(tmp_path, produced)
    raw_report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    manifest["sources"][SOURCE]["artifact"].update(pyramid_subset=True, staged_unique=102)
    held, transition = artifact["listings"][0]["id"], artifact["listings"][1]["id"]
    probe = {"schema": "pyramid-identity-probe-v1", "artifact_sha256": cp.sha256_file(path),
             "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
             "database_target_sha256": TARGET, "staged_sha256": identity.projection_digest(identity.stage(artifact)),
             "held": [held], "transitions": [transition]}
    monkeypatch.setattr(identity, "run_probe", lambda *args: probe)
    monkeypatch.setattr(cp, "git_identity", lambda: (manifest["collector_git_sha"], False))
    counter = iter(range(100))
    monkeypatch.setattr(cp, "run_artifact_compute", lambda _run, action, arguments:
                        worker.compute(action, arguments, run / f"v2-response-{next(counter)}.json"))
    def command(argv, log, **_kwargs):
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("offline fixture\n")
        if "cre_gate.py" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(raw_report))
            return 2
        assert "cre_ingest.py" in argv and "--pyramid-verified-subset-claims" in argv
        assert "--mark-missing" not in argv and "--activate-status" not in argv
        if "--dry-run" in argv:
            sql_dir = Path(argv[argv.index("--keep-artifacts") + 1]); sql_dir.mkdir(parents=True, exist_ok=True)
            (sql_dir / "ingest.sql").write_text("offline fixture")
        return 0
    monkeypatch.setattr(cp, "run_command", command)
    cp.gate_source(run, manifest, SOURCE, path, None)
    cp.run_aggregate_gate(run, manifest, None)
    assert manifest["aggregate_gate"]["admission_scope"] == "pyramid_incomplete_v2"
    cp.ingest_source(run, manifest, SOURCE, path, None)
    binding = manifest["sources"][SOURCE]["gate"]["current_inventory_proof"]
    assert (binding["emitted_count"], binding["held_count"], binding["incomplete_count"], binding["transition_count"]) == (101, 1, 2, 1)
    assert manifest["sources"][SOURCE]["ingest"]["mark_missing"] is False


def test_identity_probe_binds_inputs_and_refuses_target_drift(tmp_path, produced, monkeypatch):
    from types import SimpleNamespace
    import cre_pyramid_identity_probe as identity
    import cre_ingest
    import cre_validate
    run, path, artifact, manifest, _report = setup(tmp_path, produced)
    monkeypatch.setattr(cp, "git_identity", lambda: (manifest["collector_git_sha"], False))
    monkeypatch.setattr(cre_ingest, "load_db_url", lambda _env: ("postgresql://test.invalid/test", "fixture"))
    monkeypatch.setattr(cre_ingest, "database_target_fingerprint_from_url", lambda _url: {"value": TARGET})
    monkeypatch.setattr(cre_ingest, "find_psql", lambda: "fixture-psql")
    held = artifact["listings"][0]["id"]
    monkeypatch.setattr(cre_validate, "_run_psql_script", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=f'staged\tjoined\theld\ttransitions\n100\t100\t["{held}"]\t[]\n'))
    result = identity.run_probe(path, cp.sha256_file(path), manifest["run_id"], manifest["collector_git_sha"], TARGET, None)
    assert result["held"] == [held] and result["transitions"] == []
    calls = iter([{"value": TARGET}, {"value": "f" * 64}])
    monkeypatch.setattr(cre_ingest, "database_target_fingerprint_from_url", lambda _url: next(calls))
    with pytest.raises(ValueError, match="target changed"):
        identity.run_probe(path, cp.sha256_file(path), manifest["run_id"], manifest["collector_git_sha"], TARGET, None)


def test_missing_location_and_asset_fields_are_emitted_with_precise_counts(tmp_path):
    fixture = tmp_path / "fixture"; fixture.mkdir()
    produced = produce(fixture, total=102, legacy_subset=False,
                       missing_fields_by_id={74: ["city", "state"], 75: ["assetType"],
                                             76: ["city", "state", "assetType"]})
    run, path, artifact, manifest, report = setup(tmp_path, produced)
    assert len(artifact["listings"]) == 102
    fields = {r["wordpressRecord"]["id"]: r["missingFieldEvidence"]["fields"]
              for r in artifact["listings"] if "missingFieldEvidence" in r}
    assert fields == {74: ["state", "city"], 75: ["assetType"], 76: ["state", "city", "assetType"]}
    report["per_source"][SOURCE]["reason"] += "; a source pass also errored this run"
    binding = admit(path, manifest, report)
    assert binding["incomplete_count"] == 3
    assert binding["missing_field_counts"] == {"state": 2, "city": 2, "assetType": 2}
    assert binding["emitted_count"] == 102
