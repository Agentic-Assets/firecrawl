"""Focused LIST-001/002/003 lifecycle regression coverage (pure/offline)."""

import json
import hashlib
import os
import re
import subprocess
from datetime import datetime, timedelta

import pytest

import cre_ingest as ingest
import cre_monitor as monitor
import cre_reconcile_listing_lifecycle as reconcile

RUN = "11111111-1111-5111-8111-111111111111"
BID = "22222222-2222-4222-8222-222222222222"
LID = "33333333-3333-4333-8333-333333333333"
AT = "2026-08-31T12:00:00+00:00"
OBSERVED = "2026-08-31T11:00:00Z"


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
    finished = "2026-08-31T12:05:00+00:00"
    sql = ingest.build_sql(
        [], job, AT, {"svn"}, history_guard=False, finished_at=finished,
    )
    assert "soft_deleted = true" in sql
    assert "observation_present = false" in sql
    assert "scrape_job_id" in sql
    assert "presence_generation" in sql
    assert sql.index("UPDATE credeals.cre_listings l\nSET deleted_at") < sql.index(
        "INSERT INTO credeals.cre_listing_price_history"
    )
    assert "INSERT INTO _prior_vals" in sql
    assert "WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'" in sql
    assert "FOR UPDATE OF l" in sql
    assert "FOR UPDATE OF si" in sql
    present_advisory = sql.index("-- Global lifecycle lock order")
    present_source = sql.index("-- Lock present source-index rows", present_advisory)
    present_listing = sql.index("-- Lock present canonical listing rows", present_source)
    retired_advisory = sql.index(
        "-- Follow the global advisory-then-source-index-then-listing lock order"
    )
    retired_source = sql.index("-- Lock retirement source-index rows", retired_advisory)
    retired_listing = sql.index("-- Lock retirement canonical listing rows", retired_source)
    assert present_advisory < present_source < present_listing
    assert retired_advisory < retired_source < retired_listing
    assert sql.count("pg_advisory_xact_lock") >= 2
    assert "si.last_enumerated_at < jm.finished_at" in sql
    assert "jm.finished_at, jm.finished_at" in sql
    assert "applied.presence_generation, jm.finished_at" in sql


def test_inventory_only_updates_all_lifecycle_columns_atomically():
    sql = ingest.build_sql([], [], AT, set(), history_guard=False)
    inventory = sql[sql.index("INSERT INTO credeals.cre_source_index AS si ("):]
    assert "observation_present = true" in inventory
    assert "observation_present = false" in inventory
    assert "presence_generation = CASE" in inventory
    assert "presence_changed_at = CASE" in inventory
    assert "EXCLUDED.last_enumerated_at > si.last_enumerated_at" in inventory


def test_migration_adds_transition_contract_and_runner_order():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    migration = open(os.path.join(root, "sql", "016_cre_listing_lifecycle.sql"), encoding="utf-8").read()
    runner = open(os.path.join(root, "sql", "000_run_all.sql"), encoding="utf-8").read()
    assert "observation_present" in migration
    assert "presence_generation" in migration
    assert "cre_listing_events_presence_transition_uidx" in migration
    assert "cre_listing_events_lifecycle_identity_required" in migration
    assert "OR presence_generation IS NOT NULL" in migration
    assert "presence_generation IS NOT NULL AND scrape_job_id IS NOT NULL" not in migration
    assert (
        "WHERE event_type NOT IN ('disappeared', 'reappeared')\n"
        "      AND scrape_job_id IS NOT NULL"
    ) in migration
    assert "artifact_run_key" in migration
    assert "reconciliation_provenance" in migration
    assert "evidence_observed_at" in migration
    assert "evidence_time_semantics" in migration
    assert "cre_listing_price_history_reconciliation_job_uidx" in migration
    assert "observed_at_semantics" in migration
    assert "\\i 016_cre_listing_lifecycle.sql" not in runner
    assert "CRE_LISTING_LIFECYCLE_APPROVAL_REF" in migration
    assert "Transaction-local readback" in migration
    assert "confdeltype = 'n'" in migration
    assert migration.rstrip().endswith("COMMIT;")


def _evidence(tmp_path, state="absent", **source_overrides):
    source = {
        "state": state, "complete": False, "ambiguous": False,
        "observed_at": OBSERVED, "source_key": "svn",
        "url": "https://example.test/x",
    }
    source.update(source_overrides)
    finished = datetime.fromisoformat(source["observed_at"].replace("Z", "+00:00"))
    started = (finished - timedelta(minutes=5)).isoformat()
    artifact = {
        "runMeta": {
            "mode": "full", "transactions": ["sale", "lease"],
            "maxItemsPerSource": None, "startedAt": started,
            "finishedAt": source["observed_at"],
        },
        "brokers": [],
        "sources": [
            {
                "sourceKey": source["source_key"], "transaction": transaction,
                "supported": True,
                "listingsCollected": 1 if state == "present" and transaction == "sale" else 0,
                "error": None, "truncated": False,
            }
            for transaction in ("sale", "lease")
        ],
        "listings": ([{
            "sourceKey": source["source_key"], "transactionMode": "sale",
            "id": "x", "url": source["url"],
        }] if state == "present" else []),
        "totalListings": 1 if state == "present" else 0,
    }
    artifact_path = tmp_path / "collector-artifact.json"
    artifact_bytes = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    artifact_path.write_bytes(artifact_bytes)
    source["evidence_path"] = artifact_path.name
    source.setdefault("evidence_sha256", hashlib.sha256(artifact_bytes).hexdigest())
    return {"rows": [{
        "listing_id": LID, "brokerage_id": BID, "external_id": "x",
        "listing_deleted": False, "listing_status": "active",
        "index_soft_deleted": False, "observation_present": True,
        "presence_generation": 2, "source": source,
    }]}


def _plan(bundle, tmp_path, **kwargs):
    return reconcile.build_plan(
        bundle, evidence_base_dir=tmp_path, **kwargs,
    )


def _approval_contract(tmp_path, plan, *, operator="cayman", approval_ref="AGENTIC-1"):
    path = tmp_path / "lifecycle-approval.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "operation": "cre-listing-lifecycle-reconciliation",
        "operator": operator,
        "approval_ref": approval_ref,
        "plan_hash": plan["plan_hash"],
    }), encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _tracked(*, status="active", sale_price=1_000_000):
    return {
        "sale_price_usd": sale_price,
        "sale_price_per_sf": 100,
        "lease_rate_min": None,
        "lease_rate_max": None,
        "status": status,
        "cap_rate": 0.06,
    }


def _event_only_evidence(tmp_path, state="absent"):
    evidence = _evidence(tmp_path, state=state)
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


def _history_only_evidence(tmp_path, *, resolution="append_source_backed_history"):
    evidence = _evidence(tmp_path, state="present", tracked_values=_tracked())
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
    plan1 = _plan(_evidence(tmp_path), tmp_path)
    plan2 = _plan(_evidence(tmp_path), tmp_path)
    assert plan1 == plan2
    assert plan1["actions"][0]["desired_generation"] == 3
    assert len(plan1["actions"][0]["before_hash"]) == 64
    json_path, csv_path = reconcile.write_evidence(plan1, str(tmp_path / "plan"))
    assert json.loads(open(json_path, encoding="utf-8").read())["plan_hash"] == plan1["plan_hash"]
    assert open(csv_path, encoding="utf-8").read().splitlines()[0].startswith("outcome,reason,")

    stale = _plan(_evidence(tmp_path, observed_at="2026-08-01T00:00:00Z"), tmp_path)
    assert stale["actions"] == []
    assert stale["skipped"][0]["reason"] == "stale_source_evidence"

    duplicate_bundle = _evidence(tmp_path)
    duplicate_bundle["rows"].append(dict(duplicate_bundle["rows"][0]))
    duplicates = _plan(duplicate_bundle, tmp_path)
    assert duplicates["actions"] == []
    assert {row["reason"] for row in duplicates["skipped"]} == {
        "ambiguous_identity_or_evidence"
    }


def test_reconcile_rejects_arbitrary_or_mutated_evidence_bytes(tmp_path):
    wrong_digest = _plan(_evidence(tmp_path, evidence_sha256="a" * 64), tmp_path)
    assert wrong_digest["actions"] == []
    assert wrong_digest["skipped"][0]["reason"] == "source_evidence_hash_mismatch"

    evidence = _evidence(tmp_path, state="present")
    artifact_path = tmp_path / evidence["rows"][0]["source"]["evidence_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["listings"][0]["url"] = "https://example.test/other"
    changed = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    artifact_path.write_bytes(changed)
    evidence["rows"][0]["source"]["evidence_sha256"] = hashlib.sha256(changed).hexdigest()
    mismatch = _plan(evidence, tmp_path)
    assert mismatch["actions"] == []
    assert mismatch["skipped"][0]["reason"] == "source_url_does_not_match_artifact"


@pytest.mark.parametrize(
    ("section", "index", "field", "value"),
    [
        ("runMeta", None, "transactions", ["sale"]),
        ("runMeta", None, "maxItemsPerSource", 100),
        ("sources", 1, "supported", False),
        ("sources", 0, "error", "timeout"),
        ("sources", 0, "truncated", True),
        ("sources", 0, "listingsCollected", 1),
        (None, None, "totalListings", 1),
        (None, None, "totalListings", False),
    ],
)
def test_reconcile_requires_strict_successful_full_source_contract(
    tmp_path, section, index, field, value,
):
    evidence = _evidence(tmp_path)
    source = evidence["rows"][0]["source"]
    source["complete"] = True  # caller assertion must not rescue invalid bytes
    artifact_path = tmp_path / source["evidence_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    target = artifact if section is None else artifact[section]
    if index is not None:
        target = target[index]
    target[field] = value
    artifact_bytes = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    artifact_path.write_bytes(artifact_bytes)
    source["evidence_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()

    plan = _plan(evidence, tmp_path)

    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"].startswith("source_evidence_scope_")


def test_reconcile_requires_exact_source_pass_coverage(tmp_path):
    evidence = _evidence(tmp_path)
    source = evidence["rows"][0]["source"]
    artifact_path = tmp_path / source["evidence_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["sources"].pop()
    artifact_bytes = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    artifact_path.write_bytes(artifact_bytes)
    source["evidence_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()

    plan = _plan(evidence, tmp_path)

    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == (
        "source_evidence_scope_source_pass_coverage_mismatch"
    )


def test_reconcile_ignores_caller_complete_flag_when_artifact_is_strict(tmp_path):
    evidence = _evidence(tmp_path)
    evidence["rows"][0]["source"]["complete"] = False
    assert len(_plan(evidence, tmp_path)["actions"]) == 1


def test_reconcile_present_preserves_terminal_status(tmp_path):
    evidence = _evidence(tmp_path, state="present")
    evidence["rows"][0].update({
        "listing_deleted": True, "listing_status": "sold",
        "index_soft_deleted": True, "observation_present": False,
    })
    plan = _plan(evidence, tmp_path)
    assert plan["actions"][0]["desired_status"] == "sold"


@pytest.mark.parametrize(
    ("state", "event_type"),
    [("absent", "disappeared"), ("present", "reappeared")],
)
def test_reconcile_can_backfill_event_without_rewriting_consistent_state(
    tmp_path, state, event_type,
):
    plan = _plan(_event_only_evidence(tmp_path, state), tmp_path)
    assert plan["actions"][0]["operations"] == ["event_backfill"]
    assert plan["actions"][0]["lifecycle_change"] is False
    assert plan["actions"][0]["event"] == {
        "event_type": event_type,
        "presence_generation": 2,
        "provenance": "approved_missing_event_backfill",
        "evidence_observed_at_semantics": reconcile._EVENT_TIME_SEMANTICS,
        "require_reconciliation_provenance": True,
    }
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-1")
    assert "approved_missing_event_backfill" in sql
    assert "evidence_observed_at" in sql
    assert "source_state_observed_at_not_transition_time" in sql
    assert "detected_at, reconciliation_provenance" in sql
    assert "now(), 'approved_missing_event_backfill'" in sql
    assert "UPDATE credeals.cre_listings" not in sql
    assert "UPDATE credeals.cre_source_index" not in sql
    assert "INSERT INTO credeals.cre_listing_price_history" not in sql


def test_reconcile_history_only_uses_complete_source_values_and_authority(tmp_path):
    plan = _plan(_history_only_evidence(tmp_path), tmp_path)
    action = plan["actions"][0]
    assert action["operations"] == ["history_alignment"]
    assert action["lifecycle_change"] is False
    assert action["history"]["source_values"] == _tracked()
    assert action["history"]["authority_decision"]["decided_by"] == "Cayman"
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-1")
    assert "reconciliation_job_id" in sql
    assert "approved_source_authority_history_alignment" in sql
    assert "source_evidence_observed_at" in sql
    assert "planned latest history row missing" in sql
    assert "UPDATE credeals.cre_listings" not in sql
    assert "UPDATE credeals.cre_source_index" not in sql
    assert "INSERT INTO credeals.cre_listing_price_history" in sql


def test_reconcile_history_authority_and_source_contract_fail_closed(tmp_path):
    rejected = _history_only_evidence(tmp_path)
    rejected["rows"][0]["history_repair"]["authority_decision"]["decision"] = (
        "database_values_are_authoritative"
    )
    plan = _plan(rejected, tmp_path)
    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == "history_authority_rejected"

    partial = _history_only_evidence(tmp_path)
    del partial["rows"][0]["source"]["tracked_values"]["cap_rate"]
    plan = _plan(partial, tmp_path)
    assert plan["actions"] == []
    assert plan["skipped"][0]["reason"] == "incomplete_source_tracked_values"


def test_reconcile_history_current_update_is_source_bound_and_audited(tmp_path):
    evidence = _history_only_evidence(
        tmp_path,
        resolution="update_current_from_source_and_append_history",
    )
    evidence["rows"][0]["current_tracked_values"] = _tracked(sale_price=800_000)
    plan = _plan(evidence, tmp_path)
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-1")
    assert "sale_price_usd='1000000'::numeric" in sql
    assert "field, old_value" in sql
    assert "'sale_price_usd', '800000', '1000000'" in sql
    assert "approved_history_reconciliation:source_authority" in sql


def test_reconcile_apply_requires_operator_reference_and_exact_confirmation(tmp_path):
    plan = _plan(_evidence(tmp_path), tmp_path)
    with pytest.raises(ValueError, match="private approval contract"):
        reconcile.apply_plan(
            plan, approval_contract_path=None, confirmation="bad",
            supplied_hash=plan["plan_hash"], env_file=None,
            expected_db_target_sha256="x", batch_size=1,
        )
    with pytest.raises(ValueError, match="plan hash"):
        reconcile.apply_plan(
            plan, approval_contract_path=_approval_contract(tmp_path, plan),
            confirmation="bad",
            supplied_hash="wrong", env_file=None,
            expected_db_target_sha256="x", batch_size=1,
        )
    public_contract = _approval_contract(tmp_path, plan)
    os.chmod(public_contract, 0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        reconcile.apply_plan(
            plan, approval_contract_path=public_contract, confirmation="bad",
            supplied_hash=plan["plan_hash"], env_file=None,
            expected_db_target_sha256="x", batch_size=1,
        )


def test_reconcile_sql_locks_hash_checks_drift_and_exact_replay(tmp_path):
    plan = _plan(_history_only_evidence(tmp_path), tmp_path)
    sql = reconcile.build_apply_sql(plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-1")
    advisory = sql.index("pg_advisory_xact_lock")
    source_lock = sql.index("FROM credeals.cre_source_index si", advisory)
    listing_lock = sql.index("FROM credeals.cre_listings l", source_lock)
    assert advisory < source_lock < listing_lock
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
        reconcile.build_apply_sql(
            plan["actions"] * 251, plan["plan_hash"], "cayman", "AGENTIC-1",
        )


def test_multibatch_jobs_finalize_only_after_every_batch_succeeds(tmp_path, monkeypatch):
    evidence = _evidence(tmp_path)
    second = json.loads(json.dumps(evidence["rows"][0]))
    second["listing_id"] = "55555555-5555-4555-8555-555555555555"
    second["external_id"] = "y"
    second["source"]["url"] = "https://example.test/y"
    evidence["rows"].append(second)
    plan = _plan(evidence, tmp_path)
    assert len(plan["actions"]) == 2

    monkeypatch.setattr(reconcile, "load_db_url", lambda _path: ("postgresql://fixture", None))
    monkeypatch.setattr(reconcile, "assert_expected_database_target", lambda *_args: None)
    monkeypatch.setattr(reconcile, "find_psql", lambda: "psql")
    monkeypatch.setattr(reconcile, "_revalidate_plan_evidence", lambda *_args, **_kwargs: None)
    calls = []

    def fail_second(_psql, _url, sql):
        calls.append(sql)
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, "psql")

    monkeypatch.setattr(reconcile, "_run_psql_script", fail_second)
    confirmation = f"APPLY cre-listing-lifecycle-reconciliation {plan['plan_hash']}"
    with pytest.raises(subprocess.CalledProcessError):
        reconcile.apply_plan(
            plan, approval_contract_path=_approval_contract(tmp_path, plan),
            confirmation=confirmation, supplied_hash=plan["plan_hash"],
            env_file=None, expected_db_target_sha256="fixture", batch_size=1,
        )
    assert len(calls) == 2
    assert all("'running', now(), NULL" in sql for sql in calls)
    assert all("SET status = 'completed'" not in sql for sql in calls)

    calls.clear()
    monkeypatch.setattr(
        reconcile, "_run_psql_script", lambda _psql, _url, sql: calls.append(sql),
    )
    reconcile.apply_plan(
        plan, approval_contract_path=_approval_contract(tmp_path, plan),
        confirmation=confirmation, supplied_hash=plan["plan_hash"],
        env_file=None, expected_db_target_sha256="fixture", batch_size=1,
    )
    assert len(calls) == 3
    assert all("SET status = 'completed'" not in sql for sql in calls[:2])
    assert "SET status = 'completed'" in calls[2]


def test_artifact_identity_is_content_and_order_stable(tmp_path):
    one = tmp_path / "one.json"
    two = tmp_path / "two.json"
    one.write_text('{"a":1}', encoding="utf-8")
    two.write_text('{"b":2}', encoding="utf-8")
    assert monitor.artifact_run_identity([str(one), str(two)]) == monitor.artifact_run_identity(
        [str(two), str(one)]
    )
