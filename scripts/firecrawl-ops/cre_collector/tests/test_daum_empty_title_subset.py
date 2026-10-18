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
    assert "--daum-subset-policy" not in old
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
