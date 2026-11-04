"""Real no-network producer/artifact/gate boundaries for Buildout admission."""

import copy
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cre_buildout_current_inventory as buildout
import cre_checkpoint_refresh as cp
import cre_current_inventory_admission as admission
import pytest

SOURCE = "faris-lee"
CODE = "a" * 40
TARGET = "b" * 64


def setup(tmp_path, *, empty_lease=False, listing_host=None):
    now = datetime.now(UTC)
    start = (now - timedelta(minutes=2)).isoformat()
    observed = (now - timedelta(minutes=1)).isoformat()
    run = tmp_path / "2026-10-15T120000Z"
    run.mkdir()
    definition = buildout.definitions()[SOURCE]
    items = []
    for native, sale, closed, under, property_id in [
        ("10", True, False, True, "dual-sale"),
        ("20", False, False, False, "dual-lease"),
        ("30", False, False, False, "other-lease"),
        ("40", True, True, False, "closed-sale"),
    ]:
        items.append(
            {
                "id": native,
                "showLink": f"https://{listing_host or definition['host']}/listings/?propertyId={property_id}",
                "sale": sale,
                "closed": closed,
                "underContract": under,
                "alsoForSaleOrLease": False,
            }
        )
    if empty_lease:
        items = [i for i in items if i["sale"]]
    pages = []
    for index in range((len(items) + 1) // 2):
        pages.append(
            {
                "index": index,
                "endpoint": f"https://buildout.com/plugins/{definition['plugin']}/inventory.json?page={index}&q%5Bs%5D%5B%5D=created_at+asc%2C+id+asc",
                "observedAt": observed,
                "generationId": run.name,
                "cacheDisposition": "live",
                "transport": "direct_json",
                "parsedJsonSha256": "c" * 64,
                "total": len(items),
                "limit": 2,
                "rows": items[index * 2 : index * 2 + 2],
            }
        )
    listings = []
    for item in items:
        if item["closed"]:
            continue
        listings.append(
            {
                "sourceKey": SOURCE,
                "sourceCompany": definition["company"],
                "id": item["id"],
                "name": "Synthetic",
                "transactionMode": "sale" if item["sale"] else "lease",
                "transactionType": "Sale" if item["sale"] else "Lease",
                "url": item["showLink"],
                "canonicalUrl": item["showLink"],
                "underContract": item["underContract"],
                "statusBadge": "under_contract" if item["underContract"] else None,
                "inventoryObservedAt": observed,
                "preserveChildCollections": True,
                "freshnessProvenance": {
                    "generationId": run.name,
                    "cacheDisposition": "live",
                    "method": "buildout_inventory_feed",
                    "detailScope": "authoritative_inventory_feed",
                },
            }
        )
    entries = []
    for tx in ("sale", "lease"):
        count = sum(r["transactionMode"] == tx for r in listings)
        entries.append(
            {
                "sourceKey": SOURCE,
                "transaction": tx,
                "supported": True,
                "truncated": False,
                "listingsCollected": count,
                "totalAvailableOnSource": len(items),
                "freshness": {
                    "listings": count,
                    "detailErrors": 0,
                    "staleInventoryObservations": 0,
                    "staleDetailObservations": 0,
                    "inventoryObserved": count,
                    "detailObserved": 0,
                    "authoritativeInventoryFeed": count,
                    "childPreservationRows": count,
                },
                "currentInventoryProof": {
                    "schema": buildout.SCHEMA,
                    "kind": "complete_paginated_feed",
                    "sourceKey": SOURCE,
                    "transaction": tx,
                    "generationId": run.name,
                    "pluginKey": definition["plugin"],
                    "listingsPage": definition["page"],
                    "sort": buildout.SORT,
                    "total": len(items),
                    "limit": 2,
                    "pages": copy.deepcopy(pages),
                },
            }
        )
    artifact = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": start,
            "finishedAt": now.isoformat(),
            "freshness": {
                "generationId": run.name,
                "generationStartedAt": start,
                "requireFreshDetails": True,
            },
        },
        "sources": entries,
        "listings": listings,
        "brokers": [],
        "totalListings": len(listings),
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
        admit_current_inventory_source=SOURCE,
    )
    manifest["started_at"] = start
    refresh(path, artifact, manifest)
    report = {
        "generated_for_artifact": str(path),
        "per_source": {
            SOURCE: {
                "current_active": len(listings),
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
        current_code_sha=CODE,
        current_git_dirty=False,
        phase=phase,
    )


def test_native_partition_and_actual_staging_merge_are_separate(tmp_path):
    _, path, _, manifest, report = setup(tmp_path)
    proof = admit(path, manifest, report)
    assert (proof["raw_feed_count"], proof["emitted_count"], proof["staged_count"]) == (
        4,
        3,
        2,
    )
    assert report["per_source"][SOURCE]["raw_verdict"] == "first_seen"
    assert report["per_source"][SOURCE]["effective_floor"] == 100
    assert report["per_source"][SOURCE]["mark_missing_safe"] is False
    assert manifest["scope"]["whole_source_coverage"] is False
    manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": proof}
    report["per_source"][SOURCE]["verdict"] = "first_seen"
    report["per_source"][SOURCE]["reason"] = (
        "no baseline row; cannot gate (first sight)"
    )
    assert admit(path, manifest, report, "aggregate") == proof


def test_empty_transaction_is_proven_from_complete_raw_feed(tmp_path):
    _, path, artifact, manifest, report = setup(tmp_path, empty_lease=True)
    assert artifact["sources"][1]["listingsCollected"] == 0
    assert admit(path, manifest, report)["staged_count"] == 1


@pytest.mark.parametrize(
    ("source", "published_host"),
    [
        ("nai-bergman", "bergmancommercial.com"),
        ("nai-plotkin", "www.sdplotkin.com"),
        ("cawley-chicago", "cawleycre.com"),
    ],
)
def test_published_buildout_hosts_pass_source_and_aggregate(
    tmp_path, monkeypatch, source, published_host
):
    monkeypatch.setattr(f"{__name__}.SOURCE", source)
    _, path, _, manifest, report = setup(tmp_path, listing_host=published_host)
    proof = admit(path, manifest, report)
    assert proof["staged_count"] == 2
    manifest["sources"][source]["gate"] = {"current_inventory_proof": proof}
    report["per_source"][source].update(
        verdict="first_seen", reason="no baseline row; cannot gate (first sight)"
    )
    assert admit(path, manifest, report, "aggregate") == proof


@pytest.mark.parametrize("source", ["nai-bergman", "nai-plotkin", "cawley-chicago"])
@pytest.mark.parametrize("phase", ["source", "aggregate"])
def test_published_source_still_refuses_unowned_domain(
    tmp_path, monkeypatch, source, phase
):
    monkeypatch.setattr(f"{__name__}.SOURCE", source)
    _, path, artifact, manifest, report = setup(tmp_path)
    if phase == "aggregate":
        proof = admit(path, manifest, report)
        manifest["sources"][source]["gate"] = {"current_inventory_proof": proof}
        report["per_source"][source].update(
            verdict="first_seen", reason="no baseline row; cannot gate (first sight)"
        )
    for entry in artifact["sources"]:
        for page in entry["currentInventoryProof"]["pages"]:
            for row in page["rows"]:
                row["showLink"] = row["showLink"].replace(
                    buildout.definitions()[source]["host"], "attacker.invalid"
                )
    for listing in artifact["listings"]:
        for field in ("url", "canonicalUrl"):
            listing[field] = listing[field].replace(
                buildout.definitions()[source]["host"], "attacker.invalid"
            )
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report, phase)


@pytest.mark.parametrize("source", ["nai-bergman", "nai-plotkin", "cawley-chicago"])
@pytest.mark.parametrize("phase", ["source", "aggregate"])
@pytest.mark.parametrize("coordinate", ["pluginKey", "listingsPage"])
def test_published_source_still_refuses_exact_proof_coordinate_drift(
    tmp_path, monkeypatch, source, phase, coordinate
):
    monkeypatch.setattr(f"{__name__}.SOURCE", source)
    _, path, artifact, manifest, report = setup(tmp_path)
    if phase == "aggregate":
        proof = admit(path, manifest, report)
        manifest["sources"][source]["gate"] = {"current_inventory_proof": proof}
        report["per_source"][source].update(
            verdict="first_seen", reason="no baseline row; cannot gate (first sight)"
        )
    for entry in artifact["sources"]:
        entry["currentInventoryProof"][coordinate] = (
            "d" * 40 if coordinate == "pluginKey" else "https://attacker.invalid/"
        )
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report, phase)


@pytest.mark.parametrize(
    "fault",
    [
        "gap",
        "index",
        "total",
        "limit",
        "duplicate",
        "equal_count_id",
        "endpoint",
        "empty_extra_query",
        "empty_property_key",
        "duplicate_empty_property_key",
        "sort",
        "plugin",
        "host",
        "missing_flag",
        "truthy_flag",
        "unknown_transport",
        "digest",
        "stale",
        "future",
        "generation",
        "cached_generation",
        "missing_receipt",
        "closed_emitted",
        "cap",
        "same_mode_collision",
        "third_identity_collision",
        "native_url_mismatch",
        "pass_mismatch",
        "invented_detail",
        "dropped_active",
        "empty_pass_lie",
        "wrong_source",
        "cache_disposition",
    ],
)
def test_unproven_inventory_cannot_reach_admission(tmp_path, fault):
    _, path, artifact, manifest, report = setup(tmp_path)
    proof = artifact["sources"][0]["currentInventoryProof"]
    page, row = proof["pages"][0], proof["pages"][0]["rows"][0]
    if fault == "gap":
        proof["pages"].pop()
    elif fault == "index":
        page["index"] = 1
    elif fault == "total":
        proof["total"] += 1
    elif fault == "limit":
        page["limit"] += 1
    elif fault == "duplicate":
        proof["pages"][1]["rows"][0]["id"] = row["id"]
    elif fault == "equal_count_id":
        artifact["listings"][0]["id"] = "11"
    elif fault == "endpoint":
        page["endpoint"] += "&extra=1"
    elif fault == "empty_extra_query":
        page["endpoint"] += "&extra="
    elif fault == "empty_property_key":
        row["showLink"] = row["showLink"].replace("dual-sale", "-sale")
    elif fault == "duplicate_empty_property_key":
        row["showLink"] += "&propertyId="
    elif fault == "sort":
        proof["sort"] = "id desc"
    elif fault == "plugin":
        proof["pluginKey"] = "d" * 40
    elif fault == "host":
        row["showLink"] = "https://attacker.invalid/?propertyId=dual-sale"
    elif fault == "missing_flag":
        row.pop("closed")
    elif fault == "truthy_flag":
        row["closed"] = 0
    elif fault == "unknown_transport":
        page["transport"] = "guessed"
    elif fault == "digest":
        page["parsedJsonSha256"] = "bogus"
    elif fault == "stale":
        page["observedAt"] = "2000-01-01T00:00:00Z"
    elif fault == "future":
        page["observedAt"] = "2099-01-01T00:00:00Z"
    elif fault == "generation":
        proof["generationId"] = "other"
    elif fault == "cached_generation":
        page["generationId"] = "other"
    elif fault == "missing_receipt":
        artifact["sources"][0].pop("currentInventoryProof")
    elif fault == "closed_emitted":
        row["closed"] = True
    elif fault == "cap":
        artifact["runMeta"]["maxItemsPerSource"] = 3
    elif fault == "same_mode_collision":
        proof["pages"][0]["rows"][1]["sale"] = True
        artifact["listings"][1].update(transactionMode="sale", transactionType="Sale")
        for entry in artifact["sources"]:
            entry["listingsCollected"] = sum(
                r["transactionMode"] == entry["transaction"]
                for r in artifact["listings"]
            )
    elif fault == "third_identity_collision":
        old = proof["pages"][1]["rows"][0]
        old["showLink"] = old["showLink"].replace("other-lease", "dual-lease")
        artifact["listings"][2].update(
            url=old["showLink"], canonicalUrl=old["showLink"]
        )
    elif fault == "native_url_mismatch":
        artifact["listings"][0]["url"] += "x"
    elif fault == "pass_mismatch":
        artifact["sources"][1]["currentInventoryProof"]["pages"][0]["rows"][0][
            "underContract"
        ] = False
    elif fault == "invented_detail":
        artifact["listings"][0]["detailObservedAt"] = page["observedAt"]
    elif fault == "dropped_active":
        artifact["listings"].pop()
        artifact["totalListings"] -= 1
    elif fault == "empty_pass_lie":
        artifact["sources"][1]["listingsCollected"] = 0
    elif fault == "wrong_source":
        proof["sourceKey"] = "kiser-group"
    elif fault == "cache_disposition":
        page["cacheDisposition"] = "stale_cache"
    if fault != "pass_mismatch" and "currentInventoryProof" in artifact["sources"][0]:
        artifact["sources"][1]["currentInventoryProof"] = {
            **copy.deepcopy(proof),
            "transaction": "lease",
        }
    refresh(path, artifact, manifest)
    with pytest.raises((ValueError, KeyError)):
        admit(path, manifest, report)


@pytest.mark.parametrize(
    "fault",
    [
        "baseline",
        "reason",
        "count",
        "dry_run",
        "conversion",
        "code",
        "policy",
        "registry",
        "hash",
        "target",
    ],
)
def test_live_gate_and_binding_drift_refuse(tmp_path, fault):
    _, path, _, manifest, report = setup(tmp_path)
    gate = report["per_source"][SOURCE]
    if fault == "baseline":
        gate.update(verdict="ok", baseline_median=2)
    elif fault == "reason":
        gate["reason"] += "; source failed"
    elif fault == "count":
        gate["current_active"] = 2
    elif fault == "dry_run":
        report["summary"]["mode"] = "dry_run"
    elif fault == "conversion":
        report["summary"]["torow_errors"] = 1
    elif fault == "code":
        manifest["collector_git_sha"] = "d" * 40
    elif fault == "policy":
        manifest["config"]["current_inventory_admission"]["source_policy_sha256"] = (
            "d" * 64
        )
    elif fault == "registry":
        manifest["config"]["current_inventory_admission"][
            "buildout_registry_sha256"
        ] = "d" * 64
    elif fault == "hash":
        manifest["sources"][SOURCE]["artifact"]["sha256"] = "d" * 64
    elif fault == "target":
        proof = admit(path, manifest, report)
        manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": proof}
        manifest["preflight"]["database_target"]["value"] = "d" * 64
        gate.update(
            verdict="first_seen", reason="no baseline row; cannot gate (first sight)"
        )
        with pytest.raises(ValueError):
            admit(path, manifest, report, "aggregate")
        return
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def test_checkpoint_rechecks_buildout_at_both_gates(tmp_path, monkeypatch):
    run, path, _, manifest, report = setup(tmp_path)
    monkeypatch.setattr(cp, "git_identity", lambda: (CODE, False))

    def command(argv, _log, **_kwargs):
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report))
        return 0

    monkeypatch.setattr(cp, "run_command", command)
    cp.gate_source(run, manifest, SOURCE, path, None)
    assert manifest["sources"][SOURCE]["gate"]["raw_verdict"] == "first_seen"
    cp.run_aggregate_gate(run, manifest, None)
    assert manifest["aggregate_gate"]["non_ok_sources"] == []
    assert manifest["scope"]["whole_source_coverage"] is False


@pytest.mark.parametrize("source", list(buildout.definitions()))
def test_each_allowlisted_source_is_exact_not_a_family_bypass(source):
    admission.validate_option(source, [source], ("sale", "lease"))
    assert admission.configuration(source)["source_key"] == source
    for selected in ([SOURCE, "kiser-group"], ["all"], ["svn"]):
        with pytest.raises(ValueError):
            admission.validate_option(source, selected, ("sale", "lease"))


def test_series_forwards_buildout_and_reconciles_only_exact_scope(tmp_path):
    import cre_checkpoint_series as series
    from test_interra_series_admission import config

    cfg = config()
    cfg["sources"] = [SOURCE]
    cfg["current_inventory_admission"] = admission.configuration(SOURCE)
    argv = series.build_checkpoint_argv(
        SOURCE, child_out_root=tmp_path / "runs", env_file=None, config=cfg
    )
    assert argv[-2:] == ["--admit-current-inventory-source", SOURCE]
    child = {
        "run_id": "child",
        "status": "additive_scope_complete_current_inventory",
        "config": series._expected_child_config(SOURCE, cfg),
        "scope": {
            "kind": "collector_registry_additive_current_inventory",
            "whole_source_coverage": False,
        },
    }
    assert series._child_succeeded(child, cfg)
    for mutation in ("source", "registry", "coverage"):
        changed = copy.deepcopy(child)
        if mutation == "source":
            changed["config"]["current_inventory_admission"]["source_key"] = (
                "kiser-group"
            )
        elif mutation == "registry":
            changed["config"]["current_inventory_admission"][
                "buildout_registry_sha256"
            ] = "d" * 64
        else:
            changed["scope"]["whole_source_coverage"] = True
        assert not series._child_succeeded(changed, cfg)


def test_historical_certificate_still_refuses_buildout_inventory_admission(tmp_path):
    import cre_freshness_certificate as certificate
    from test_cre_freshness_certificate import NOW, _run

    run = _run(tmp_path, SOURCE)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["scope"]["kind"] = "collector_registry_additive_current_inventory"
    path.write_text(json.dumps(manifest))
    result = certificate.build_freshness_certificate(
        [run], now=NOW, max_source_age_hours=2
    )
    assert result["status"] == "invalid"
    assert any(f["code"] == "subset_scope" for f in result["failures"])


@pytest.mark.parametrize(
    "url_pattern,accepted,stored_ids",
    [
        ("https://{host}/listings/?propertyId={pid}", True, {"one", "two"}),
        ("http://{host}/listings/?propertyId={pid}", True, {"one", "two"}),
        ("https://{host}/listings/?propertyId=&propertyId={pid}", True, {"one", "two"}),
        ("https://{host}/properties/{pid}/", True, {"10", "20"}),
        ("https://foreign.example/listings/?propertyId={pid}", False, None),
    ],
)
def test_actual_mocked_http_collect_artifact_enters_source_and_aggregate_gates(
    tmp_path, monkeypatch, url_pattern, accepted, stored_ids
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for actual offline collector contract")
    run, path, _, manifest, report = setup(tmp_path)
    start = manifest["started_at"]
    definition = buildout.definitions()[SOURCE]
    feed = {
        "meta": {"total": 2, "limit": 30},
        "inventory": [
            {
                "id": 10,
                "sale": True,
                "closed": False,
                "under_contract": False,
                "also_for_sale_or_lease": False,
                "show_link": url_pattern.format(
                    host=definition["host"], pid="one-sale"
                ),
                "display_name": "Synthetic",
            },
            {
                "id": 20,
                "sale": False,
                "closed": False,
                "under_contract": True,
                "also_for_sale_or_lease": False,
                "show_link": url_pattern.format(
                    host=definition["host"], pid="two-lease"
                ),
                "display_name": "Synthetic",
            },
        ],
    }
    preload = tmp_path / "transport.mts"
    preload.write_text(
        f'globalThis.fetch=async(url)=>{{if(!String(url).startsWith("https://buildout.com/plugins/{definition["plugin"]}/inventory.json?"))throw new Error("Unexpected offline endpoint");return new Response({json.dumps(json.dumps(feed))});}};'
    )
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG")
        if key in os.environ
    }
    env.update(
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_REFRESH_GENERATION=run.name,
        CRE_REFRESH_STARTED_AT=start,
        BUILDOUT_CACHE_DIR=str(tmp_path / "cache"),
    )
    # The setup placeholder must not preempt the collector's private exclusive create.
    path.unlink()
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
        cwd=Path(cp.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    artifact = json.loads(path.read_text())
    report["per_source"][SOURCE]["current_active"] = 2
    if accepted:
        assert artifact["sources"][0]["currentInventoryProof"]["total"] == 2
        assert {row["url"] for row in artifact["listings"]} == {
            row["show_link"] for row in feed["inventory"]
        }
        assert {
            buildout.to_row(row, {}, artifact["runMeta"]["finishedAt"])["external_id"]
            for row in artifact["listings"]
        } == stored_ids
    else:
        assert all(
            "currentInventoryProof" not in entry for entry in artifact["sources"]
        )
    manifest["sources"][SOURCE]["artifact"] = {
        **cp.validate_source_artifact(
            path,
            SOURCE,
            start,
            expected_generation_id=run.name,
            expected_generation_started_at=start,
            expected_transactions=("sale", "lease"),
        ),
        "path": path.name,
        "attempt_started_at": start,
    }
    monkeypatch.setattr(cp, "git_identity", lambda: (CODE, False))

    def command(argv, _log, **_kwargs):
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report))
        return 0

    monkeypatch.setattr(cp, "run_command", command)
    if not accepted:
        with pytest.raises(cp.GlobalStageError):
            cp.gate_source(run, manifest, SOURCE, path, None)
        return
    cp.gate_source(run, manifest, SOURCE, path, None)
    cp.run_aggregate_gate(run, manifest, None)
    assert (
        manifest["sources"][SOURCE]["gate"]["current_inventory_proof"]["staged_count"]
        == 2
    )


@pytest.mark.parametrize(
    "host", ["hudsonpeters.com", "www.hudsonpeters.com", "foreign.example"]
)
def test_hudson_admission_allows_only_bare_www_listing_equivalence(
    tmp_path, monkeypatch, host
):
    monkeypatch.setitem(globals(), "SOURCE", "hudson-peters")
    _, path, artifact, manifest, report = setup(tmp_path)
    for entry in artifact["sources"]:
        for page in entry["currentInventoryProof"]["pages"]:
            for row in page["rows"]:
                property_id = row["showLink"].split("propertyId=", 1)[1]
                row["showLink"] = f"https://{host}/listings/?propertyId={property_id}"
    for row in artifact["listings"]:
        property_id = row["url"].split("propertyId=", 1)[1]
        row["url"] = row["canonicalUrl"] = (
            f"https://{host}/listings/?propertyId={property_id}"
        )
    refresh(path, artifact, manifest)
    if host in {"hudsonpeters.com", "www.hudsonpeters.com"}:
        proof = admit(path, manifest, report)
        assert proof["staged_count"] == 2
        manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": proof}
        report["per_source"][SOURCE]["verdict"] = "first_seen"
        report["per_source"][SOURCE]["reason"] = (
            "no baseline row; cannot gate (first sight)"
        )
        assert admit(path, manifest, report, "aggregate") == proof
    else:
        with pytest.raises(ValueError):
            admit(path, manifest, report)


HOST_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "buildout-listing-hosts.json").read_text()
)


@pytest.mark.parametrize("vector", HOST_VECTORS)
def test_buildout_listing_host_vectors_preserve_urls_and_admission(
    tmp_path, monkeypatch, vector
):
    original = buildout.definitions()
    original[SOURCE] = {
        **original[SOURCE],
        "host": vector["host"],
        "page": f"https://{vector['host']}/",
    }
    monkeypatch.setattr(buildout, "definitions", lambda: original)
    _, path, artifact, manifest, report = setup(tmp_path)
    for entry in artifact["sources"]:
        for page in entry["currentInventoryProof"]["pages"]:
            for row in page["rows"]:
                property_id = row["showLink"].split("propertyId=", 1)[1]
                row["showLink"] = vector["url"].replace("687920-lease", property_id)
    for row in artifact["listings"]:
        property_id = row["url"].split("propertyId=", 1)[1]
        row["url"] = row["canonicalUrl"] = vector["url"].replace(
            "687920-lease", property_id
        )
    refresh(path, artifact, manifest)
    if vector.get("inventoryOwned", vector["owned"]):
        proof = admit(path, manifest, report)
        assert proof["staged_count"] == 2
        assert (
            artifact["listings"][0]["url"].split("/listings/", 1)[0]
            == vector["url"].split("/listings/", 1)[0]
        )
        manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": proof}
        report["per_source"][SOURCE]["verdict"] = "first_seen"
        report["per_source"][SOURCE]["reason"] = (
            "no baseline row; cannot gate (first sight)"
        )
        assert admit(path, manifest, report, "aggregate") == proof
    else:
        with pytest.raises(ValueError):
            admit(path, manifest, report)


@pytest.mark.parametrize("coordinate", ["listingsPage", "endpoint"])
def test_listing_www_equivalence_does_not_rebind_proof_coordinates(
    tmp_path, coordinate
):
    _, path, artifact, manifest, report = setup(tmp_path)
    for entry in artifact["sources"]:
        proof = entry["currentInventoryProof"]
        if coordinate == "listingsPage":
            proof[coordinate] = proof[coordinate].replace("https://www.", "https://")
        else:
            for page in proof["pages"]:
                page[coordinate] = page[coordinate].replace(
                    "https://buildout.com/", "https://www.buildout.com/"
                )
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def test_path_inventory_refuses_one_url_with_different_native_identities(tmp_path):
    _, path, artifact, manifest, report = setup(tmp_path)
    host = buildout.definitions()[SOURCE]["host"]
    for entry in artifact["sources"]:
        for page in entry["currentInventoryProof"]["pages"]:
            for row in page["rows"]:
                row["showLink"] = f"https://{host}/properties/shared-property/"
    for row in artifact["listings"]:
        row["url"] = row["canonicalUrl"] = f"https://{host}/properties/shared-property/"
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report)


def test_query_inventory_refuses_dual_modes_on_different_property_paths(tmp_path):
    _, path, artifact, manifest, report = setup(tmp_path)
    for entry in artifact["sources"]:
        for page in entry["currentInventoryProof"]["pages"]:
            for row in page["rows"]:
                if row["id"] == "20":
                    row["showLink"] = row["showLink"].replace(
                        "/listings/", "/different-property/"
                    )
    for row in artifact["listings"]:
        if row["id"] == "20":
            row["url"] = row["canonicalUrl"] = row["url"].replace(
                "/listings/", "/different-property/"
            )
    refresh(path, artifact, manifest)
    with pytest.raises(ValueError):
        admit(path, manifest, report)


INVENTORY_URL_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "buildout-inventory-urls.json").read_text()
)


@pytest.mark.parametrize("vector", INVENTORY_URL_VECTORS, ids=lambda row: row["name"])
def test_shared_inventory_url_grammar_preserves_actual_ingest_identity(vector):
    if vector["accepted"]:
        assert buildout._owned_url(vector["url"], "broker.example") == vector["url"]
        transformed = buildout.to_row(
            {
                "sourceKey": SOURCE,
                "id": "968697",
                "url": vector["url"],
                "transactionType": "Lease",
            },
            {},
            datetime.now(UTC).isoformat(),
        )
        assert transformed["external_id"] == vector["externalId"]
        assert transformed["source_url"] == vector["url"]
    else:
        with pytest.raises(ValueError):
            buildout._owned_url(vector["url"], "broker.example")


@pytest.mark.parametrize("phase", ["source", "aggregate"])
@pytest.mark.parametrize(
    "sale_query,lease_query,accepted",
    [
        ("unit=A", "unit=B", False),
        ("tag=A&tag=B", "tag=B&tag=A", False),
        ("unit=A&tag=B", "unit=A&tag=B", True),
    ],
)
def test_dual_mode_inventory_preserves_unrelated_query_routing(
    tmp_path, phase, sale_query, lease_query, accepted
):
    _, path, artifact, manifest, report = setup(tmp_path)
    if phase == "aggregate":
        initial = admit(path, manifest, report)
        manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": initial}
        report["per_source"][SOURCE]["verdict"] = "first_seen"
        report["per_source"][SOURCE]["reason"] = (
            "no baseline row; cannot gate (first sight)"
        )
        assert admit(path, manifest, report, "aggregate") == initial
    for entry in artifact["sources"]:
        for page in entry["currentInventoryProof"]["pages"]:
            for row in page["rows"]:
                if row["id"] in {"10", "20"}:
                    row["showLink"] += "&" + (
                        sale_query if row["id"] == "10" else lease_query
                    )
    for row in artifact["listings"]:
        if row["id"] in {"10", "20"}:
            row["url"] = row["canonicalUrl"] = (
                row["url"] + "&" + (sale_query if row["id"] == "10" else lease_query)
            )
    refresh(path, artifact, manifest)
    report["per_source"][SOURCE]["verdict"] = "first_seen"
    report["per_source"][SOURCE]["reason"] = (
        "no baseline row; cannot gate (first sight)"
    )
    if accepted:
        if phase == "aggregate":
            current = admit(path, manifest, report)
            manifest["sources"][SOURCE]["gate"] = {"current_inventory_proof": current}
            report["per_source"][SOURCE]["verdict"] = "first_seen"
            report["per_source"][SOURCE]["reason"] = (
                "no baseline row; cannot gate (first sight)"
            )
        assert admit(path, manifest, report, phase)["staged_count"] == 2
    else:
        with pytest.raises(ValueError):
            admit(path, manifest, report, phase)
