"""Focused LIST-001/002/003 lifecycle regression coverage (pure/offline)."""

import json
import os
import re

import pytest

import cre_ingest as ingest
import cre_monitor as monitor
import cre_reconcile_listing_lifecycle as reconcile

RUN = "11111111-1111-5111-8111-111111111111"
BID = "22222222-2222-4222-8222-222222222222"
LID = "33333333-3333-4333-8333-333333333333"
AT = "2026-08-31T12:00:00+00:00"


def _group(eid="x"):
    return {
        "slug": "svn", "external_id": eid, "source_key": "svn",
        "url": f"https://example.test/{eid}", "source_lastmod": None,
        "fingerprint": "abc", "norm_status": None, "raw_status": None,
        "canonical_key": f"key-{eid}", "sale_price_usd": None,
        "sale_price_text": None, "lease_rate_min": None,
        "lease_rate_max": None, "lease_rate_text": None,
    }


def _derive(current, prior):
    return monitor.derive_events(
        current, prior, {(BID, "x"): {"id": LID, "status": "active", "deleted": False}},
        {}, {"svn"}, set(), {"svn": True}, RUN,
    )


def test_presence_generation_allows_repeated_cycles_and_replay_is_stable():
    present = {
        "fingerprint": "abc", "soft_deleted": False,
        "observation_present": True, "presence_generation": 0,
        "observed_status": None, "source_key": "svn",
        "url": "https://example.test/x",
    }
    events, _, _, marks, _ = _derive({}, {(BID, "x"): present})
    assert [(event["event_type"], event["presence_generation"]) for event in events] == [
        ("disappeared", 1)
    ]
    assert marks == [(BID, "x")]

    absent = {**present, "observation_present": False, "presence_generation": 1}
    events, *_ = _derive({(BID, "x"): _group()}, {(BID, "x"): absent})
    assert [(event["event_type"], event["presence_generation"]) for event in events] == [
        ("reappeared", 2)
    ]

    present_again = {**present, "presence_generation": 2}
    events, *_ = _derive({}, {(BID, "x"): present_again})
    assert events[0]["presence_generation"] == 3


def test_monitor_sql_is_observation_only_and_job_is_replay_safe():
    sql = monitor.build_write_sql(
        [_group()], [], {}, {}, [(BID, "x")], RUN, AT, "test", ["svn"],
    )
    assert "SET observation_present = false" in sql
    assert "presence_generation = si.presence_generation + 1" in sql
    assert "artifact_run_key" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert not re.search(r"SET\s+soft_deleted\s*=", sql)
    assert not re.search(r"UPDATE\s+credeals\.cre_listings[\s\S]{0,250}(status|deleted_at)\s*=", sql)


def test_ingest_owns_canonical_sync_events_and_final_history_order():
    job = [{"slug": "svn", "discovered": 1, "saved": 0, "errors": 0, "notes": None}]
    sql = ingest.build_sql([], job, AT, {"svn"}, history_guard=False)
    assert "soft_deleted = true" in sql
    assert "observation_present = false" in sql
    assert "scrape_job_id" in sql
    assert "presence_generation" in sql
    assert sql.index("UPDATE credeals.cre_listings l\nSET deleted_at") < sql.index(
        "INSERT INTO credeals.cre_listing_price_history"
    )
    assert "INSERT INTO _prior_vals" in sql
    assert "WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'" in sql


def test_migration_adds_transition_contract_and_runner_order():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    migration = open(os.path.join(root, "sql", "016_cre_listing_lifecycle.sql"), encoding="utf-8").read()
    runner = open(os.path.join(root, "sql", "000_run_all.sql"), encoding="utf-8").read()
    assert "observation_present" in migration
    assert "presence_generation" in migration
    assert "cre_listing_events_presence_transition_uidx" in migration
    assert "cre_listing_events_lifecycle_identity_required" in migration
    assert "presence_generation IS NOT NULL AND scrape_job_id IS NOT NULL" in migration
    assert "artifact_run_key" in migration
    assert "reconciliation_provenance" in migration
    assert "evidence_observed_at" in migration
    assert "evidence_time_semantics" in migration
    assert "cre_listing_price_history_reconciliation_job_uidx" in migration
    assert "observed_at_semantics" in migration
    assert "\\i 016_cre_listing_lifecycle.sql" in runner


def _evidence(state="absent", **source_overrides):
    source = {
        "state": state, "complete": True, "ambiguous": False,
        "observed_at": "2026-08-31T11:00:00Z", "source_key": "svn",
        "url": "https://example.test/x",
        "evidence_sha256": "a" * 64,
    }
    source.update(source_overrides)
    return {"rows": [{
        "listing_id": LID, "brokerage_id": BID, "external_id": "x",
        "listing_deleted": False, "listing_status": "active",
        "index_soft_deleted": False, "observation_present": True,
        "presence_generation": 2, "source": source,
    }]}


def _tracked(*, status="active", sale_price=1_000_000):
    return {
        "sale_price_usd": sale_price,
        "sale_price_per_sf": 100,
        "lease_rate_min": None,
        "lease_rate_max": None,
        "status": status,
        "cap_rate": 0.06,
    }


def _event_only_evidence(state="absent"):
    evidence = _evidence(state=state)
    present = state == "present"
    evidence["rows"][0].update({
        "listing_deleted": not present,
        "listing_status": "active" if present else "inactive",
        "index_soft_deleted": not present,
        "observation_present": present,
        "event_repair": {
            "missing": True,
            "event_type": "reappeared" if present else "disappeared",
            "presence_generation": 2,
            "time_semantics": reconcile._EVENT_TIME_SEMANTICS,
        },
    })
    return evidence


def _history_only_evidence(*, resolution="append_source_backed_history"):
    evidence = _evidence(state="present", tracked_values=_tracked())
    evidence["rows"][0].update({
        "current_tracked_values": _tracked(),
        "latest_history": {
            "id": "44444444-4444-4444-8444-444444444444",
            "observed_at": "2026-08-30T10:00:00Z",
            "tracked_values": _tracked(sale_price=900_000),
        },
        "history_repair": {
            "resolution": resolution,
            "authority_decision": {
                "decision": reconcile._SOURCE_AUTHORITY,
                "decided_by": "Cayman",
                "reason": "Reviewed source document is authoritative",
            },
        },
    })
    return evidence


def test_reconcile_plan_is_deterministic_and_skips_untrusted_evidence(tmp_path):
    plan1 = reconcile.build_plan(_evidence(), as_of="2026-08-31T12:00:00Z")
    plan2 = reconcile.build_plan(_evidence(), as_of="2026-08-31T12:00:00Z")
    assert plan1 == plan2
    assert plan1["actions"][0]["desired_generation"] == 3
    assert len(plan1["actions"][0]["before_hash"]) == 64
    json_path, csv_path = reconcile.write_evidence(plan1, str(tmp_path / "plan"))
    assert json.loads(open(json_path, encoding="utf-8").read())["plan_hash"] == plan1["plan_hash"]
    assert open(csv_path, encoding="utf-8").read().splitlines()[0].startswith("outcome,reason,")

    stale = reconcile.build_plan(
        _evidence(observed_at="2026-08-01T00:00:00Z"),
        as_of="2026-08-31T12:00:00Z",
    )
    assert stale["actions"] == []
    assert stale["skipped"][0]["reason"] == "stale_source_evidence"

    duplicate_bundle = _evidence()
    duplicate_bundle["rows"].append(dict(duplicate_bundle["rows"][0]))
    duplicates = reconcile.build_plan(
        duplicate_bundle, as_of="2026-08-31T12:00:00Z",
    )
    assert duplicates["actions"] == []
    assert {row["reason"] for row in duplicates["skipped"]} == {
        "ambiguous_identity_or_evidence"
    }


def test_reconcile_present_preserves_terminal_status():
    evidence = _evidence(state="present")
    evidence["rows"][0].update({
        "listing_deleted": True, "listing_status": "sold",
        "index_soft_deleted": True, "observation_present": False,
    })
    plan = reconcile.build_plan(evidence, as_of="2026-08-31T12:00:00Z")
    assert plan["actions"][0]["desired_status"] == "sold"


@pytest.mark.parametrize(
    ("state", "event_type"),
    [("absent", "disappeared"), ("present", "reappeared")],
)
def test_reconcile_can_backfill_event_without_rewriting_consistent_state(
    state, event_type,
):
    plan = reconcile.build_plan(
        _event_only_evidence(state), as_of="2026-08-31T12:00:00Z",
    )
    assert plan["actions"][0]["operations"] == ["event_backfill"]
    assert plan["actions"][0]["lifecycle_change"] is False
    assert plan["actions"][0]["event"] == {
        "event_type": event_type,
        "presence_generation": 2,
        "provenance": "approved_missing_event_backfill",
        "evidence_observed_at_semantics": reconcile._EVENT_TIME_SEMANTICS,
        "require_reconciliation_provenance": True,
    }
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "Cayman")
    assert "approved_missing_event_backfill" in sql
    assert "evidence_observed_at" in sql
    assert "source_state_observed_at_not_transition_time" in sql
    assert "detected_at, reconciliation_provenance" in sql
    assert "now(), 'approved_missing_event_backfill'" in sql
    assert "UPDATE credeals.cre_listings" not in sql
    assert "UPDATE credeals.cre_source_index" not in sql
    assert "INSERT INTO credeals.cre_listing_price_history" not in sql


def test_reconcile_history_only_uses_complete_source_values_and_authority():
    plan = reconcile.build_plan(
        _history_only_evidence(), as_of="2026-08-31T12:00:00Z",
    )
    action = plan["actions"][0]
    assert action["operations"] == ["history_alignment"]
    assert action["lifecycle_change"] is False
    assert action["history"]["source_values"] == _tracked()
    assert action["history"]["authority_decision"]["decided_by"] == "Cayman"
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "Cayman")
    assert "reconciliation_job_id" in sql
    assert "approved_source_authority_history_alignment" in sql
    assert "source_evidence_observed_at" in sql
    assert "planned latest history row missing" in sql
    assert "UPDATE credeals.cre_listings" not in sql
    assert "UPDATE credeals.cre_source_index" not in sql
    assert "INSERT INTO credeals.cre_listing_price_history" in sql


def test_reconcile_history_authority_and_source_contract_fail_closed():
    rejected = _history_only_evidence()
    rejected["rows"][0]["history_repair"]["authority_decision"]["decision"] = (
        "database_values_are_authoritative"
    )
    plan = reconcile.build_plan(rejected, as_of="2026-08-31T12:00:00Z")
    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == "history_authority_rejected"

    partial = _history_only_evidence()
    del partial["rows"][0]["source"]["tracked_values"]["cap_rate"]
    plan = reconcile.build_plan(partial, as_of="2026-08-31T12:00:00Z")
    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == "incomplete_source_tracked_values"


def test_reconcile_history_current_update_is_source_bound_and_audited():
    evidence = _history_only_evidence(
        resolution="update_current_from_source_and_append_history",
    )
    evidence["rows"][0]["current_tracked_values"] = _tracked(sale_price=800_000)
    plan = reconcile.build_plan(evidence, as_of="2026-08-31T12:00:00Z")
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "Cayman")
    assert "sale_price_usd='1000000'::numeric" in sql
    assert "field, old_value" in sql
    assert "'sale_price_usd', '800000', '1000000'" in sql
    assert "approved_history_reconciliation:source_authority" in sql


def test_reconcile_apply_requires_exact_named_approval(monkeypatch):
    plan = reconcile.build_plan(_evidence(), as_of="2026-08-31T12:00:00Z")
    with pytest.raises(ValueError, match="named approval"):
        reconcile.apply_plan(
            plan, approved_by="", approval_token=None,
            supplied_hash=plan["plan_hash"], env_file=None,
            expected_db_target_sha256="x", batch_size=1,
        )
    with pytest.raises(ValueError, match="plan hash"):
        reconcile.apply_plan(
            plan, approved_by="Cayman", approval_token=reconcile.APPROVAL_TOKEN,
            supplied_hash="wrong", env_file=None,
            expected_db_target_sha256="x", batch_size=1,
        )


def test_reconcile_sql_locks_hash_checks_drift_and_exact_replay():
    plan = reconcile.build_plan(
        _history_only_evidence(), as_of="2026-08-31T12:00:00Z",
    )
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "Cayman")
    assert "FOR UPDATE OF l, si" in sql
    assert "FOR UPDATE;" in sql
    assert "evidence drifted" in sql
    assert "exact-plan replay: already applied, zero mutations" in sql
    assert "CREATE TEMP TABLE _reconcile_apply" in sql
    assert "IN (SELECT listing_id FROM _reconcile_apply)" in sql
    assert "artifact_run_key" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "ORDER BY h.observed_at DESC, h.id DESC LIMIT 1" in sql
    assert sql.index("exact-plan replay") < sql.index("planned latest history row missing")
    with pytest.raises(ValueError, match="exceeds"):
        reconcile.build_apply_sql(plan["actions"] * 251, plan["plan_hash"], "Cayman")


def test_artifact_identity_is_content_and_order_stable(tmp_path):
    one = tmp_path / "one.json"
    two = tmp_path / "two.json"
    one.write_text('{"a":1}', encoding="utf-8")
    two.write_text('{"b":2}', encoding="utf-8")
    assert monitor.artifact_run_identity([str(one), str(two)]) == monitor.artifact_run_identity(
        [str(two), str(one)]
    )
