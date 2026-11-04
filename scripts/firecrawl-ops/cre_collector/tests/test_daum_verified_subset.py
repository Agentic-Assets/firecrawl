"""DAUM subset is a distinct, exhaustive proof, never full-source authority."""

import copy
import pytest
import cre_daum_current_inventory as daum
from cre_ingest import strict_full_source_scope
from test_daum_current_inventory import produce
import test_essex_current_inventory as helpers


@pytest.fixture(scope="module")
def complete(tmp_path_factory):
    return produce(
        tmp_path_factory.mktemp("subset-primary-shape"), total=102, all_sale=True
    )


@pytest.fixture
def subset(tmp_path, complete, monkeypatch):
    monkeypatch.setattr(helpers, "SOURCE", daum.SOURCE)
    run, path, artifact, manifest, report = helpers.setup(tmp_path, complete)
    for entry in artifact["sources"]:
        proof = entry["currentInventoryProof"]
        proof.update(
            schema="daum-verified-subset-v1", kind="verified_subset_with_holds"
        )
        held = proof["cohort"][-2:]
        ids = {row[0] for row in held}
        for row in held:
            row[3:] = ["unknown", "", "unknown", "", "unknown"]
        for snapshot in proof["passes"]:
            for page in snapshot["pages"]:
                for member in page["members"]:
                    if member[0] in {row[1] for row in held}:
                        member[1:] = ["unknown", ""]
        proof["held"] = [
            [row[0], "unknown_inventory_and_detail_tenure"] for row in held
        ]
        entry["truncated"] = True
    artifact["listings"] = [
        row for row in artifact["listings"] if row["daumPostId"] not in ids
    ]
    artifact["totalListings"] = len(artifact["listings"])
    for entry in artifact["sources"]:
        entry["listingsCollected"] = sum(
            row["transactionMode"] == entry["transaction"]
            for row in artifact["listings"]
        )
        entry["totalAvailableOnSource"] = entry["listingsCollected"]
    manifest["config"]["current_inventory_admission"]["admission_scope"] = (
        "daum_verified_subset_v1"
    )
    report["per_source"][daum.SOURCE]["current_active"] = len(artifact["listings"])
    report["per_source"][daum.SOURCE]["reason"] += "; a source pass also errored this run"
    helpers.refresh(path, artifact, manifest)
    return run, path, artifact, manifest, report


def test_subset_structural_scope_never_changes_complete_contract(subset):
    _, _, artifact, _, _ = subset
    assert len(daum.subset_source_scope(artifact)["listings"]) == 100
    with pytest.raises(ValueError, match="truncated"):
        strict_full_source_scope(artifact, daum.SOURCE)


@pytest.mark.parametrize(
    "mutation",
    [
        "pass_error",
        "wrong_count",
        "truncated_false",
        "foreign_source",
        "limited",
        "missing_pass",
    ],
)
def test_subset_scope_is_explicit_and_exact(subset, mutation):
    artifact = subset[2]
    if mutation == "pass_error":
        artifact["sources"][0]["error"] = "provider failed"
    if mutation == "wrong_count":
        artifact["totalListings"] += 1
    if mutation == "truncated_false":
        artifact["sources"][0]["truncated"] = False
    if mutation == "foreign_source":
        artifact["listings"][0]["sourceKey"] = "jll"
    if mutation == "limited":
        artifact["runMeta"]["maxItemsPerSource"] = 100
    if mutation == "missing_pass":
        artifact["sources"].pop()
    with pytest.raises(ValueError):
        daum.subset_source_scope(artifact)


def admit(prepared, phase="source"):
    from cre_checkpoint_refresh import git_identity

    return daum.admit_subset(
        prepared[1],
        prepared[3],
        prepared[4],
        current_code_sha=git_identity()[0],
        current_git_dirty=False,
        phase=phase,
    )


def test_subset_truncation_remains_unsafe_and_actual_gate_reason_is_bound(subset):
    from cre_gate import count_artifacts, verdict_for

    counts, errors, _, _, _ = count_artifacts([str(subset[1])], True)
    verdict, reason, safe, _ = verdict_for(
        counts[daum.SOURCE], bool(errors[daum.SOURCE]), errors[daum.SOURCE], None, 100, 0.2
    )
    assert verdict == "first_seen" and safe is False
    assert reason == subset[4]["per_source"][daum.SOURCE]["reason"]
    admit(subset)
    assert subset[4]["per_source"][daum.SOURCE]["raw_reason"] == reason
    assert subset[4]["per_source"][daum.SOURCE]["mark_missing_safe"] is False
    assert subset[4]["scope"]["whole_source_coverage"] is False
    subset[3]["sources"][daum.SOURCE]["gate"] = copy.deepcopy(subset[4]["per_source"][daum.SOURCE])
    admit(subset, phase="aggregate")


def test_emitted_union_and_unknown_lease_coverage_through_both_gates(subset):
    binding = admit(subset)
    assert {
        key: binding[key]
        for key in [
            "inventory_count",
            "emitted_count",
            "held_count",
            "sale_count",
            "lease_count",
        ]
    } == {
        "inventory_count": 102,
        "emitted_count": 100,
        "held_count": 2,
        "sale_count": 100,
        "lease_count": 0,
    }
    assert binding["whole_lease_coverage"] == "unknown"
    assert binding["scope"] == daum.SUBSET_SCOPE
    assert subset[4]["per_source"][daum.SOURCE]["verdict"] == daum.SUBSET_VERDICT
    subset[3]["sources"][daum.SOURCE]["gate"] = copy.deepcopy(
        subset[4]["per_source"][daum.SOURCE]
    )
    assert admit(subset, "aggregate") == binding


@pytest.mark.parametrize(
    "case",
    [
        "missing_hold",
        "wrong_reason",
        "held_known",
        "duplicate_native",
        "duplicate_url",
        "detail_missing",
        "detail_stale",
        "detail_hash",
        "invented_listing",
        "subfloor",
        "scope_borrow",
        "baseline",
        "pass_drift",
        "detail_conflict",
        "wrong_target",
        "wrong_generation",
        "wrong_code",
    ],
)
def test_exhaustive_partition_and_existing_bindings_fail_closed(subset, case):
    _, path, artifact, manifest, report = subset
    if case == "scope_borrow":
        manifest["config"]["current_inventory_admission"].pop("admission_scope")
    if case == "baseline":
        report["per_source"][daum.SOURCE]["baseline_median"] = 100
    if case == "wrong_target":
        manifest["preflight"]["database_target"]["value"] = "bad"
    if case == "wrong_code":
        manifest["collector_git_sha"] = "0" * 40
    for entry in artifact["sources"]:
        proof = entry["currentInventoryProof"]
        if case == "missing_hold":
            proof["held"].pop()
        if case == "wrong_reason":
            proof["held"][0][1] = "transport_failure"
        if case == "held_known":
            proof["held"][0][0] = proof["cohort"][0][0]
        if case == "duplicate_native":
            proof["cohort"][-1][0] = proof["cohort"][0][0]
        if case == "duplicate_url":
            proof["cohort"][-1][1:3] = proof["cohort"][0][1:3]
        if case == "detail_missing":
            proof["details"].pop()
        if case == "detail_stale":
            proof["details"][-1][2] = "2000-01-01T00:00:00Z"
        if case == "detail_hash":
            proof["details"][-1][3] = "bad"
        if case == "detail_conflict":
            proof["cohort"][0][5:] = ["lease", "Lease", "lease"]
        if case == "pass_drift":
            proof["passes"][0]["pages"][0]["members"][0][1:] = ["lease", "Lease"]
        if case == "wrong_generation":
            proof["generationId"] = "foreign"
        if case == "subfloor":
            row = proof["cohort"][-3]
            row[3:] = ["unknown", "", "unknown", "", "unknown"]
            proof["held"].insert(0, [row[0], "unknown_inventory_and_detail_tenure"])
            for snapshot in proof["passes"]:
                for page in snapshot["pages"]:
                    for member in page["members"]:
                        if member[0] == row[1]:
                            member[1:] = ["unknown", ""]
    if case == "invented_listing":
        artifact["listings"][0]["daumPostId"] = artifact["sources"][0][
            "currentInventoryProof"
        ]["held"][0][0]
    if case == "subfloor":
        # 99 emitted identities plus a dual-tenure second transaction is still
        # below the unique floor even though the raw gate counts 100 flat rows.
        for entry in artifact["sources"]:
            proof = entry["currentInventoryProof"]
            member = proof["cohort"][0]
            member[3:] = [
                "sale_or_lease",
                "Sale or Lease",
                "sale_or_lease",
                "Sale or Lease",
                "sale_or_lease",
            ]
            for snapshot in proof["passes"]:
                for page in snapshot["pages"]:
                    for row in page["members"]:
                        if row[0] == member[1]:
                            row[1:] = ["sale_or_lease", "Sale or Lease"]
        listing = next(
            row for row in artifact["listings"] if row["daumPostId"] == member[0]
        )
        listing["transactionType"] = "Sale/Lease"
        listing["daumTenureEvidence"].update(
            inventory="sale_or_lease",
            inventoryText="Sale or Lease",
            detail="sale_or_lease",
            detailText="Sale or Lease",
            resolved="sale_or_lease",
        )
        artifact["listings"].append(
            {**copy.deepcopy(listing), "transactionMode": "lease"}
        )
        removed = artifact["sources"][0]["currentInventoryProof"]["cohort"][-3][0]
        artifact["listings"] = [
            row for row in artifact["listings"] if row["daumPostId"] != removed
        ]
        artifact["totalListings"] = len(artifact["listings"])
        for entry in artifact["sources"]:
            entry["listingsCollected"] = sum(
                row["transactionMode"] == entry["transaction"]
                for row in artifact["listings"]
            )
            entry["totalAvailableOnSource"] = entry["listingsCollected"]
        report["per_source"][daum.SOURCE]["current_active"] = len(artifact["listings"])
        assert len(artifact["listings"]) == 100
    helpers.refresh(path, artifact, manifest)
    with pytest.raises((ValueError, KeyError, TypeError)):
        admit(subset)


def envelope(prepared):
    binding = admit(prepared)
    manifest, source = prepared[3], copy.deepcopy(prepared[4])
    manifest["sources"][daum.SOURCE]["gate"] = copy.deepcopy(
        source["per_source"][daum.SOURCE]
    )
    assert admit(prepared, "aggregate") == binding
    return {
        "manifest": manifest,
        "source_report": source,
        "aggregate_report": copy.deepcopy(prepared[4]),
        "binding": binding,
        "attempt_id": "e" * 32,
    }


def context(prepared, claims):
    from cre_checkpoint_refresh import git_identity

    return daum.ingest_subset_context(
        prepared[1],
        claims,
        current_code_sha=git_identity()[0],
        current_git_dirty=False,
        expected_database_target_sha256="b" * 64,
        attempt_id="e" * 32,
    )


def test_final_ingest_context_includes_held_and_exact_emitted_staging(subset):
    from cre_ingest import build_sql, to_row, merge_rows

    claims = context(subset, envelope(subset))
    assert len(claims["members"]) == 102
    assert sum(row["emitted"] for row in claims["members"]) == 100
    staged = {}
    for listing in subset[2]["listings"]:
        row = to_row(listing, {}, subset[2]["runMeta"]["finishedAt"])
        key = row["external_id"]
        staged[key] = merge_rows(staged[key], row) if key in staged else row
    sql = build_sql(
        list(staged.values()),
        [],
        subset[3]["started_at"],
        set(),
        daum_verified_subset_claims=claims,
    )
    assert sql.index("DAUM verified subset") < sql.index(
        "INSERT INTO credeals.cre_listings AS t"
    )
    assert "si.observation_present IS DISTINCT FROM true" in sql
    assert "t.deleted_at IS NOT NULL" in sql
    assert "_daum_historical_identity_claims" in sql
    assert "UPDATE credeals.cre_source_baseline" not in sql


@pytest.mark.parametrize(
    "case",
    [
        "attempt",
        "binding",
        "source_gate",
        "aggregate_gate",
        "gen",
        "target",
        "artifact",
    ],
)
def test_ingest_rechecks_both_gates_and_attempt(subset, case):
    claims = envelope(subset)
    if case == "attempt":
        claims["attempt_id"] = "f" * 32
    if case == "binding":
        claims["binding"] = {**claims["binding"], "held_count": 0}
    if case in {"source_gate", "aggregate_gate"}:
        claims["source_report" if case == "source_gate" else "aggregate_report"][
            "per_source"
        ][daum.SOURCE]["current_inventory_proof"] = {}
    if case == "gen":
        claims["manifest"]["run_id"] = "other"
    if case == "target":
        claims["manifest"]["preflight"]["database_target"]["value"] = "c" * 64
    if case == "artifact":
        subset[1].write_bytes(subset[1].read_bytes() + b" ")
    with pytest.raises((ValueError, KeyError, TypeError)):
        context(subset, claims)


def test_subset_zero_holds_keeps_distinct_schema(tmp_path, complete, monkeypatch):
    monkeypatch.setattr(helpers, "SOURCE", daum.SOURCE)
    prepared = helpers.setup(tmp_path, complete)
    for entry in prepared[2]["sources"]:
        entry["currentInventoryProof"].update(
            schema=daum.SUBSET_SCHEMA, kind="verified_subset_with_holds", held=[]
        )
    prepared[3]["config"]["current_inventory_admission"]["admission_scope"] = (
        daum.SUBSET_SCOPE
    )
    helpers.refresh(prepared[1], prepared[2], prepared[3])
    result = admit(prepared)
    assert result["held_count"] == 0 and result["emitted_count"] == 102
    assert (
        result["whole_source_coverage"] is False
        and result["schema"] == daum.SUBSET_SCHEMA
    )
    with pytest.raises(ValueError):
        daum.admit(
            prepared[1],
            prepared[3],
            prepared[4],
            current_code_sha=prepared[3]["collector_git_sha"],
            current_git_dirty=False,
            phase="source",
        )


def test_real_ingest_cli_builds_subset_sql_only_after_both_gates(
    subset, tmp_path, monkeypatch
):
    import json
    import sys
    import cre_checkpoint_refresh as cp
    import cre_ingest as ingest

    claims = envelope(subset)
    claim_path = tmp_path / "claims.json"
    claim_path.write_text(json.dumps(claims))
    claim_path.chmod(0o600)
    monkeypatch.setattr(
        cp, "git_identity", lambda: (subset[3]["collector_git_sha"], False)
    )
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *args: pytest.fail("dryrun reached database")
    )
    monkeypatch.delenv("CRE_ACTIVATE_STATUS", raising=False)
    out = tmp_path / "sql"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(subset[1]),
            "--dry-run",
            "--require-strict-freshness",
            "--expected-db-target-sha256",
            "b" * 64,
            "--checkpoint-ingest-attempt",
            "e" * 32,
            "--daum-verified-subset-claims",
            str(claim_path),
            "--keep-artifacts",
            str(out),
        ],
    )
    ingest.main()
    assert "DAUM verified subset" in (out / "ingest.sql").read_text()


def test_direct_ingest_without_subset_claims_refuses_before_database(
    subset, monkeypatch
):
    import sys
    import cre_ingest as ingest

    monkeypatch.setattr(
        ingest,
        "load_db_url",
        lambda *args: pytest.fail("unbound ingest reached database"),
    )
    monkeypatch.setattr(
        sys, "argv", ["cre_ingest.py", "--in", str(subset[1]), "--dry-run"]
    )
    with pytest.raises(SystemExit, match="explicit guarded claims"):
        ingest.main()


def test_subset_counts_do_not_accept_boolean_zero(subset):
    subset[2]["sources"][1]["totalAvailableOnSource"] = False
    with pytest.raises(ValueError):
        daum.subset_source_scope(subset[2])
