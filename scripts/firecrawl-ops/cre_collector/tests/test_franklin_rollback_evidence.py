import copy
import hashlib

import pytest

from cre_franklin_rollback_evidence import classify, snapshot_sql


@pytest.fixture
def case():
    binding = {
        "generation_id": "run-1", "attempt_id": "a" * 32,
        "artifact_sha256": "b" * 64, "claims_sha256": "c" * 64,
        "target_sha256": "d" * 64, "job_id": "e1bc6ef5-9889-5120-bdf5-e9fde98344fe",
        "artifact_run_key": "ingest:v1:" + "e" * 64,
    }
    sql = "\nBEGIN;\nDO $$ BEGIN\n RAISE EXCEPTION 'Franklin expected scalar postimage differs';\nEND $$;\nCOMMIT;\n"
    log = ("[clock] command: python cre_ingest.py --franklin-site-claims /fixture "
           "--checkpoint-ingest-attempt " + binding["attempt_id"]
           + " --expected-db-target-sha256 " + binding["target_sha256"] + "\n"
           "psql:<stdin>:5: ERROR:  Franklin expected scalar postimage differs\n"
           "CONTEXT:  PL/pgSQL function inline_code_block line 2 at RAISE\n"
           "[clock] rc=1\n")
    snapshot = {"target_sha256": binding["target_sha256"], "job_uuid_or_key": 0,
                "last_generation": 0, "history_generation": 0, "malformed_history": 0}
    return {"binding": binding, "expected_sql_sha": hashlib.sha256(sql.encode()).hexdigest(),
            "sql": sql, "log": log, "snapshot": snapshot}


def test_exact_refusal_requires_all_zero_snapshot(case):
    result = classify(**case)
    assert result["outcome"] == "exact_precommit_rollback"
    assert "remote and descendant settlement separate" in result["scope"]
    for field in ("job_uuid_or_key", "last_generation", "history_generation", "malformed_history"):
        changed = copy.deepcopy(case)
        changed["snapshot"][field] = 1
        with pytest.raises(ValueError):
            classify(**changed)


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(sql=c["sql"].replace("COMMIT;", "ROLLBACK;")),
    lambda c: c.update(expected_sql_sha="f" * 64),
    lambda c: c.update(log=c["log"].replace("stdin>:5", "stdin>:6")),
    lambda c: c.update(log=c["log"].replace("rc=1", "rc=0")),
    lambda c: c.update(log=c["log"].replace(c["binding"]["attempt_id"], "f" * 32)),
    lambda c: c.update(log=c["log"].replace(c["binding"]["target_sha256"], "f" * 64)),
    lambda c: c["snapshot"].update(job_uuid_or_key=False),
    lambda c: c["snapshot"].update(target_sha256="f" * 64),
])
def test_ambiguous_or_unbound_evidence_refused(case, mutation):
    mutation(case)
    with pytest.raises(ValueError):
        classify(**case)


def test_snapshot_queries_all_parents_and_both_job_identities(case):
    sql = snapshot_sql(case["binding"])
    assert "REPEATABLE READ READ ONLY" in sql
    assert "id=" in sql and " OR artifact_run_key=" in sql
    assert "deleted_at" not in sql
    assert "franklinSiteLastGenerationId" in sql
    assert "jsonb_array_elements" in sql