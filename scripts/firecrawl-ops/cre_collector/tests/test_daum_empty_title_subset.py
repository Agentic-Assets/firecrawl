"""The empty-title policy is distinct and never implicit in V1/default scope."""

import pytest
import cre_current_inventory_admission as admission


def test_v2_configuration_is_explicit_and_leaves_v1_bytes_unchanged():
    v1 = admission.subset_configuration()
    assert "subset_policy" not in v1
    assert v1["admission_scope"] == "daum_verified_subset_v1"
    v2 = admission.subset_configuration("tenure-and-empty-title-v2")
    assert v2 == {
        **admission.configuration("daum-commercial"),
        "admission_scope": "daum_verified_subset_v2",
        "subset_policy": "tenure-and-empty-title-v2",
    }
    with pytest.raises(ValueError):
        admission.validate_options(
            [],
            ["daum-commercial"],
            ("sale", "lease"),
            daum_subset_policy="tenure-and-empty-title-v2",
        )
    admission.validate_options(
        [],
        ["daum-commercial"],
        ("sale", "lease"),
        daum_verified_subset=True,
        daum_subset_policy="tenure-and-empty-title-v2",
    )


def test_invocation_default_and_resume_preserve_recorded_policy(tmp_path):
    import json

    assert admission.invocation_subset_policy(None, False) == "tenure-only-v1"
    assert admission.invocation_subset_policy(None, True) == "tenure-and-empty-title-v2"
    assert admission.invocation_subset_policy("tenure-only-v1", True) == "tenure-only-v1"
    manifest = tmp_path / "manifest.json"
    for policy in ("tenure-only-v1", "tenure-and-empty-title-v2"):
        manifest.write_text(json.dumps({"config": {
            "sources": ["daum-commercial"], "transactions": ["sale", "lease"],
            "current_inventory_admission": admission.subset_configuration(policy),
        }}))
        assert admission.invocation_subset_policy(None, True, tmp_path) == policy
        assert admission.invocation_subset_policy(None, True, manifest) == policy
        manifest.write_text(json.dumps({"config": {
            "pipeline": True, "sources": ["daum-commercial", "cbre"], "transactions": ["sale", "lease"],
            "current_inventory_admissions": admission.configuration_map(
                [], ["daum-commercial", "cbre"], daum_verified_subset=True, daum_subset_policy=policy),
        }}))
        assert admission.invocation_subset_policy(None, True, tmp_path) == policy
    manifest.write_text('{"config": {}}')
    with pytest.raises(ValueError):
        admission.invocation_subset_policy(None, True, tmp_path)


@pytest.fixture(scope="module")
def actual_v2(tmp_path_factory):
    from test_daum_current_inventory import produce

    return produce(
        tmp_path_factory.mktemp("v2-empty-title-producer"),
        total=101,
        empty_title=True,
        subset_policy="tenure-and-empty-title-v2",
    )


@pytest.fixture
def v2(tmp_path, actual_v2, monkeypatch):
    import test_essex_current_inventory as helpers

    monkeypatch.setattr(helpers, "SOURCE", "daum-commercial")
    prepared = helpers.setup(tmp_path, actual_v2)
    prepared[3]["config"]["current_inventory_admission"] = (
        admission.subset_configuration("tenure-and-empty-title-v2")
    )
    return prepared


def test_actual_v2_producer_through_both_gates_and_unique_staging(v2):
    import cre_daum_current_inventory as daum
    from test_daum_verified_subset import admit

    binding = admit(v2)
    assert binding["scope"] == "daum_verified_subset_v2"
    assert binding["subset_policy"] == "tenure-and-empty-title-v2"
    assert binding["inventory_count"] == 101
    assert binding["held_count"] == 1
    assert binding["emitted_count"] == binding["staged_count"] == 100
    assert binding["whole_lease_coverage"] == "unknown"
    proof = v2[2]["sources"][0]["currentInventoryProof"]
    assert proof["titleObservations"] == [["50100", 1, "empty"]]
    assert (
        proof["cohort"][
            next(i for i, row in enumerate(proof["cohort"]) if row[0] == "50100")
        ][7]
        == "lease"
    )
    assert all(row["daumPostId"] != 50100 for row in v2[2]["listings"])
    v2[3]["sources"][daum.SOURCE]["gate"] = {"current_inventory_proof": binding}
    assert admit(v2, phase="aggregate") == binding


def test_corroborated_empty_visible_title_is_bound_to_card_and_detail(tmp_path, monkeypatch):
    from test_daum_current_inventory import produce
    import test_essex_current_inventory as helpers
    from test_daum_verified_subset import admit

    generated = produce(tmp_path, total=101, empty_title=True, corroborated_title=True, with_spaces=True,
                        subset_policy="tenure-and-empty-title-v2")
    monkeypatch.setattr(helpers, "SOURCE", "daum-commercial")
    prepared = helpers.setup(tmp_path, generated)
    prepared[3]["config"]["current_inventory_admission"] = admission.subset_configuration("tenure-and-empty-title-v2")
    artifact = prepared[2]
    proof = artifact["sources"][0]["currentInventoryProof"]
    assert proof["titleFallbackObservations"] == [{"postId": "50100", "cardTitle": "Synthetic 100",
                                                   "documentTitle": "Synthetic 100 – Daum Commercial"}]
    listing = next(item for item in artifact["listings"] if item["daumPostId"] == "50100")
    assert listing["name"] == "Synthetic 100" and listing["daumSearchCard"]["title"] == listing["name"]
    assert all(not listing.get(key) for key in ("city", "state", "postalCode"))
    assert listing["daumTitleFallbackDetail"]["city"] == "Phoenix"
    assert listing["daumTitleFallbackDetail"]["state"] == "AZ"
    assert listing["daumTitleFallbackDetail"]["availableSpaceObservations"] == listing["daumAvailableSpaceObservations"]
    assert "$6,600 per month" in listing["daumAvailableSpaceObservations"][0]["text"]
    from cre_daum_current_inventory import _sha
    from cre_daum_fallback_partition import staged_fallback_members
    members = staged_fallback_members(artifact)
    manifest = prepared[3]
    manifest["sources"]["daum-commercial"]["daum_fallback_probe"] = {
        "artifact_sha256": manifest["sources"]["daum-commercial"]["artifact"]["sha256"],
        "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
        "database_target_sha256": manifest["preflight"]["database_target"]["value"],
        "members_sha256": _sha(members),
        "partition": [{**{k: member[k] for k in ("native_id", "external_id", "source_url")},
                       "hold_reason": None, "preimage": "f" * 64} for member in members],
    }
    import copy
    held_prepared = copy.deepcopy(prepared)
    held_prepared[3]["sources"]["daum-commercial"]["daum_fallback_probe"]["partition"][0]["hold_reason"] = "identity_conflict"
    held_binding = admit(held_prepared)
    assert held_binding["source_emitted_count"] == 101
    assert held_binding["emitted_count"] == 100 and held_binding["held_count"] == 1
    assert held_binding["target_held"] == [{"native_id": "50100", "external_id": listing["id"], "reason": "identity_conflict"}]
    assert held_binding["whole_lease_coverage"] == "unknown"
    binding = admit(prepared)
    assert binding["emitted_count"] == 101 and binding["held_count"] == 0
    from cre_daum_fallback_partition import staged_fallback_members
    assert staged_fallback_members(artifact) == [{
        "native_id": "50100", "external_id": listing["id"], "source_url": listing["url"],
        "inventory_at": listing["inventoryObservedAt"], "detail_at": listing["detailObservedAt"],
    }]
    from cre_daum_current_inventory import checkpoint_retained_unknown_ids
    assert checkpoint_retained_unknown_ids(artifact) == frozenset()
    import cre_checkpoint_refresh as checkpoint
    stats = checkpoint._validate_source_artifact_local(
        prepared[1], "daum-commercial", generated[2], expected_generation_id=generated[3],
        expected_generation_started_at=generated[2], expected_transactions=("sale", "lease"),
        daum_verified_subset=True,
    )
    assert stats["staged_unique"] == 101
    from test_daum_verified_subset import context, envelope
    from cre_ingest import build_sql, merge_rows, to_row
    claims = context(prepared, envelope(prepared))
    assert claims["fallbackMembers"] == members
    staged = {}
    for source_listing in artifact["listings"]:
        row = to_row(source_listing, {}, artifact["runMeta"]["finishedAt"])
        key = row["external_id"]
        staged[key] = merge_rows(staged[key], row) if key in staged else row
    generated_sql = build_sql(list(staged.values()), [], manifest["started_at"], set(),
                              daum_verified_subset_claims=claims)
    assert generated_sql.index("DAUM fallback partition changed") < generated_sql.index("DAUM identity contradiction")
    assert "DAUM fallback evidence targets changed" in generated_sql


@pytest.mark.parametrize("fault", ["page_title", "card_title", "listing_title", "unexpected_city", "missing_witness", "duplicate_witness", "foreign_post", "missing_detail", "detail_post"])
def test_corroborated_title_proof_rejects_drift(tmp_path, monkeypatch, fault):
    import copy
    from test_daum_current_inventory import produce
    import test_essex_current_inventory as helpers
    from test_daum_verified_subset import admit

    generated = produce(tmp_path, total=101, empty_title=True, corroborated_title=True,
                        subset_policy="tenure-and-empty-title-v2")
    monkeypatch.setattr(helpers, "SOURCE", "daum-commercial")
    prepared = helpers.setup(tmp_path, generated)
    prepared[3]["config"]["current_inventory_admission"] = admission.subset_configuration("tenure-and-empty-title-v2")
    artifact = prepared[2]
    for source in artifact["sources"]:
        rows = source["currentInventoryProof"]["titleFallbackObservations"]
        if fault == "page_title":
            rows[0]["documentTitle"] = "Different – Daum Commercial"
        elif fault == "missing_witness":
            rows.clear()
        elif fault == "duplicate_witness":
            rows.append(copy.deepcopy(rows[0]))
        elif fault == "foreign_post":
            rows[0]["postId"] = "59999"
    listing = next(item for item in artifact["listings"] if item["daumPostId"] == "50100")
    if fault == "card_title":
        listing["daumSearchCard"]["title"] = "Different"
    elif fault == "listing_title":
        listing["name"] = "Different"
    elif fault == "unexpected_city":
        listing["city"] = "AL"
    elif fault == "missing_detail":
        listing.pop("daumTitleFallbackDetail")
    elif fault == "detail_post":
        listing["daumTitleFallbackDetail"]["postId"] = 59999
    helpers.refresh(prepared[1], artifact, prepared[3])
    with pytest.raises(ValueError):
        admit(prepared)


def test_wip_fallback_cannot_use_unguarded_generic_ingest():
    from cre_ingest import build_sql

    row = {"raw_data": {"daumTitleFallback": {"cardTitle": "Synthetic 100"}}}
    with pytest.raises(ValueError, match="requires guarded partial persistence"):
        build_sql([row], [], "2026-11-05T11:00:00Z", [])


@pytest.mark.parametrize(
    "fault",
    [
        "count-bool",
        "count-zero",
        "state",
        "unknown-post",
        "duplicate",
        "missing-observation",
        "observed-emitted",
        "wrong-policy",
        "lease-proof-drift",
    ],
)
def test_v2_title_observation_partition_tamper_refuses(v2, fault):
    import copy
    import test_essex_current_inventory as helpers
    from test_daum_verified_subset import admit

    for entry in v2[2]["sources"]:
        proof = entry["currentInventoryProof"]
        if fault == "count-bool":
            proof["titleObservations"][0][1] = True
        if fault == "count-zero":
            proof["titleObservations"][0][1] = 0
        if fault == "state":
            proof["titleObservations"][0][2] = "nonempty"
        if fault == "unknown-post":
            proof["titleObservations"][0][0] = "59999"
        if fault == "duplicate":
            proof["titleObservations"].append(
                copy.deepcopy(proof["titleObservations"][0])
            )
        if fault == "missing-observation":
            proof["titleObservations"] = []
        if fault == "observed-emitted":
            proof["titleObservations"][0][0] = "50000"
        if fault == "wrong-policy":
            proof["subsetPolicy"] = "tenure-only-v1"
        if fault == "lease-proof-drift" and entry["transaction"] == "lease":
            proof["titleObservations"] = []
    helpers.refresh(v2[1], v2[2], v2[3])
    with pytest.raises(ValueError):
        admit(v2)


def test_v2_orchestration_binds_policy_and_clears_ambient_scope(tmp_path):
    import cre_checkpoint_refresh as refresh

    m = refresh.new_manifest(
        tmp_path,
        git_sha="a" * 40,
        git_dirty=False,
        sources=["daum-commercial"],
        page_cap=400,
        concurrency=1,
        admit_daum_verified_subset=True,
        daum_subset_policy="tenure-and-empty-title-v2",
    )
    assert admission.subset_policy(m["config"]) == "tenure-and-empty-title-v2"
    env, _ = refresh.fresh_source_env(
        "daum-commercial",
        tmp_path,
        daum_verified_subset=True,
        daum_subset_policy="tenure-and-empty-title-v2",
    )
    assert env["DAUM_SUBSET_POLICY"] == "tenure-and-empty-title-v2"
    env, _ = refresh.fresh_source_env(
        "daum-commercial",
        tmp_path,
        base={"DAUM_SUBSET_POLICY": "tenure-and-empty-title-v2"},
    )
    assert "DAUM_SUBSET_POLICY" not in env


def test_v2_partial_summary_is_not_a_complete_source_result(tmp_path, monkeypatch):
    import test_daum_subset_reporting as fixture
    import test_notification_summary as summary
    import cre_checkpoint_series as series

    evidence = fixture.evidence.__wrapped__(tmp_path, monkeypatch)
    parent, child = evidence["values"]["parent"], evidence["values"]["child"]
    parent["config"]["current_inventory_admission"] = admission.subset_configuration(
        "tenure-and-empty-title-v2"
    )
    child["config"] = series._expected_child_config("daum-commercial", parent["config"])
    gate = child["sources"]["daum-commercial"]["gate"]
    gate["admission_scope"] = "daum_verified_subset_v2"
    gate["current_inventory_proof"].update(
        schema="daum-verified-subset-v2",
        scope="daum_verified_subset_v2",
        subset_policy="tenure-and-empty-title-v2",
        title_observations_sha256="a" * 64,
    )
    evidence["save"]()
    result = summary.read(evidence)
    assert result["partial"] is True
    assert result["whole_source_coverage"] is False
    assert result["whole_lease_coverage"] == "unknown"


def test_target_held_fallback_summary_is_bound_and_keeps_verified_peers(tmp_path, monkeypatch):
    import test_daum_subset_reporting as fixture
    import test_notification_summary as summary
    import cre_checkpoint_series as series
    from cre_daum_current_inventory import _sha

    evidence = fixture.evidence.__wrapped__(tmp_path, monkeypatch)
    parent, child = evidence["values"]["parent"], evidence["values"]["child"]
    parent["config"]["current_inventory_admission"] = admission.subset_configuration("tenure-and-empty-title-v2")
    child["config"] = series._expected_child_config("daum-commercial", parent["config"])
    checkpoint = child["sources"]["daum-commercial"]
    gate = checkpoint["gate"]
    gate["admission_scope"] = "daum_verified_subset_v2"
    partition = [{"native_id": "50100", "external_id": "property/example",
                  "source_url": "https://daumcommercial.com/property/example/",
                  "hold_reason": "identity_conflict", "preimage": "f" * 64}]
    checkpoint["daum_fallback_probe"] = {
        "artifact_sha256": checkpoint["artifact"]["sha256"], "generation_id": child["run_id"],
        "producer_sha": child["collector_git_sha"],
        "database_target_sha256": child["preflight"]["database_target"]["value"],
        "members_sha256": "a" * 64, "partition": partition,
    }
    checkpoint["artifact"]["staged_unique"] = 101
    binding = gate["current_inventory_proof"]
    binding.update(schema="daum-verified-subset-v2", scope="daum_verified_subset_v2",
                   subset_policy="tenure-and-empty-title-v2", title_observations_sha256="a" * 64,
                   source_emitted_count=101, source_held_count=1, fallback_partition_sha256=_sha(partition),
                   target_held=[{"native_id": "50100", "external_id": "property/example", "reason": "identity_conflict"}],
                   sale_count=100, lease_count=0)
    evidence["save"]()
    result = summary.read(evidence)
    assert result["partial"] is True and result["emitted_count"] == 100 and result["held_count"] == 2
    assert result["transaction_breakdown"]["sale"] == 100
    import copy
    from cre_notification_summary import _artifact_expected_count, _subset_summary_fields
    small = copy.deepcopy(child)
    small_binding = small["sources"]["daum-commercial"]["gate"]["current_inventory_proof"]
    small_binding.update(inventory_count=101, source_emitted_count=100, emitted_count=99,
                         held_count=2, staged_count=99, sale_count=99)
    assert _subset_summary_fields(small, "daum-commercial", 99)["emitted_count"] == 99
    assert _artifact_expected_count(small, "daum-commercial", 99) == 100
    from cre_source_health import _qualified_subset_attempt
    assert _qualified_subset_attempt(parent["sources"]["daum-commercial"], child, child["finished_at"],
                                     computed_at=child["finished_at"], source_key="daum-commercial")
    checkpoint["daum_fallback_probe"]["partition"][0]["hold_reason"] = None
    assert not _qualified_subset_attempt(parent["sources"]["daum-commercial"], child, child["finished_at"],
                                         computed_at=child["finished_at"], source_key="daum-commercial")
    evidence["save"]()
    with pytest.raises(summary.REFUSED):
        summary.read(evidence)


def test_actual_v2_sealed_gates_reach_exact_staging_sql(v2):
    from test_daum_verified_subset import context, envelope
    from cre_ingest import build_sql, merge_rows, to_row

    claims = context(v2, envelope(v2))
    assert claims["schema"] == "daum-verified-subset-v2"
    assert claims["subset_policy"] == "tenure-and-empty-title-v2"
    assert claims["titleObservations"] == [["50100", 1, "empty"]]
    assert len(claims["members"]) == 101
    assert sum(member["emitted"] for member in claims["members"]) == 100
    staged = {}
    for listing in v2[2]["listings"]:
        row = to_row(listing, {}, v2[2]["runMeta"]["finishedAt"])
        key = row["external_id"]
        staged[key] = merge_rows(staged[key], row) if key in staged else row
    sql = build_sql(
        list(staged.values()),
        [],
        v2[3]["started_at"],
        set(),
        daum_verified_subset_claims=claims,
    )
    assert "_daum_historical_identity_claims" in sql
    assert "si.observation_present IS DISTINCT FROM true" in sql
    assert "UPDATE credeals.cre_source_baseline" not in sql


def test_v2_worker_argv_and_resume_configuration_are_policy_bound(tmp_path):
    import cre_checkpoint_refresh as refresh

    kwargs = dict(
        run_dir=tmp_path,
        attempt_log=tmp_path / "log",
        attempt_number=1,
        generation_started_at="2026-10-18T00:00:00Z",
        page_cap=400,
        concurrency=1,
        transactions=("sale", "lease"),
        daum_verified_subset=True,
    )
    argv = refresh.build_cohort_collect_worker_argv(
        "daum-commercial",
        tmp_path / "artifact.json",
        **kwargs,
        daum_subset_policy="tenure-and-empty-title-v2",
    )
    assert argv.count("--admit-daum-verified-subset") == 1
    assert argv[argv.index("--daum-subset-policy") + 1] == "tenure-and-empty-title-v2"
    old = refresh.build_cohort_collect_worker_argv(
        "daum-commercial", tmp_path / "artifact.json", **kwargs
    )
    assert old[old.index("--daum-subset-policy") + 1] == "tenure-only-v1"
    with pytest.raises(ValueError):
        admission.admission_configuration(
            {
                "sources": ["daum-commercial"],
                "transactions": ["sale", "lease"],
                "current_inventory_admission": {
                    **admission.subset_configuration(),
                    "subset_policy": "tenure-and-empty-title-v2",
                },
            },
            "daum-commercial",
        )


@pytest.mark.parametrize(
    "script", ["cre_checkpoint_series.py", "cre_checkpoint_refresh.py"]
)
def test_actual_cli_nondefault_policy_without_optin_refuses_before_dispatch(
    script, tmp_path
):
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / script),
            "--sources",
            "daum-commercial",
            "--daum-subset-policy",
            "tenure-and-empty-title-v2",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "Current-inventory admission evidence is incomplete or drifted" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_historical_v2_without_retained_unknown_field_still_admits(v2):
    import test_essex_current_inventory as helpers
    from test_daum_verified_subset import admit

    for entry in v2[2]["sources"]:
        entry["currentInventoryProof"].pop("retainedUnknownTenure")
    helpers.refresh(v2[1], v2[2], v2[3])
    binding = admit(v2)
    assert binding["held_count"] == 1
    assert "retained_unknown_tenure_count" not in binding
