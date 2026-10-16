"""Actual contained DAUM producer through source/aggregate admission and staging."""

import copy
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cre_checkpoint_refresh as cp
import cre_current_inventory_admission as admission
import cre_daum_current_inventory as daum
import test_essex_current_inventory as helpers

SOURCE = "daum-commercial"
ROOT = Path(cp.__file__).parent
DENY = ROOT / "tests/fixtures/daum-offline-deny.mjs"


def produce(folder, *, total=100, all_sale=False, unknown_detail=False, reorder=False):
    preload = folder / "transport.mts"
    fixtures = ROOT / "tests/fixtures/daum-proof-pages.ts"
    preload.write_text(
        "import {daumProofPage,daumProofDetail} from "
        + json.dumps(str(fixtures))
        + ";\n"
        + r"""
const timer=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms>0&&ms<=3000?0:ms,...args);
const calls=[];
globalThis.fetch=async(input,init)=>{
 const url=String(input);calls.push([init?.method??'GET',url]);
 if(init?.method==='POST' && /^https:\/\/daumcommercial[.]com\/property-search\/(?:page\/2\/)?$/.test(url))return new Response(daumProofPage(__TOTAL__,url.endsWith('/page/2/')?2:1,__ALL_SALE__,__REORDER__ && calls.filter(x=>x[0]==='POST').length>2));
 const match=url.match(/^https:\/\/daumcommercial[.]com\/property\/Synthetic-(\d+)\/$/);
 if(init?.method==='GET'&&match)return new Response(daumProofDetail(Number(match[1]),__ALL_SALE__).replace(__UNKNOWN_DETAIL__ ? /class="title-about">[^<]*</g : /UNMATCHABLE/, 'class="title-about"><'));
 throw Error('UNEXPECTED_OFFLINE_URL_NO_FALLBACK');
};
process.on('exit',()=>console.error('OFFLINE_CALLS '+JSON.stringify(calls)));
""".replace("__TOTAL__", str(total))
        .replace("__ALL_SALE__", str(all_sale).lower())
        .replace("__UNKNOWN_DETAIL__", str(unknown_detail).lower())
        .replace("__REORDER__", str(reorder).lower())
    )
    start = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    generation = "2026-10-15T172001Z-123456abcdef"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env.update(
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_REQUIRE_FRESH_PROPERTY_DETAILS="1",
        CRE_REFRESH_GENERATION=generation,
        CRE_REFRESH_STARTED_AT=start,
    )
    path = folder / "artifact.json"
    child = subprocess.run(
        [
            "node",
            "--import",
            str(DENY),
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
    assert child.returncode == 0, child.stderr
    return json.loads(path.read_text()), child.stderr, start, generation


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    return produce(tmp_path_factory.mktemp("daum-contained-producer"))


@pytest.fixture
def prepared(tmp_path, produced, monkeypatch):
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    return helpers.setup(tmp_path, produced)


def test_actual_producer_source_aggregate_and_unique_staging(prepared, produced):
    _, path, artifact, manifest, report = prepared
    assert artifact["totalListings"] == 133
    calls = json.loads(produced[1].split("OFFLINE_CALLS ")[-1].splitlines()[0])
    assert len(calls) == 104
    assert sum(method == "POST" for method, _ in calls) == 4
    binding = helpers.admit(path, manifest, report)
    assert binding["emitted_count"] == binding["staged_count"] == 100
    assert report["per_source"][SOURCE]["raw_verdict"] == "first_seen"
    assert report["per_source"][SOURCE]["mark_missing_safe"] is False
    manifest["sources"][SOURCE]["gate"] = copy.deepcopy(report["per_source"][SOURCE])
    assert helpers.admit(path, manifest, report, "aggregate") == binding


@pytest.mark.parametrize(
    "case",
    [
        "missing_pass",
        "page_endpoint",
        "duplicate_member",
        "membership_drift",
        "wrong_total",
        "nonconsecutive",
        "canonical",
        "duplicate_native",
        "tenure",
        "stale_detail",
        "hash",
        "listing_key",
        "listing_tenure",
        "preserve_children",
        "generation",
        "missing_listing",
        "raw_hold",
        "baseline",
        "subfloor",
        "artifact_hash",
    ],
)
def test_refuses_incomplete_or_drifted_evidence(prepared, case):
    _, path, artifact, manifest, report = prepared
    for entry in artifact["sources"]:
        proof = entry["currentInventoryProof"]
        if case == "missing_pass":
            proof["passes"].pop()
        if case == "page_endpoint":
            proof["passes"][0]["pages"][0]["endpoint"] += "?guess=1"
        if case == "duplicate_member":
            proof["passes"][0]["pages"][0]["members"][1] = proof["passes"][0]["pages"][
                0
            ]["members"][0]
        if case == "membership_drift":
            proof["passes"][0]["pages"][0]["members"][0][0] += "Changed/"
        if case == "wrong_total":
            proof["passes"][0]["pages"][0]["reportedTotal"] = 101
        if case == "nonconsecutive":
            proof["passes"][1]["index"] += 1
        if case == "canonical":
            proof["details"][0][1] += "Changed/"
        if case == "duplicate_native":
            proof["cohort"][1][0] = proof["cohort"][0][0]
        if case == "tenure":
            proof["cohort"][0][7] = "lease"
        if case == "stale_detail":
            proof["details"][0][2] = "2000-01-01T00:00:00Z"
        if case == "hash":
            proof["details"][0][3] = "invalid"
        if case == "generation":
            proof["generationId"] = "old"
        if case == "subfloor":
            proof["total"] = 99
    listing = artifact["listings"][0]
    if case == "listing_key":
        listing["id"] = "wrong"
    if case == "listing_tenure":
        listing["transactionType"] = "Lease"
    if case == "preserve_children":
        listing["preserveChildCollections"] = True
    if case == "missing_listing":
        artifact["listings"].pop()
        artifact["totalListings"] -= 1
    if case == "raw_hold":
        report["per_source"][SOURCE]["verdict"] = "hold"
    if case == "baseline":
        report["per_source"][SOURCE]["baseline_median"] = 100
    helpers.refresh(path, artifact, manifest)
    if case == "artifact_hash":
        manifest["sources"][SOURCE]["artifact"]["sha256"] = "0" * 64
    with pytest.raises((ValueError, KeyError, TypeError)):
        helpers.admit(path, manifest, report)


def test_configuration_remains_explicit_singleton():
    admission.validate_options([SOURCE], [SOURCE], ["sale", "lease"], pipeline=True)
    with pytest.raises(ValueError):
        admission.validate_options(
            [SOURCE], [SOURCE, "pyramid-brokerage"], ["sale", "lease"], pipeline=True
        )


@pytest.mark.parametrize(
    "value",
    [
        "https://unowned.test/property/one/",
        "https://daumcommercial.com/property/one/?extra=",
        "https://user@daumcommercial.com/property/one/",
        "https://daumcommercial.com/property/one/#x",
        "https://daumcommercial.com/property/../",
        "https://daumcommercial.com/property/%2e/",
        "https://daumcommercial.com/property/one\\two/",
        "https://daumcommercial.com/property-search/",
    ],
)
def test_receipt_url_keeps_exact_owned_literal_route(value):
    with pytest.raises(ValueError):
        daum._path_key(value)


def test_namespace_guard_uses_existing_transaction_and_all_history():
    from cre_ingest import build_sql, to_row

    listing = {
        "sourceKey": SOURCE,
        "id": "property/Exact%20Case",
        "daumPostId": "123",
        "url": "https://daumcommercial.com/property/Exact%20Case/",
        "name": "Synthetic",
        "transactionType": "Sale",
    }
    row = to_row(listing, {}, "2026-10-16T00:00:00Z")
    sql = build_sql([row], [], "2026-10-16T00:00:00Z", set())
    guard = sql[
        sql.index("-- DAUM keeps historical") : sql.index(
            "-- The transaction-wide lifecycle advisory lock above"
        )
    ]
    assert "ON COMMIT DROP" in guard
    assert "FROM credeals.cre_listings t" in guard
    assert "deleted_at" not in guard
    assert "$.**.daumPostId" in guard
    assert "DAUM identity contradiction within incoming cohort" in guard
    assert sql.index(guard) < sql.index("INSERT INTO credeals.cre_listings")


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

    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    run, path, artifact, manifest, report = helpers.setup(tmp_path, produced)
    manifest["collector_git_sha"] = support.SHA
    manifest["preflight"]["database_target"]["value"] = "d" * 64
    monkeypatch.setattr(cp, "git_identity", lambda: (support.SHA, False))
    binding = helpers.admit(path, manifest, report)
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
        key: result["transaction_breakdown"][key] for key in ("sale", "lease", "both")
    } == {"sale": 67, "lease": 66, "both": 33}


@pytest.mark.parametrize("mode", ["all_sale", "unknown_detail", "reorder", "subfloor"])
def test_actual_supported_tenure_and_floor_cases(tmp_path, monkeypatch, mode):
    producer = tmp_path / "producer"
    producer.mkdir()
    actual = produce(
        producer,
        total=99 if mode == "subfloor" else 100,
        all_sale=mode == "all_sale",
        unknown_detail=mode == "unknown_detail",
        reorder=mode == "reorder",
    )
    destination = tmp_path / "admission"
    destination.mkdir()
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    _, path, artifact, manifest, report = helpers.setup(destination, actual)
    if mode == "subfloor":
        with pytest.raises(ValueError):
            helpers.admit(path, manifest, report)
    else:
        assert helpers.admit(path, manifest, report)["staged_count"] == 100
        if mode == "all_sale":
            assert artifact["sources"][1]["listingsCollected"] == 0
            assert all(row["transactionMode"] == "sale" for row in artifact["listings"])


@pytest.mark.parametrize(
    "case",
    [
        "target",
        "code",
        "missing_binding",
        "changed_receipt",
        "wrong_scope",
        "baseline_hold",
    ],
)
def test_aggregate_does_not_inherit_or_retag_source_authority(prepared, case):
    _, path, artifact, manifest, report = prepared
    binding = helpers.admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": binding}
    if case == "target":
        manifest["preflight"]["database_target"]["value"] = "c" * 64
    elif case == "code":
        manifest["collector_git_sha"] = "0" * 40
    elif case == "missing_binding":
        manifest["sources"][SOURCE]["gate"] = {}
    elif case == "changed_receipt":
        for entry in artifact["sources"]:
            entry["currentInventoryProof"]["passes"][0]["pages"][0][
                "responseTextSha256"
            ] = "f" * 64
        helpers.refresh(path, artifact, manifest)
    elif case == "wrong_scope":
        manifest["config"]["transactions"] = ["sale"]
    elif case == "baseline_hold":
        report["per_source"][SOURCE].update(
            raw_verdict="below_floor", baseline_median=200
        )
    with pytest.raises(ValueError):
        helpers.admit(path, manifest, report, "aggregate")
