"""DAUM unknown tenure must not be inferred from the collection pass."""

from pathlib import Path

import pytest

from cre_ingest import transaction_type_of


def worker_validate(path, started, generation, tmp_path, *, enabled=True):
    """Call the actual artifact-worker CLI against a contained artifact."""
    import json
    import subprocess
    import sys

    request = tmp_path / "validate-request.json"
    response = tmp_path / "validate-response.json"
    request.write_text(
        json.dumps(
            {
                "request_id": "test-daum-retained",
                "action": "validate",
                "arguments": {
                    "path": str(path),
                    "source": "daum-commercial",
                    "attempt_started_at": started,
                    "kwargs": {
                        "require_strict_freshness": True,
                        "expected_generation_id": generation,
                        "expected_generation_started_at": started,
                        "daum_verified_subset": enabled,
                    },
                },
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "cre_pipeline_artifact_worker.py",
            "--request",
            str(request),
            "--response",
            str(response),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(response.read_text())


def test_unknown_tenure_has_no_transaction_type():
    listing = {
        "sourceKey": "daum-commercial",
        "transactionMode": "sale",
        "daumUnknownTenure": True,
        "daumTenureEvidence": {
            "inventory": "unknown",
            "detail": "unknown",
            "resolved": "unknown",
        },
    }
    assert transaction_type_of(listing) is None


@pytest.mark.parametrize("policy", ["tenure-only-v1", "tenure-and-empty-title-v2"])
def test_actual_producer_admits_unknown_once_without_complete_coverage(
    tmp_path, monkeypatch, policy
):
    import test_essex_current_inventory as helpers
    from test_daum_current_inventory import produce
    from test_daum_verified_subset import admit, context, envelope

    import cre_current_inventory_admission as admission
    import cre_daum_current_inventory as daum
    from cre_ingest import build_sql, to_row

    generated = produce(
        tmp_path,
        total=101,
        all_sale=False,
        unknown_inventory=True,
        unknown_detail=True,
        subset_policy=policy,
    )
    monkeypatch.setattr(helpers, "SOURCE", daum.SOURCE)
    prepared = helpers.setup(tmp_path, generated)
    prepared[0].chmod(0o700)
    prepared[3]["config"]["current_inventory_admission"] = (
        admission.subset_configuration(policy)
    )
    from cre_pipeline_artifact_worker import compute

    validated = worker_validate(prepared[1], generated[2], generated[3], tmp_path)
    assert validated["ok"] is True
    assert validated["value"]["bytes"] == prepared[1].stat().st_size
    refused = worker_validate(
        prepared[1], generated[2], generated[3], tmp_path, enabled=False
    )
    assert refused["ok"] is False and refused["artifact_rejected"] is True

    # Exercise the actual worker's source and aggregate paths, not only the
    # pure DAUM verifier; the aggregate worker must emit usable ingest claims.
    import copy
    import json

    import cre_checkpoint_refresh as refresh

    worker_manifest = copy.deepcopy(prepared[3])
    worker_report = copy.deepcopy(prepared[4])
    monkeypatch.setattr(
        refresh, "git_identity", lambda: (worker_manifest["collector_git_sha"], False)
    )
    source_result = compute(
        "admit",
        {
            "path": str(prepared[1]),
            "run_dir": str(prepared[0]),
            "source": daum.SOURCE,
            "manifest": worker_manifest,
            "report": worker_report,
            "phase": "source",
        },
        prepared[0] / "source-response.json",
    )
    worker_source = json.loads(Path(source_result["report_path"]).read_text())
    source_gate = worker_source["per_source"][daum.SOURCE]
    worker_manifest["sources"][daum.SOURCE]["gate"] = {
        **source_gate,
        "path": Path(source_result["report_path"]).name,
    }
    worker_manifest["sources"][daum.SOURCE]["subset_ingest_attempt_id"] = "e" * 32
    aggregate_result = compute(
        "admit",
        {
            "path": str(prepared[1]),
            "run_dir": str(prepared[0]),
            "source": daum.SOURCE,
            "manifest": worker_manifest,
            "report": worker_source,
            "phase": "aggregate",
        },
        prepared[0] / "aggregate-response.json",
    )
    assert aggregate_result["subset_claims"]["attempt_id"] == "e" * 32
    assert Path(aggregate_result["subset_claims"]["path"]).is_file()

    # Feed the exact worker-produced binding into the real summary projection.
    from cre_notification_summary import _daum_unknown_breakdown, _subset_summary_fields

    worker_binding = source_gate["current_inventory_proof"]
    count = worker_binding["staged_count"]
    summary = _subset_summary_fields(worker_manifest, daum.SOURCE, count)
    breakdown = _daum_unknown_breakdown(worker_manifest, daum.SOURCE, count)
    assert summary["whole_source_coverage"] is False
    assert summary["whole_lease_coverage"] == "unknown"
    assert summary["retained_unknown_tenure_count"] == len(
        prepared[2]["sources"][0]["currentInventoryProof"]["retainedUnknownTenure"]
    )
    assert (
        breakdown["sale"]
        + breakdown["lease"]
        - breakdown["both"]
        + summary["retained_unknown_tenure_count"]
        == count
    )

    # Exercise the complete private-file summary reader using this actual
    # worker binding and artifact, with synthetic settlement/readback receipts.
    import test_daum_subset_reporting as reporting
    import test_notification_summary as support

    import cre_checkpoint_series as series

    summary_evidence = reporting.evidence.__wrapped__(tmp_path / "summary", monkeypatch)
    parent, child = (
        summary_evidence["values"]["parent"],
        summary_evidence["values"]["child"],
    )
    config = series.series_config(
        sources=[daum.SOURCE],
        page_cap=400,
        concurrency=1,
        attempts_per_source=3,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=5,
        cpu_sample_seconds=1,
        nice=10,
        admit_daum_verified_subset=True,
        daum_subset_policy=policy,
    )
    parent["config"] = config
    child["config"] = series._expected_child_config(daum.SOURCE, config)
    target = worker_manifest["preflight"]["database_target"]
    parent["database_target"] = child["preflight"]["database_target"] = target
    parent["collector_git_sha"] = child["collector_git_sha"] = worker_manifest[
        "collector_git_sha"
    ]
    parent["latest_invocation_admission"]["expected_sha"] = worker_manifest[
        "collector_git_sha"
    ]
    summary_evidence["values"]["settlement"]["collector_git_sha"] = worker_manifest[
        "collector_git_sha"
    ]
    checkpoint = child["sources"][daum.SOURCE]
    checkpoint["gate"] = copy.deepcopy(source_gate)
    checkpoint["artifact"].update(
        sha256=worker_binding["artifact_sha256"],
        bytes=prepared[1].stat().st_size,
        staged_unique=count,
        transaction_breakdown=None,
    )
    checkpoint["readback"].update(
        expected_staged_unique=count, latest_inventory_batch_active=count
    )
    summary_evidence["save"]()
    summary_evidence["files"]["artifact"].write_bytes(prepared[1].read_bytes())
    summary_evidence["files"]["artifact"].chmod(0o600)
    actual_summary = support.read(
        summary_evidence, expected_sha=worker_manifest["collector_git_sha"]
    )
    assert (
        actual_summary["retained_unknown_tenure_count"]
        == summary["retained_unknown_tenure_count"]
    )
    assert actual_summary["whole_source_coverage"] is False
    assert actual_summary["whole_lease_coverage"] == "unknown"
    assert actual_summary["transaction_breakdown"] == breakdown

    binding = admit(prepared)
    proof = prepared[2]["sources"][0]["currentInventoryProof"]
    unknown_ids = set(proof["retainedUnknownTenure"])
    assert unknown_ids
    unknown_rows = [
        row for row in prepared[2]["listings"] if row["daumPostId"] in unknown_ids
    ]
    assert len(unknown_rows) == len(unknown_ids)
    assert all(row["transactionMode"] == "sale" for row in unknown_rows)
    assert all(
        to_row(row, {}, prepared[2]["runMeta"]["finishedAt"])["transaction_type"]
        is None
        for row in unknown_rows
    )
    assert binding["whole_source_coverage"] is False
    assert binding["whole_lease_coverage"] == "unknown"
    assert binding["retained_unknown_tenure_count"] == len(unknown_ids)
    assert binding["held_count"] == 0
    assert binding["sale_count"] == sum(
        row[-1] in {"sale", "sale_or_lease"} for row in proof["cohort"]
    )
    assert binding["lease_count"] > 0
    assert (
        len(proof["held"])
        == len(proof["heldInventoryObservations"])
        == len(proof["heldDetailObservations"])
        == len(unknown_ids)
    )
    assert prepared[4]["per_source"][daum.SOURCE]["raw_verdict"] == "first_seen"
    assert prepared[4]["per_source"][daum.SOURCE]["mark_missing_safe"] is False
    claims = context(prepared, envelope(prepared))
    assert all(
        row["emitted"] for row in claims["members"] if row["native_id"] in unknown_ids
    )
    staged = [
        to_row(row, {}, prepared[2]["runMeta"]["finishedAt"])
        for row in prepared[2]["listings"]
    ]
    sql = build_sql(
        staged, [], prepared[3]["started_at"], set(), daum_verified_subset_claims=claims
    )
    assert "DAUM subset historical identity contradiction" in sql
    assert "latestDaumUnknownTenureObservation" in sql
    assert "WHEN EXCLUDED.transaction_type IS NOT NULL" in sql
    assert (
        "DELETE FROM credeals.cre_listing_documents WHERE listing_id IN (SELECT id FROM _child_refresh)"
        in sql
    )

    import json
    import sys

    import cre_checkpoint_refresh as cp
    import cre_ingest as ingest

    claim_file = Path(aggregate_result["subset_claims"]["path"])
    monkeypatch.setattr(
        cp, "git_identity", lambda: (prepared[3]["collector_git_sha"], False)
    )
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *args: pytest.fail("dry run reached database")
    )
    monkeypatch.delenv("CRE_ACTIVATE_STATUS", raising=False)
    out = tmp_path / "sql"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(prepared[1]),
            "--dry-run",
            "--require-strict-freshness",
            "--expected-db-target-sha256",
            "b" * 64,
            "--checkpoint-ingest-attempt",
            "e" * 32,
            "--daum-verified-subset-claims",
            str(claim_file),
            "--keep-artifacts",
            str(out),
        ],
    )
    ingest.main()
    assert "latestDaumUnknownTenureObservation" in (out / "ingest.sql").read_text()


def test_unknown_arrays_do_not_invent_primary_but_known_daum_behavior_is_unchanged():
    from cre_ingest import to_row

    listing = {
        "sourceKey": "daum-commercial",
        "id": "property/example",
        "url": "https://daumcommercial.com/property/example/",
        "transactionMode": "sale",
        "photos": ["https://example.test/first.jpg", "https://example.test/second.jpg"],
        "contactsDetailed": [{"name": "First"}, {"name": "Second"}],
    }
    known = to_row(listing, {}, "2026-11-04T00:00:00Z")
    assert known["images"][0]["isPrimary"] is True
    assert known["contacts"][0]["isPrimary"] is True
    unknown = to_row(
        {
            **listing,
            "daumUnknownTenure": True,
            "daumTenureEvidence": {
                "inventory": "unknown",
                "detail": "unknown",
                "resolved": "unknown",
            },
        },
        {},
        "2026-11-04T00:00:00Z",
    )
    assert all(
        "isPrimary" not in row for row in unknown["images"] + unknown["contacts"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_pass",
        "missing_marker",
        "changed_evidence",
        "lease_carrier",
        "sale_price",
        "sale_price_text",
        "lease_rate",
        "title",
        "photo",
        "contact",
        "broker_ids",
        "unsupported_field",
        "proof_cross_tx",
        "retained_missing",
        "retained_mismatch",
        "fake_known_preservation",
        "count_metric",
        "missing_retained_detail",
        "missing_preservation",
        "missing_detail_preservation",
        "boolean_count",
    ],
)
def test_unknown_admission_rejects_unbound_or_duplicate_rows(
    tmp_path, monkeypatch, fault
):
    import copy

    import test_essex_current_inventory as helpers
    from test_daum_current_inventory import produce
    from test_daum_verified_subset import admit

    import cre_current_inventory_admission as admission
    import cre_daum_current_inventory as daum

    generated = produce(
        tmp_path,
        total=101,
        all_sale=True,
        unknown_inventory=True,
        unknown_detail=True,
        subset_policy="tenure-and-empty-title-v2",
    )
    monkeypatch.setattr(helpers, "SOURCE", daum.SOURCE)
    prepared = helpers.setup(tmp_path, generated)
    artifact = prepared[2]
    prepared[3]["config"]["current_inventory_admission"] = (
        admission.subset_configuration("tenure-and-empty-title-v2")
    )
    unknown = next(row for row in artifact["listings"] if row.get("daumUnknownTenure"))
    if fault == "missing_marker":
        unknown.pop("daumUnknownTenure")
    elif fault == "changed_evidence":
        unknown["daumUnknownEvidence"] = "{}"
    elif fault == "lease_carrier":
        unknown["transactionMode"] = "lease"
    elif fault == "sale_price":
        unknown["salePriceUsd"] = 1234567
    elif fault == "sale_price_text":
        unknown["salePriceText"] = "$1234567"
    elif fault == "lease_rate":
        unknown["leaseRateText"] = "$99/SF"
    elif fault == "unsupported_field":
        unknown["availableSf"] = 99999
    elif fault == "title":
        unknown["name"] = "Unsupported changed title"
    elif fault == "photo":
        unknown["photos"] = ["https://example.test/unobserved.jpg"]
    elif fault == "contact":
        unknown["contactsDetailed"] = [{"name": "Unobserved contact"}]
    elif fault == "broker_ids":
        unknown["brokerIds"] = [0]
    elif fault == "proof_cross_tx":
        artifact["sources"][1]["currentInventoryProof"]["retainedUnknownTenure"] = []
    elif fault == "retained_missing":
        for entry in artifact["sources"]:
            entry["currentInventoryProof"]["retainedUnknownTenure"] = []
    elif fault == "retained_mismatch":
        for entry in artifact["sources"]:
            entry["currentInventoryProof"]["retainedUnknownTenure"].append("59999")
    elif fault == "fake_known_preservation":
        known = next(
            row for row in artifact["listings"] if not row.get("daumUnknownTenure")
        )
        known["preserveChildCollections"] = True
    elif fault == "count_metric":
        artifact["sources"][0]["freshness"]["childPreservationRows"] += 1
    elif fault == "boolean_count":
        artifact["sources"][0]["freshness"]["childPreservationRows"] = True
    elif fault == "missing_preservation":
        unknown.pop("preserveChildCollections")
    elif fault == "missing_detail_preservation":
        unknown.pop("detailObservedWithChildPreservation")
    elif fault == "missing_retained_detail":
        for entry in artifact["sources"]:
            entry["currentInventoryProof"]["heldDetailObservations"].pop()
    else:
        duplicate = copy.deepcopy(unknown)
        duplicate["transactionMode"] = "lease"
        artifact["listings"].append(duplicate)
    artifact["totalListings"] = len(artifact["listings"])
    for entry in artifact["sources"]:
        entry["listingsCollected"] = sum(
            row["transactionMode"] == entry["transaction"]
            for row in artifact["listings"]
        )
        entry["totalAvailableOnSource"] = entry["listingsCollected"]
    prepared[4]["per_source"][daum.SOURCE]["current_active"] = len(artifact["listings"])
    helpers.refresh(prepared[1], artifact, prepared[3])
    validated = worker_validate(prepared[1], generated[2], generated[3], tmp_path)
    assert validated["ok"] is False and validated["artifact_rejected"] is True
    if fault not in {"count_metric", "boolean_count"}:
        with pytest.raises((ValueError, KeyError, TypeError)):
            admit(prepared)


def test_daum_latest_freshness_uses_explicit_source_and_marker():
    from cre_validate import persisted_freshness_provenance

    old = {
        "inventoryObservedAt": "2026-11-01T00:00:00Z",
        "detailObservedAt": "2026-11-01T00:00:01Z",
        "freshnessProvenance": {"generationId": "old"},
    }
    new = {
        "sourceKey": "daum-commercial",
        "daumUnknownTenure": True,
        "inventoryObservedAt": "2026-11-04T00:00:00Z",
        "detailObservedAt": "2026-11-04T00:00:01Z",
        "freshnessProvenance": {
            "generationId": "new",
            "detailScope": "detail_page",
            "cacheDisposition": "live",
        },
    }
    raw = {**old, "latestDaumUnknownTenureObservation": new}
    assert (
        persisted_freshness_provenance(raw, source_key="daum-commercial")[
            "generation_id"
        ]
        == "new"
    )
    assert (
        persisted_freshness_provenance(raw, source_key="other")["generation_id"]
        == "old"
    )
    assert persisted_freshness_provenance(raw)["generation_id"] == "old"
    assert (
        persisted_freshness_provenance(
            {
                **raw,
                "latestDaumUnknownTenureObservation": {
                    **new,
                    "daumUnknownTenure": False,
                },
            },
            source_key="daum-commercial",
        )["generation_id"]
        == "old"
    )
    assert (
        persisted_freshness_provenance(
            {
                **raw,
                "latestDaumUnknownTenureObservation": {**new, "sourceKey": "other"},
            },
            source_key="daum-commercial",
        )["generation_id"]
        == "old"
    )
    assert (
        persisted_freshness_provenance(
            {
                **raw,
                "latestDaumUnknownTenureObservation": {
                    "sourceKey": "daum-commercial",
                    "daumUnknownTenure": True,
                    "inventoryObservedAt": "",
                    "freshnessProvenance": {"generationId": ""},
                },
            },
            source_key="daum-commercial",
        )["generation_id"]
        == "old"
    )


@pytest.mark.parametrize("policy", ["tenure-only-v1", "tenure-and-empty-title-v2"])
def test_historical_subset_without_retained_list_passes_worker(tmp_path, policy):
    import json

    from test_daum_current_inventory import produce

    generated = produce(
        tmp_path,
        total=102,
        all_sale=True,
        unknown_inventory=True,
        unknown_detail=True,
        subset_policy=policy,
    )
    artifact = generated[0]
    retained = set(
        artifact["sources"][0]["currentInventoryProof"]["retainedUnknownTenure"]
    )
    artifact["listings"] = [
        row for row in artifact["listings"] if row["daumPostId"] not in retained
    ]
    artifact["totalListings"] = len(artifact["listings"])
    for entry in artifact["sources"]:
        entry["currentInventoryProof"].pop("retainedUnknownTenure")
        count = sum(
            row["transactionMode"] == entry["transaction"]
            for row in artifact["listings"]
        )
        entry["listingsCollected"] = entry["totalAvailableOnSource"] = count
        for key in ("listings", "inventoryObserved", "detailObserved"):
            entry["freshness"][key] = count
        entry["freshness"]["childPreservationRows"] = 0
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(artifact))
    result = worker_validate(path, generated[2], generated[3], tmp_path)
    assert result["ok"] is True, result
