"""AGENTIC-4179: explicit index-absent retirement, offline synthetic evidence."""

import hashlib
import json
from datetime import datetime, timedelta

import pytest

import cre_reconcile_listing_lifecycle as reconcile
from test_listing_lifecycle_remediation import _evidence


def bundle(tmp_path):
    value = _evidence(tmp_path)
    row = value["rows"][0]
    old = (
        datetime.fromisoformat(row["source"]["observed_at"].replace("Z", "+00:00"))
        - timedelta(days=1)
    ).isoformat()
    row.update(
        index_exists=False,
        index_soft_deleted=None,
        observation_present=None,
        presence_generation=None,
        listing_deleted=False,
        listing_status="active",
        listing_deleted_at=None,
        listing_updated_at=old,
        listing_scraped_at=old,
    )
    return value


def plan(tmp_path, value=None, enabled=True):
    return reconcile.build_plan(
        value or bundle(tmp_path),
        evidence_base_dir=tmp_path,
        allow_index_absent_retirement=enabled,
    )


def test_default_refuses_missing_index(tmp_path):
    result = plan(tmp_path, enabled=False)
    assert result["actions"] == []
    assert (
        result["skipped"][0]["reason"]
        == "index_absent_retirement_requires_explicit_mode"
    )


def test_opt_in_has_listing_only_cas_and_no_generation(tmp_path):
    result = plan(tmp_path)
    action = result["actions"][0]
    assert action["index_absent_retirement"] is True
    assert action["operations"] == ["listing_only_retirement", "status_change"]
    assert action["desired_generation"] is None
    assert action["before"]["index_exists"] is False
    assert action["event"]["event_type"] == "status_change"
    assert (
        action["before_hash"]
        == hashlib.sha256(reconcile._canon(action["before"]).encode()).hexdigest()
    )
    sql = reconcile.build_apply_sql(
        result["actions"], result["plan_hash"], "cayman", "AGENTIC-4179"
    )
    assert "SHARE ROW EXCLUSIVE MODE" in sql
    assert sql.index("SHARE ROW EXCLUSIVE MODE") < sql.index("FOR UPDATE")
    assert "UPDATE credeals.cre_source_index" not in sql
    assert "INSERT INTO credeals.cre_source_index" not in sql
    assert "disappeared" not in sql
    assert "e.detected_at=l.updated_at" in sql
    assert "presence_generation IS NULL" in sql
    assert "presence_generation=0" not in sql
    for field in ["updated_at", "scraped_at", "deleted_at", "status"]:
        assert field in sql


@pytest.mark.parametrize(
    "field,value",
    [
        ("index_exists", True),
        ("index_exists", None),
        ("index_soft_deleted", False),
        ("listing_deleted", True),
        ("listing_status", "inactive"),
        ("listing_status", "sold"),
        ("listing_deleted_at", "2020-01-01T00:00:00Z"),
        ("listing_updated_at", None),
        ("listing_updated_at", "not-time"),
        ("listing_updated_at", "2020-01-01T00:00:00"),
    ],
)
def test_opt_in_rejects_wrong_or_incomplete_before_state(tmp_path, field, value):
    data = bundle(tmp_path)
    data["rows"][0][field] = value
    assert plan(tmp_path, data)["actions"] == []


def test_opt_in_rejects_fresher_listing(tmp_path):
    data = bundle(tmp_path)
    data["rows"][0]["listing_updated_at"] = datetime.now().astimezone().isoformat()
    assert plan(tmp_path, data)["actions"] == []


@pytest.mark.parametrize("change", ["present", "stale", "hash", "incomplete"])
def test_absence_authority_still_required(tmp_path, change):
    data = bundle(tmp_path)
    row = data["rows"][0]
    if change == "present":
        row["source"]["state"] = "present"
    elif change == "stale":
        row["source"]["observed_at"] = "2020-01-01T00:00:00Z"
    elif change == "hash":
        row["source"]["evidence_sha256"] = "a" * 64
    else:
        path = tmp_path / row["source"]["evidence_path"]
        artifact = json.loads(path.read_text())
        artifact["sources"][0]["truncated"] = True
        path.write_text(json.dumps(artifact))
        row["source"]["evidence_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert plan(tmp_path, data)["actions"] == []


def test_before_state_changes_plan_hash(tmp_path):
    data = bundle(tmp_path)
    first = plan(tmp_path, data)
    data["rows"][0]["listing_scraped_at"] = "2020-01-01T00:00:00Z"
    second = plan(tmp_path, data)
    assert first["plan_hash"] != second["plan_hash"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("listing_id", "44444444-4444-4444-8444-444444444444"),
        ("brokerage_id", "44444444-4444-4444-8444-444444444444"),
        ("external_id", "other-provider"),
        ("listing_updated_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_approved_plan_hash_binds_exact_listing_before_state(tmp_path, field, value):
    data = bundle(tmp_path)
    first = plan(tmp_path, data)
    data["rows"][0][field] = value
    assert first["plan_hash"] != plan(tmp_path, data)["plan_hash"]


def test_index_absent_apply_target_drift_refuses_before_psql(tmp_path, monkeypatch):
    result = plan(tmp_path)
    monkeypatch.setattr(
        reconcile, "_load_approval_contract", lambda *_: ("cayman", "AGENTIC-999999")
    )
    monkeypatch.setattr(
        reconcile,
        "load_db_url",
        lambda *_: ("postgresql://synthetic.invalid/test", "synthetic"),
    )
    monkeypatch.setattr(
        reconcile,
        "_run_psql_script",
        lambda *_: pytest.fail("unexpected database dispatch"),
    )
    with pytest.raises(SystemExit, match="target"):
        reconcile.apply_plan(
            result,
            approval_contract_path="synthetic",
            supplied_hash=result["plan_hash"],
            confirmation=f"APPLY cre-listing-lifecycle-reconciliation {result['plan_hash']}",
            env_file="synthetic",
            expected_db_target_sha256="b" * 64,
            batch_size=1,
        )


def test_index_absent_artifact_revalidation_preserves_digest_guard(tmp_path):
    result = plan(tmp_path)
    path = tmp_path / "collector-artifact.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="bytes changed"):
        reconcile._revalidate_plan_evidence(result, max_age_hours=48)
