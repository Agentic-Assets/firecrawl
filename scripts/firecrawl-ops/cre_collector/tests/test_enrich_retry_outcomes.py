from cre_enrich import run
from test_cre_enrich_psql import _Args, _wire_run


def test_mixed_group_failure_and_unattempted_claims_do_not_consume_attempts(monkeypatch, tmp_path):
    rows = [
        {"id": "done", "source_key": "jll-investor", "external_id": "a", "url": "https://x/a", "attempts": 0},
        {"id": "source-error", "source_key": "svn", "external_id": "b", "url": "https://x/b", "attempts": 4},
        {"id": "attempted", "source_key": "newmark", "external_id": "c", "url": "https://x/c", "attempts": 0},
        {"id": "unattempted", "source_key": "newmark", "external_id": "d", "url": "https://x/d", "attempts": 4},
        {"id": "unknown", "source_key": "newmark", "external_id": "e", "url": "https://x/e", "attempts": 4},
    ]
    artifact = {"listings": [{"url": "https://x/a"}], "sources": [
        {"sourceKey": "jll-investor"},
        {"sourceKey": "svn", "error": "source unavailable"},
        {"sourceKey": "newmark", "retainedObservations": [
            {"event": "generic_enrichment_observation", "claim": {"sourceKey": "newmark", "externalId": "c", "url": "https://x/c"}},
            {"event": "generic_enrichment_failure", "claim": {"sourceKey": "newmark", "externalId": "d", "url": "https://x/d"}, "failure": "not_attempted_after_access_denial"},
        ]},
    ]}
    calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows, write_enriched=artifact)
    assert run(_Args()) == 0
    sql = calls["exec_sqls"]
    completed = next(x for x in sql if "DELETE FROM credeals.cre_enrichment_queue" in x)
    incremented = next(x for x in sql if "attempts = attempts + 1" in x)
    deferred = next(x for x in sql if "enrichment attempt not established" in x)
    assert "'done'::uuid" in completed
    assert "'attempted'::uuid" in incremented
    assert "'unknown'::uuid" in incremented
    assert all(f"'{key}'::uuid" not in incremented for key in ("source-error", "unattempted"))
    assert all(f"'{key}'::uuid" in deferred for key in ("source-error", "unattempted"))
    assert "claimed_at = NULL" not in deferred



def test_same_url_different_source_does_not_complete_unrelated_claim(monkeypatch, tmp_path):
    rows = [
        {"id": "other", "source_key": "svn", "external_id": "x", "url": "https://x/a", "attempts": 0},
        {"id": "done", "source_key": "jll-investor", "external_id": "y", "url": "https://x/a", "attempts": 0},
    ]
    artifact = {"listings": [{"url": "https://x/a", "sourceKey": "jll-investor"}], "sources": []}
    calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows, write_enriched=artifact)
    assert run(_Args()) == 0
    complete = next(x for x in calls["exec_sqls"] if "DELETE FROM credeals.cre_enrichment_queue" in x)
    assert "'done'::uuid" in complete
    assert "'other'::uuid" not in complete


def test_targeted_defer_keeps_claim_tenure_without_increment():
    from cre_enrich import build_targeted_queue_sql
    item = {"claimId": "id", "externalId": "ex", "url": "https://x", "claimedAt": "2026-01-01Z"}
    sql = build_targeted_queue_sql([item], "defer", last_error="unknown")
    assert "SET claimed_at=NULL" not in sql
    assert "attempts=attempts+1" not in sql
    assert "q.claimed_at IS NOT DISTINCT FROM e.claimed_at" in sql

def test_same_source_url_with_different_external_ids_refuses_ambiguous_ingest(monkeypatch, tmp_path):
    rows = [
        {"id": "one", "source_key": "svn", "external_id": "x", "url": "https://x/a", "attempts": 0},
        {"id": "two", "source_key": "svn", "external_id": "y", "url": "https://x/a", "attempts": 0},
    ]
    calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows,
                      write_enriched={"listings": [{"url": "https://x/a", "sourceKey": "svn"}]})
    assert run(_Args()) == 1
    assert calls["ingest_called"] is False


def test_newmark_source_stop_defers_missing_observations_and_success_is_completed(monkeypatch, tmp_path):
    rows = [
        {"id": "done", "source_key": "newmark", "external_id": "a", "url": "https://x/a", "attempts": 0},
        {"id": "later", "source_key": "newmark", "external_id": "b", "url": "https://x/b", "attempts": 4},
    ]
    artifact = {"listings": [{"url": "https://x/a"}], "sources": [
        {"sourceKey": "newmark", "evidenceError": "newmark_detail_access_denial", "retainedObservations": []},
    ]}
    calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows, write_enriched=artifact)
    assert run(_Args()) == 0
    sql = "\n".join(calls["exec_sqls"])
    assert "'done'::uuid" in sql
    assert "'later'::uuid" in sql
    assert "attempts = attempts + 1" not in sql


def test_malformed_source_evidence_never_matches_a_different_claim():
    from cre_enrich import deferred_claim_ids
    rows = [{"id": "one", "source_key": "svn", "external_id": "x", "url": "https://x/a"}]
    artifact = {"sources": [None, {"sourceKey": "svn", "retainedObservations": [None,
        {"stopReason": "access_denial", "claim": {"sourceKey": "newmark", "externalId": "x", "url": "https://x/a"}},
    ]}]}
    assert deferred_claim_ids(rows, artifact) == set()
