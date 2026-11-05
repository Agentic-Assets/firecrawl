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
    lambda c: c.update(log=c["log"] + "[other] rc=0\n"),
    lambda c: c.update(log=c["log"].replace("[clock] command:", "[earlier] rc=1\n[clock] command:").replace("[clock] rc=1\n", "")),
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

def test_authenticated_wrapper_binds_paths_command_and_post_snapshot(tmp_path, monkeypatch, case):
    import json
    import os

    import cre_checkpoint_refresh as cp
    import cre_franklin_rollback_evidence as module
    import cre_franklin_site_admission as admission
    import cre_franklin_site_sql as source_sql

    run = tmp_path.resolve() / "run"
    run.mkdir()
    (run / "sources").mkdir()
    (run / "claims").mkdir()
    (run / "logs").mkdir()
    artifact = run / "sources" / "franklin.json"
    claims = run / "claims" / "sealed.json"
    log = run / "logs" / "ingest.log"
    artifact.write_bytes(b"synthetic artifact")
    claims.write_text("{}")
    artifact.chmod(0o600)
    claims.chmod(0o600)
    binding = case["binding"]
    binding["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    binding["claims_sha256"] = hashlib.sha256(claims.read_bytes()).hexdigest()
    target = binding["target_sha256"]
    attempt = binding["attempt_id"]
    command = (f"/usr/bin/python3 cre_ingest.py --in {artifact} --skip-post-commit-summary "
               f"--require-strict-freshness --env-file /fixture/env --expected-db-target-sha256 {target} "
               f"--franklin-site-claims {claims} --checkpoint-ingest-attempt {attempt} "
               f"--expected-db-target-sha256 {target}")
    def write_log(cmd=command, suffix="[clock] rc=1\n"):
        log.write_text("[clock] command: " + cmd + "\n"
                       "psql:<stdin>:5: ERROR:  Franklin expected scalar postimage differs\n"
                       "CONTEXT: PL/pgSQL function inline_code_block line 2 at RAISE\n" + suffix)
        log.chmod(0o644)

    write_log()
    m = {"run_id": binding["generation_id"], "collector_git_sha": "f" * 40,
         "pipeline": {"commit_outcome_unknown": True},
         "sources": {"franklin-street": {
             "state": "ingest_recovery_required", "subset_ingest_attempt_id": attempt,
             "ingest": {"finished_at": "clock", "rc": 1, "attempt_id": attempt, "log": "logs/ingest.log"},
             "artifact": {"path": "sources/franklin.json", "sha256": binding["artifact_sha256"]},
             "franklin_partial_claims": {"path": str(claims), "sha256": binding["claims_sha256"]},
             "dry_run": {"sql": {"sha256": case["expected_sql_sha"]}},
         }}}
    manifest = run / "manifest.json"
    def save_manifest():
        manifest.write_text(json.dumps(m))
        manifest.chmod(0o600)

    save_manifest()
    monkeypatch.setattr(cp, "git_identity", lambda: ("f" * 40, False))
    monkeypatch.setattr(cp, "manifest_database_target_sha256", lambda _: target)
    monkeypatch.setattr(cp, "safe_process_env", dict)
    context = {"listings": [{}], "held": [], "sql_binding": {"generation_id": binding["generation_id"]}}
    monkeypatch.setattr(admission, "ingest_subset_context", lambda *a, **k: context)
    monkeypatch.setattr(source_sql, "build_sql", lambda *a: case["sql"])
    monkeypatch.setattr(source_sql, "stage", lambda data: data)
    monkeypatch.setattr(source_sql, "_binding", lambda *a: {"job_id": binding["job_id"], "key": binding["artifact_run_key"]})
    monkeypatch.setattr(module, "query_snapshot", lambda *a: case["snapshot"])

    assert module.authenticated_report(run, "/fixture/env")["outcome"] == "exact_precommit_rollback"
    for extra in (" --checkpoint-ingest-attempt=" + "f" * 32,
                  " --in=/other", " --expected-db-target-sha256=" + "f" * 64,
                  " --checkpoint-ingest-att " + attempt):
        write_log(command + extra)
        with pytest.raises(ValueError, match="command differs"):
            module.authenticated_report(run, "/fixture/env")
    write_log()
    def changed_snapshot(*args):
        artifact.write_bytes(b"changed")
        return case["snapshot"]
    monkeypatch.setattr(module, "query_snapshot", changed_snapshot)
    with pytest.raises(ValueError, match="input changed"):
        module.authenticated_report(run, "/fixture/env")
    artifact.write_bytes(b"synthetic artifact")
    monkeypatch.setattr(module, "query_snapshot", lambda *a: case["snapshot"])
    m["sources"]["franklin-street"]["artifact"]["path"] = "sources/../sources/franklin.json"
    save_manifest()
    with pytest.raises(ValueError, match="artifact path"):
        module.authenticated_report(run, "/fixture/env")
    assert os.stat(artifact).st_mode & 0o077 == 0