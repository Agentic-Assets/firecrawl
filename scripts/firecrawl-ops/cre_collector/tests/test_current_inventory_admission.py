"""Offline Interra current-inventory admission, never a provider/DB call."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cre_checkpoint_refresh as cp
import pytest

SOURCE = "interra-realty"
CODE = "a" * 40
TARGET = "b" * 64
ENDPOINT = "https://interrarealty.com/wp-json/filtered-loop/v1/posts?post_type=torque_listing&posts_per_page=1000&paged=1"


def setup(tmp_path, generation=None, started_at=None):
    now = datetime.now(UTC)
    start = started_at or (now - timedelta(minutes=2)).isoformat()
    observed = (now if started_at else now - timedelta(minutes=1)).isoformat()
    run = tmp_path / (generation or "2026-10-14T120000Z")
    run.mkdir()
    generation = run.name
    rows = []
    for native, badge in [("10", "Available"), ("20", "Under Contract")]:
        rows.append(
            {
                "sourceKey": SOURCE,
                "transactionMode": "sale",
                "transactionType": "Sale",
                "id": native,
                "name": "Synthetic",
                "url": f"https://interrarealty.com/listing/synthetic-{native}/",
                "canonicalUrl": f"https://interrarealty.com/listing/synthetic-{native}/",
                "statusBadge": badge,
                "inventoryObservedAt": observed,
                "detailObservedAt": observed,
                "preserveChildCollections": True,
                "freshnessProvenance": {
                    "generationId": generation,
                    "observedAt": observed,
                    "method": "interra_filtered_loop_inventory",
                    "cacheDisposition": "live",
                    "detailScope": "authoritative_inventory_feed",
                },
            }
        )
    receipt = {
        "schema": "interra-current-inventory-v1",
        "sourceKey": SOURCE,
        "transaction": "sale",
        "generationId": generation,
        "kind": "terminal_feed",
        "endpoint": ENDPOINT,
        "observedAt": observed,
        "hasNextPage": False,
        "responseTextSha256": "c" * 64,
        "nativeIdsByLifecycle": {
            "available": ["10"],
            "under_contract": ["20"],
            "closed": ["30"],
        },
        "emittedNativeIdsSha256": hashlib.sha256(b'["10","20"]').hexdigest(),
    }
    artifact = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": start,
            "finishedAt": now.isoformat(),
            "freshness": {
                "generationId": generation,
                "generationStartedAt": start,
                "requireFreshDetails": True,
            },
        },
        "sources": [
            {
                "sourceKey": SOURCE,
                "transaction": "sale",
                "supported": True,
                "truncated": False,
                "listingsCollected": 2,
                "currentInventoryProof": receipt,
            },
            {
                "sourceKey": SOURCE,
                "transaction": "lease",
                "supported": True,
                "truncated": False,
                "listingsCollected": 0,
                "currentInventoryProof": {
                    "schema": receipt["schema"],
                    "sourceKey": SOURCE,
                    "transaction": "lease",
                    "generationId": generation,
                    "kind": "known_empty_transaction",
                    "reason": "sale_only_source",
                    "count": 0,
                },
            },
        ],
        "listings": rows,
        "brokers": [],
        "totalListings": 2,
    }
    for entry in artifact["sources"]:
        count = entry["listingsCollected"]
        entry["freshness"] = {
            "listings": count,
            "detailErrors": 0,
            "staleInventoryObservations": 0,
            "staleDetailObservations": 0,
            "inventoryObserved": count,
            "detailObserved": count,
            "authoritativeInventoryFeed": count,
            "childPreservationRows": count,
        }
    path = run / "artifact.json"
    path.write_text(json.dumps(artifact))
    manifest = cp.new_manifest(
        run,
        git_sha=CODE,
        git_dirty=False,
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        database_target={"algorithm": "sha256", "value": TARGET},
    )
    manifest["started_at"] = start
    manifest["config"]["current_inventory_admission"] = configuration()
    stats = cp.validate_source_artifact(
        path,
        SOURCE,
        start,
        expected_generation_id=generation,
        expected_generation_started_at=start,
        expected_transactions=("sale", "lease"),
    )
    manifest["sources"][SOURCE]["artifact"] = {
        **stats,
        "path": "artifact.json",
        "attempt_started_at": start,
    }
    report = {
        "generated_for_artifact": str(path),
        "per_source": {
            SOURCE: {
                "current_active": 2,
                "baseline_median": None,
                "effective_floor": 100,
                "floor_policy": "default",
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


def configuration():
    import cre_current_inventory_admission as admission

    return admission.configuration(SOURCE)


def admit(path, manifest, report, phase="source"):
    import cre_current_inventory_admission as admission

    return admission.admit(
        path,
        manifest,
        report,
        current_code_sha=CODE,
        current_git_dirty=False,
        phase=phase,
    )


def test_current_inventory_both_gates_preserve_raw_hold_and_never_mark_missing(
    tmp_path,
):
    _, path, _, manifest, report = setup(tmp_path)
    proof = admit(path, manifest, report)
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": proof}
    aggregate = copy.deepcopy(report)
    aggregate["per_source"][SOURCE]["verdict"] = "first_seen"
    aggregate["per_source"][SOURCE]["reason"] = (
        "no baseline row; cannot gate (first sight)"
    )
    assert admit(path, manifest, aggregate, phase="aggregate") == proof
    info = aggregate["per_source"][SOURCE]
    assert info["raw_verdict"] == "first_seen"
    assert info["baseline_median"] is None and info["effective_floor"] == 100
    assert info["admission_scope"] == "additive_current_inventory"
    assert info["mark_missing_safe"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "same_count_different_id",
        "duplicate_id",
        "closed_overlap",
        "terminal_missing",
        "terminal_true",
        "digest",
        "endpoint",
        "lease_missing",
        "lease_count",
        "cap",
        "source_error",
        "stale",
        "generation",
        "lifecycle",
        "url",
        "observation",
        "staged_rejection",
    ],
)
def test_receipt_or_artifact_faults_refuse(tmp_path, fault):
    _, path, artifact, manifest, report = setup(tmp_path)
    proof = artifact["sources"][0]["currentInventoryProof"]
    if fault == "same_count_different_id":
        proof["nativeIdsByLifecycle"]["available"] = ["11"]
    elif fault == "duplicate_id":
        proof["nativeIdsByLifecycle"]["available"] = ["10", "10"]
    elif fault == "closed_overlap":
        proof["nativeIdsByLifecycle"]["closed"] = ["10"]
    elif fault == "terminal_missing":
        proof.pop("hasNextPage")
    elif fault == "terminal_true":
        proof["hasNextPage"] = True
    elif fault == "digest":
        proof["emittedNativeIdsSha256"] = "d" * 64
    elif fault == "endpoint":
        proof["endpoint"] += "&paged=2"
    elif fault == "lease_missing":
        artifact["sources"].pop()
    elif fault == "lease_count":
        artifact["sources"][1]["currentInventoryProof"]["count"] = 1
    elif fault == "cap":
        artifact["runMeta"]["maxItemsPerSource"] = 2
    elif fault == "source_error":
        artifact["sources"][0]["error"] = "Synthetic failure"
    elif fault == "stale":
        proof["observedAt"] = "2000-01-01T00:00:00Z"
    elif fault == "generation":
        proof["generationId"] = "other"
    elif fault == "lifecycle":
        artifact["listings"][0]["statusBadge"] = "Closed"
    elif fault == "url":
        artifact["listings"][0]["url"] = "https://example.test/foreign"
    elif fault == "observation":
        artifact["listings"][0]["inventoryObservedAt"] = "2000-01-01T00:00:00Z"
    elif fault == "staged_rejection":
        artifact["listings"][0].pop("url")
        artifact["listings"][0].pop("canonicalUrl")
    path.write_text(json.dumps(artifact))
    manifest["sources"][SOURCE]["artifact"]["sha256"] = cp.sha256_file(path)
    manifest["sources"][SOURCE]["artifact"]["bytes"] = path.stat().st_size
    with pytest.raises(ValueError):
        admit(path, manifest, report)


@pytest.mark.parametrize(
    "fault",
    [
        "baseline",
        "source_error_reason",
        "dry_run",
        "torow_errors",
        "count",
        "code",
        "target",
        "policy",
        "artifact_hash",
    ],
)
def test_live_baseline_or_binding_drift_refuses(tmp_path, fault):
    _, path, _, manifest, report = setup(tmp_path)
    info = report["per_source"][SOURCE]
    if fault == "baseline":
        info.update(
            verdict="hold",
            reason="current_active 2 below floor 100",
            baseline_median=100,
        )
    elif fault == "source_error_reason":
        info["reason"] += "; a source pass also errored this run"
    elif fault == "dry_run":
        report["summary"]["mode"] = "dry_run"
    elif fault == "torow_errors":
        report["summary"]["torow_errors"] = 1
    elif fault == "count":
        info["current_active"] = 3
    elif fault == "code":
        manifest["collector_git_sha"] = "d" * 40
    elif fault == "policy":
        manifest["config"]["current_inventory_admission"]["source_policy_sha256"] = (
            "d" * 64
        )
    elif fault == "artifact_hash":
        manifest["sources"][SOURCE]["artifact"]["sha256"] = "d" * 64
    elif fault == "target":
        prior = admit(path, manifest, report)
        manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": prior}
        manifest["preflight"]["database_target"]["value"] = "d" * 64
        report["per_source"][SOURCE]["verdict"] = "first_seen"
        report["per_source"][SOURCE]["reason"] = (
            "no baseline row; cannot gate (first sight)"
        )
        with pytest.raises(ValueError):
            admit(path, manifest, report, phase="aggregate")
        return
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def test_checkpoint_source_and_aggregate_hooks_admit_exact_receipt(
    tmp_path, monkeypatch
):
    run, path, _, manifest, report = setup(tmp_path)
    monkeypatch.setattr(cp, "git_identity", lambda: (CODE, False))

    def gate(_argv, _log, **_kwargs):
        output = run / (
            "gates/interra-realty.json"
            if "interra-realty.json" in _argv[_argv.index("--out") + 1]
            else "aggregate-gate.json"
        )
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(copy.deepcopy(report)))
        return 0

    monkeypatch.setattr(cp, "run_command", gate)
    cp.gate_source(run, manifest, SOURCE, path, None)
    assert cp.gate_verdict_is_admitted(
        manifest, manifest["sources"][SOURCE]["gate"]["verdict"]
    )
    assert manifest["sources"][SOURCE]["gate"]["raw_verdict"] == "first_seen"
    cp.run_aggregate_gate(run, manifest, None)
    assert manifest["aggregate_gate"]["non_ok_sources"] == []
    assert manifest["aggregate_gate"]["mark_missing_safe_brokerages"] == []
    assert manifest["scope"]["whole_source_coverage"] is False


def test_baseline_appearing_between_gates_refuses_before_ingest(tmp_path, monkeypatch):
    run, path, _, manifest, report = setup(tmp_path)
    monkeypatch.setattr(cp, "git_identity", lambda: (CODE, False))

    def gate(argv, _log, **_kwargs):
        current = copy.deepcopy(report)
        if "gates" not in argv[argv.index("--out") + 1]:
            current["per_source"][SOURCE].update(
                verdict="ok", reason=None, baseline_median=2
            )
        output = (
            run / argv[argv.index("--out") + 1]
            if not argv[argv.index("--out") + 1].startswith("/")
            else Path(argv[argv.index("--out") + 1])
        )
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(current))
        return 0

    monkeypatch.setattr(cp, "run_command", gate)
    cp.gate_source(run, manifest, SOURCE, path, None)
    with pytest.raises(cp.GlobalStageError, match="current-inventory"):
        cp.run_aggregate_gate(run, manifest, None)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--sources", "svn"],
        ["--sources", "all"],
        ["--sources", SOURCE, "--transactions", "sale"],
        ["--sources", SOURCE, "--admit-baseline-hold-additively"],
        ["--sources", SOURCE, "--allow-dirty"],
    ],
)
def test_opt_in_refused_before_credentials_or_output_creation(monkeypatch, arguments):
    def forbidden(*_args, **_kwargs):
        pytest.fail("credentials or outputs touched before opt-in validation")

    monkeypatch.setattr(cp, "database_target_fingerprint", forbidden)
    monkeypatch.setattr(cp, "create_fresh_run_dir", forbidden)
    with pytest.raises(SystemExit) as result:
        cp.main([*arguments, "--admit-current-inventory-source", SOURCE])
    assert result.value.code == 2


def test_default_resume_omits_new_option_and_opt_in_identity_cannot_drift(tmp_path):
    run, _, _, manifest, _ = setup(tmp_path)
    manifest["config"].pop("current_inventory_admission")
    cp.save_manifest(run, manifest)
    args = {
        "git_sha": CODE,
        "sources": [SOURCE],
        "page_cap": 400,
        "concurrency": 1,
        "database_target": {"algorithm": "sha256", "value": TARGET},
    }
    loaded = cp.load_resume_manifest(run / "manifest.json", **args)
    assert loaded["config"] == manifest["config"]
    with pytest.raises(cp.RefreshError, match="configuration differs"):
        cp.load_resume_manifest(
            run / "manifest.json", **args, admit_current_inventory_source=SOURCE
        )


def test_historical_certificate_refuses_current_inventory_scope(tmp_path):
    import cre_freshness_certificate as certificate
    from test_cre_freshness_certificate import NOW, _run

    run = _run(tmp_path, SOURCE)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["scope"]["kind"] = "collector_registry_additive_current_inventory"
    manifest["sources"][SOURCE]["gate"].update(
        verdict="ok_additive_current_inventory", raw_verdict="first_seen"
    )
    path.write_text(json.dumps(manifest))
    result = certificate.build_freshness_certificate(
        [run], now=NOW, max_source_age_hours=2
    )
    assert result["status"] == "invalid"
    assert any(failure["code"] == "subset_scope" for failure in result["failures"])


def test_actual_mocked_collect_artifact_passes_both_checkpoint_gates(
    tmp_path, monkeypatch
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the offline producer/consumer contract")
    run = tmp_path / "2026-10-14T120000Z"
    run.mkdir()
    start = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    path = run / "artifact.json"
    feed = {
        "success": True,
        "has_next_page": False,
        "posts": [
            {
                "ID": native,
                "post_title": "Synthetic",
                "permalink": f"https://interrarealty.com/listing/synthetic-{native}/",
                "meta": {"listing_status": status, "key_details": "0"},
            }
            for native, status in [
                (2, "available"),
                (10, "under_contract"),
                (31, "closed"),
            ]
        ],
    }
    preload = tmp_path / "transport.mts"
    preload.write_text(f"""globalThis.fetch = async (url) => {{
 if (String(url) !== {json.dumps(ENDPOINT)}) throw new Error("Unexpected offline endpoint");
 return new Response({json.dumps(json.dumps(feed))});
}};""")
    collector = Path(cp.__file__).parent
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG")
        if key in os.environ
    }
    env.update(
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_REFRESH_GENERATION=run.name,
        CRE_REFRESH_STARTED_AT=start,
    )
    child = subprocess.run(
        [
            node,
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
            "--concurrency",
            "1",
            "--out",
            str(path),
        ],
        cwd=collector,
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    artifact = json.loads(path.read_text())
    assert (
        artifact["sources"][0]["currentInventoryProof"]["emittedNativeIdsSha256"]
        == hashlib.sha256(b'["10","2"]').hexdigest()
    )
    stats = cp.validate_source_artifact(
        path,
        SOURCE,
        start,
        expected_generation_id=run.name,
        expected_generation_started_at=start,
        expected_transactions=("sale", "lease"),
    )
    manifest = cp.new_manifest(
        run,
        git_sha=CODE,
        git_dirty=False,
        sources=[SOURCE],
        page_cap=400,
        concurrency=1,
        database_target={"algorithm": "sha256", "value": TARGET},
        admit_current_inventory_source=SOURCE,
    )
    manifest["started_at"] = start
    manifest["sources"][SOURCE]["artifact"] = {
        **stats,
        "path": "artifact.json",
        "attempt_started_at": start,
    }
    report = {
        "generated_for_artifact": str(path),
        "per_source": {
            SOURCE: {
                "current_active": 2,
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

    def gate(argv, _log, **_kwargs):
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report))
        assert "--update-baseline" not in argv
        assert argv[argv.index("--expected-db-target-sha256") + 1] == TARGET
        return 0

    monkeypatch.setattr(cp, "run_command", gate)
    monkeypatch.setattr(cp, "git_identity", lambda: (CODE, False))
    cp.gate_source(run, manifest, SOURCE, path, None)
    cp.run_aggregate_gate(run, manifest, None)
    assert (
        manifest["sources"][SOURCE]["gate"]["verdict"]
        == "ok_additive_current_inventory"
    )
    assert manifest["aggregate_gate"]["admission_scope"] == "additive_current_inventory"
    assert manifest["aggregate_gate"]["non_ok_sources"] == []
