"""AGENTIC-4206: real caller-bound worker/ingest contracts, synthetic IO only."""

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import cre_enrich as worker
import cre_ingest as ingest
import cre_stop_guarded_tier as stop
import pytest


def fixture_batch():
    now = datetime.now(timezone.utc)
    stamp = lambda seconds: (now + timedelta(seconds=seconds)).isoformat()
    context = {
        "sourceKey": "matthews",
        "generationId": "matthews-targeted-" + "a" * 32,
        "generationStartedAt": stamp(-5),
    }
    items = [
        {
            "sourceKey": "matthews",
            "externalId": identity,
            "url": "https://www.matthews.com/properties/" + identity,
            "claimId": "10000000-0000-4000-8000-00000000000" + str(index),
            "claimedAt": stamp(-10),
        }
        for index, identity in enumerate(("test-sale", "leasing-test-lease"), 1)
    ]
    listings = [
        {
            "sourceKey": "matthews",
            "id": item["externalId"],
            "url": item["url"],
            "canonicalUrl": item["url"],
            "name": "Synthetic listing",
            "transactionMode": "lease" if "leasing-" in item["externalId"] else "sale",
            "transactionType": "Lease" if "leasing-" in item["externalId"] else "Sale",
            "inventoryObservedAt": stamp(-4),
            "detailObservedAt": stamp(-2),
            "freshnessProvenance": {
                "generationId": context["generationId"],
                "detailScope": "detail_page",
                "cacheDisposition": "live",
                "method": "matthews_canonical_detail",
                "identityMethod": "canonical_property_url",
            },
            "photos": [{"url": "https://www.matthews.com/synthetic.jpg"}],
        }
        for item in items
    ]
    artifact = {
        "runMeta": {
            "mode": "enrich",
            "startedAt": stamp(-5),
            "finishedAt": stamp(-1),
            "matthewsTargetedRefresh": context,
            "freshness": {
                "generationId": context["generationId"],
                "generationStartedAt": context["generationStartedAt"],
                "requireFreshDetails": True,
            },
        },
        "sources": [
            {
                "sourceKey": "matthews",
                "supported": True,
                "enricher": "bespoke",
                "requested": 2,
                "listingsCollected": 2,
            }
        ],
        "listings": listings,
    }
    return {"items": items, "matthewsTargetedRefresh": context}, artifact


def test_sale_and_lease_are_bound_to_separate_real_observations():
    claims, artifact = fixture_batch()
    rows = ingest.validate_matthews_targeted_artifact(artifact, claims)
    assert [row["transactionMode"] for row in rows] == ["sale", "lease"]
    normalized = [ingest.to_row(row, {}, row["detailObservedAt"]) for row in rows]
    assert [row["transaction_type"] for row in normalized] == ["sale", "lease"]
    assert all(row["inventoryObservedAt"] != row["detailObservedAt"] for row in rows)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generationId", "old-generation"),
        ("generationStartedAt", "2000-01-01T00:00:00Z"),
    ],
)
def test_self_consistent_artifact_cannot_replace_caller_boundary(field, value):
    claims, artifact = fixture_batch()
    claims = copy.deepcopy(claims)
    artifact["runMeta"]["matthewsTargetedRefresh"][field] = value
    artifact["runMeta"]["freshness"][field] = value
    if field == "generationId":
        for row in artifact["listings"]:
            row["freshnessProvenance"][field] = value
    with pytest.raises(ValueError):
        ingest.validate_matthews_targeted_artifact(artifact, claims)


@pytest.mark.parametrize(
    "change",
    [
        "id",
        "url",
        "canonical",
        "tenure",
        "duplicate",
        "count",
        "stale_inventory",
        "stale_detail",
        "wrong_source",
    ],
)
def test_targeted_binding_failures_refuse_before_sql(change):
    claims, artifact = fixture_batch()
    row = artifact["listings"][0]
    if change == "id":
        row["id"] = "alias"
    if change == "url":
        row["url"] += "-alias"
    if change == "canonical":
        row["canonicalUrl"] += "-alias"
    if change == "tenure":
        row["transactionMode"] = "lease"
    if change == "duplicate":
        artifact["listings"][1] = copy.deepcopy(row)
    if change == "count":
        artifact["sources"][0]["listingsCollected"] = 3
    if change == "stale_inventory":
        row["inventoryObservedAt"] = "2000-01-01T00:00:00Z"
    if change == "stale_detail":
        row["detailObservedAt"] = "2000-01-01T00:00:00Z"
    if change == "wrong_source":
        row["sourceKey"] = "cbre"
    with pytest.raises(ValueError):
        ingest.validate_matthews_targeted_artifact(artifact, claims)


def test_unknown_claim_tenure_is_not_fabricated_sale():
    rows = [
        {
            "id": "q",
            "source_key": "matthews",
            "external_id": "leasing-test",
            "url": "https://www.matthews.com/properties/leasing-test",
            "claimed_at": "x",
        }
    ]
    assert (
        "transaction" not in worker.claim_rows_to_items(rows, targeted=True)["items"][0]
    )


def test_claim_witness_changes_refuse_successor_mutation():
    claims, artifact = fixture_batch()
    sql = worker.build_targeted_queue_sql(
        claims["items"], "complete", artifact=artifact, claims=claims
    )
    assert "claimed_at" in sql and "FOR UPDATE" in sql
    assert "RAISE EXCEPTION" in sql
    assert "source_key" in sql and "external_id" in sql and "url" in sql


def test_real_worker_readback_failure_retains_claims(monkeypatch, tmp_path):
    claims, artifact = fixture_batch()
    calls = []
    monkeypatch.setattr(worker, "OUT_ENRICH_DIR", str(tmp_path))
    run_id = "2026-10-14T150000Z-aaaaaaaaaaaa"
    (tmp_path / run_id).mkdir(mode=0o700)
    monkeypatch.setattr(stop, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(stop, "require_guarded_matthews_parent", lambda *a, **kw: {})
    monkeypatch.setattr(stop, "_process_identity", lambda pid: {"pid": pid})
    monkeypatch.setattr(worker, "load_db_url", lambda _: ("synthetic", "private"))
    monkeypatch.setattr(worker, "assert_expected_database_target", lambda *a: None)
    returned = [
        (
            i["claimId"],
            "matthews",
            i["externalId"],
            i["url"],
            "changed",
            "0",
            i["claimedAt"],
        )
        for i in claims["items"]
    ]
    monkeypatch.setattr(
        worker,
        "_psql_query",
        lambda db, sql: returned if "RETURNING" in sql else [("0",)],
    )
    monkeypatch.setattr(worker, "_psql_exec", lambda db, sql: calls.append(sql))

    def native(argv, **kwargs):
        if "collect.ts" in argv:
            claim = json.loads(
                (tmp_path / argv[argv.index("--enrich-input") + 1]).read_text()
            )
            fresh = artifact["runMeta"]["freshness"]
            ctx = claim["matthewsTargetedRefresh"]
            artifact["runMeta"]["matthewsTargetedRefresh"] = ctx
            fresh.update(
                {
                    "generationId": ctx["generationId"],
                    "generationStartedAt": ctx["generationStartedAt"],
                }
            )
            observed = datetime.now(timezone.utc).isoformat()
            for row in artifact["listings"]:
                row.update(inventoryObservedAt=observed, detailObservedAt=observed)
                row["freshnessProvenance"]["generationId"] = ctx["generationId"]
            (tmp_path / argv[argv.index("--out") + 1]).write_text(json.dumps(artifact))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", native)
    args = SimpleNamespace(
        batch=2,
        source="matthews",
        env_file=None,
        dry_run=False,
        expected_db_target_sha256="a" * 64,
        matthews_targeted_refresh=True,
        work_seconds=600,
        guarded_run_id=run_id,
    )
    assert worker.run(args) == 1
    assert calls == []  # No completion, retry or release after uncertain readback.


def test_direct_opt_in_refuses_before_credentials_or_claims(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "load_db_url", lambda _: calls.append("credentials"))
    monkeypatch.setattr(worker, "_psql_query", lambda *_: calls.append("claims"))
    args = SimpleNamespace(
        batch=2,
        source="matthews",
        env_file=None,
        dry_run=False,
        expected_db_target_sha256="a" * 64,
        matthews_targeted_refresh=True,
        work_seconds=600,
        guarded_run_id=None,
    )
    with pytest.raises(stop.StopRefused):
        worker.run(args)
    assert calls == []


@pytest.fixture
def fake_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(stop.sys, "platform", "linux")
    run_id = "2026-10-14T150000Z-aaaaaaaaaaaa"
    receipt = {
        "status": "running",
        "tier": "enrich",
        "phase": "enrich_apply",
        "source": "matthews",
        "apply": True,
        "database_target_sha256": "a" * 64,
        "batch": 2,
        "deadline_seconds": 600,
        "stop_owner": {"pid": 101},
    }
    monkeypatch.setattr(stop.os, "getpid", lambda: 202)
    monkeypatch.setattr(stop.os, "getppid", lambda: 101)
    monkeypatch.setattr(stop.os, "getpgid", lambda _: 202)
    monkeypatch.setattr(stop.os, "getsid", lambda _: 202)
    monkeypatch.setattr(stop.os, "getpgrp", lambda: 202)
    monkeypatch.setattr(stop, "_read_receipt", lambda *_: copy.deepcopy(receipt))
    monkeypatch.setattr(stop, "_validate_owner", lambda *_: None)
    monkeypatch.setattr(
        stop.checkpoint, "canonical_shared_lock_dir", lambda: tmp_path / "lock"
    )
    return run_id, receipt


def test_guarded_worker_admission_uses_supported_running_owner(fake_guard):
    run_id, receipt = fake_guard
    assert stop.require_guarded_matthews_parent(run_id, "a" * 64, 2) == receipt


@pytest.mark.parametrize(
    "key,value",
    [
        ("status", "complete"),
        ("phase", "preflight"),
        ("source", "cbre"),
        ("apply", False),
        ("tier", "monitor"),
        ("database_target_sha256", "b" * 64),
        ("batch", 3),
        ("deadline_seconds", 601),
        ("deadline_seconds", True),
    ],
)
def test_guarded_owner_context_mismatch_refused(fake_guard, key, value):
    run_id, receipt = fake_guard
    receipt[key] = value
    with pytest.raises(stop.StopRefused):
        stop.require_guarded_matthews_parent(run_id, "a" * 64, 2)


def test_guarded_owner_changed_on_second_read_refused(fake_guard, monkeypatch):
    run_id, receipt = fake_guard
    snapshots = iter([receipt, {**receipt, "status": "complete"}])
    monkeypatch.setattr(stop, "_read_receipt", lambda *_: next(snapshots))
    with pytest.raises(stop.StopRefused):
        stop.require_guarded_matthews_parent(run_id, "a" * 64, 2)


def test_grandchild_requires_actual_worker_birth_and_parent_chain(
    fake_guard, monkeypatch
):
    run_id, receipt = fake_guard
    identity = {"pid": 202, "start_ticks": 50}
    monkeypatch.setattr(stop.os, "getppid", lambda: 202)
    monkeypatch.setattr(stop, "_process_identity", lambda _: identity)
    monkeypatch.setattr(stop, "_proc_text", lambda _: "202 (worker) S 101 202 202")
    assert (
        stop.require_guarded_matthews_parent(
            run_id, "a" * 64, 2, worker_identity=identity
        )
        == receipt
    )
    with pytest.raises(stop.StopRefused):
        stop.require_guarded_matthews_parent(
            run_id, "a" * 64, 2, worker_identity={**identity, "start_ticks": 49}
        )
    monkeypatch.setattr(stop, "_proc_text", lambda _: "202 (worker) S 1 202 202")
    with pytest.raises(stop.StopRefused):
        stop.require_guarded_matthews_parent(
            run_id, "a" * 64, 2, worker_identity=identity
        )


def test_grandchild_separate_group_cannot_inherit_admission(fake_guard, monkeypatch):
    run_id, _ = fake_guard
    identity = {"pid": 202, "start_ticks": 50}
    monkeypatch.setattr(stop.os, "getppid", lambda: 202)
    monkeypatch.setattr(stop, "_process_identity", lambda _: identity)
    monkeypatch.setattr(stop, "_proc_text", lambda _: "202 (worker) S 101 202 202")
    monkeypatch.setattr(stop.os, "getpgrp", lambda: 303)
    with pytest.raises(stop.StopRefused):
        stop.require_guarded_matthews_parent(
            run_id, "a" * 64, 2, worker_identity=identity
        )


def test_partially_filled_batch_keeps_requested_worker_and_actual_ingest_bounds(
    fake_guard, monkeypatch
):
    run_id, receipt = fake_guard
    receipt["batch"] = 50
    assert stop.require_guarded_matthews_parent(run_id, "a" * 64, 50) == receipt
    identity = {"pid": 202, "start_ticks": 50}
    monkeypatch.setattr(stop.os, "getppid", lambda: 202)
    monkeypatch.setattr(stop, "_process_identity", lambda _: identity)
    monkeypatch.setattr(stop, "_proc_text", lambda _: "202 (worker) S 101 202 202")
    assert (
        stop.require_guarded_matthews_parent(
            run_id, "a" * 64, 2, worker_identity=identity
        )
        == receipt
    )
    receipt["batch"] = 1
    with pytest.raises(stop.StopRefused):
        stop.require_guarded_matthews_parent(
            run_id, "a" * 64, 2, worker_identity=identity
        )


def test_targeted_sql_and_completion_keep_inventory_detail_clocks_distinct():
    claims, artifact = fixture_batch()
    rows = [
        ingest.to_row(row, {}, row["detailObservedAt"]) for row in artifact["listings"]
    ]
    targets = ingest.matthews_targeted_sql_context(artifact, claims)
    sql = ingest.build_sql(
        rows,
        [],
        artifact["runMeta"]["startedAt"],
        set(),
        matthews_targeted_targets=targets,
    )
    assert (
        sql.index("_present_source_locks")
        < sql.index("_present_listing_locks")
        < sql.index("_matthews_queue_locks")
        < sql.index("CREATE TEMP TABLE _up")
    )
    assert "last_seen=e.inventory_at,last_enumerated_at=e.inventory_at" in sql
    assert "'full_ingest_present'" not in sql
    assert "l.scraped_at<=e.detail_at" in sql
    complete = worker.build_targeted_queue_sql(
        claims["items"], "complete", artifact=artifact, claims=claims
    )
    assert (
        complete.index("_completion_source_locks")
        < complete.index("_completion_listing_locks")
        < complete.index("_targeted_locked")
    )
    assert (
        "completion readback changed" in complete and "scraped_at=detail_at" in complete
    )


def test_targeted_ingest_cli_wrong_worker_refuses_before_database(
    monkeypatch, tmp_path
):
    import sys

    claims, artifact = fixture_batch()
    claims["matthewsGuardedWorker"] = {
        "runId": "2026-10-14T150000Z-aaaaaaaaaaaa",
        "workerIdentity": {"pid": 1},
    }
    cp, ap = tmp_path / "claims.json", tmp_path / "artifact.json"
    cp.write_text(json.dumps(claims))
    cp.chmod(0o600)
    ap.write_text(json.dumps(artifact))
    tmp_path.chmod(0o700)
    calls = []
    monkeypatch.setattr(ingest, "load_db_url", lambda _: calls.append("DB"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(ap),
            "--matthews-targeted-claims",
            str(cp),
            "--expected-db-target-sha256",
            "a" * 64,
        ],
    )
    with pytest.raises(stop.StopRefused):
        ingest.main()
    assert calls == []


REAL_GUARDED_CHAIN = r'''
import ast,json,sys
from pathlib import Path
import cre_checkpoint_refresh as checkpoint
import cre_guarded_tier as tier
root=Path(sys.argv[1]); path=Path(tier.__file__)
checkpoint.canonical_shared_lock_dir=lambda:root/'private.lock'
checkpoint.database_target_fingerprint=lambda _: {'value':'a'*64}
tree=ast.parse(path.read_text()); ns={'__name__':'__main__','__file__':str(path)}
exec(compile(ast.Module(body=tree.body[:-1],type_ignores=[]),str(path),'exec'),ns)
original=ns['run_guarded_tier']
grandchild="""
import json,os,sys,time
from pathlib import Path
import cre_stop_guarded_tier as stop
root=Path(sys.argv[1]); stop.OUT_ROOT=root/'runs'
stop.checkpoint.canonical_shared_lock_dir=lambda:root/'private.lock'
identity=json.loads(sys.argv[3])
stop.require_guarded_matthews_parent(sys.argv[2],'a'*64,2,worker_identity=identity)
(root/'chain-admitted.json').write_text(json.dumps({'worker':os.getppid(),'child':os.getpid(),'admitted':True}))
time.sleep(60)
"""
worker="""
import json,os,subprocess,sys
from pathlib import Path
import cre_stop_guarded_tier as stop
root=Path(sys.argv[1]); stop.OUT_ROOT=root/'runs'
stop.checkpoint.canonical_shared_lock_dir=lambda:root/'private.lock'
stop.require_guarded_matthews_parent(sys.argv[2],'a'*64,50)
subprocess.run([sys.executable,'-c',sys.argv[3],str(root),sys.argv[2],json.dumps(stop._process_identity(os.getpid()))],check=True)
"""
def run(args,**kw):
 def runner(argv,log,*,env):
  run_id=argv[argv.index('--guarded-run-id')+1]
  return checkpoint.run_command([sys.executable,'-c',worker,str(root),run_id,grandchild],log,env=env)
 return original(args,_lock_path=root/'private.lock',_out_root=root/'runs',_runner=runner,
                 _cpu_sampler=lambda:10.,_memory_sampler=lambda:1,
                 _verify_checkout=lambda *_:None,_deadline_seconds=1,**kw)
ns['run_guarded_tier']=run
sys.argv=[str(path),'enrich','--source','matthews','--expected-sha','a'*40,
          '--apply','--batch','50','--deadline-seconds','30']
exec(compile(ast.Module(body=[tree.body[-1]],type_ignores=[]),str(path),'exec'),ns)
'''


@pytest.mark.skipif(
    __import__("sys").platform != "linux", reason="Linux fixed parent-chain proof"
)
def test_real_cli_owner_worker_ingest_chain_and_outer_deadline_settle(tmp_path):
    import os
    import subprocess
    import sys

    owner = subprocess.Popen(
        [sys.executable, "-c", REAL_GUARDED_CHAIN, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = owner.communicate(timeout=40)
        assert owner.returncode == 124, (stdout, stderr)
        evidence = json.loads((tmp_path / "chain-admitted.json").read_text())
        assert evidence["admitted"] is True
        (receipt_path,) = (tmp_path / "runs").glob("*/receipt.json")
        receipt = json.loads(receipt_path.read_text())
        assert receipt["status"] == "deadline_interrupted"
        assert receipt["guards_stopped"] is True
        assert receipt["local_settlement"]["state"] == "confirmed"
        assert receipt["recovery_clear_eligible"] is False
        assert all(
            group["leader_reaped"] and group["group_gone"]
            for group in receipt["local_settlement"]["groups"]
        )
        with pytest.raises(ProcessLookupError):
            os.kill(evidence["worker"], 0)
        with pytest.raises(ProcessLookupError):
            os.kill(evidence["child"], 0)
    finally:
        if owner.poll() is None:
            owner.terminate()  # This test owns the exact unreaped synthetic parent.
            owner.communicate(timeout=45)


@pytest.mark.skipif(
    __import__("sys").platform != "linux",
    reason="New guarded targeted lane is Linux-only",
)
@pytest.mark.parametrize("inventory", ["both", "one", "none"])
def test_real_producer_worker_cli_sql_and_witnessed_omissions(
    monkeypatch, tmp_path, inventory
):
    import subprocess
    import sys
    from pathlib import Path

    claims, _ = fixture_batch()
    run_id = "2026-10-14T150000Z-aaaaaaaaaaaa"
    private = tmp_path / run_id
    private.mkdir(mode=0o700)
    monkeypatch.setattr(worker, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(stop, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(stop, "require_guarded_matthews_parent", lambda *a, **kw: {})
    monkeypatch.setattr(worker, "load_db_url", lambda _: ("synthetic", "private"))
    monkeypatch.setattr(worker, "assert_expected_database_target", lambda *a: None)
    items = claims["items"]
    urls = (
        [i["url"] for i in items]
        if inventory == "both"
        else [items[0]["url"]]
        if inventory == "one"
        else ["https://www.matthews.com/properties/not-selected"]
    )
    expected = 2 if inventory == "both" else 1 if inventory == "one" else 0
    xml = (
        "<urlset>"
        + "".join("<url><loc>" + u + "</loc></url>" for u in urls)
        + "</urlset>"
    )
    preload = private / "fixture.mjs"
    preload.write_text(
        """
import { MATTHEWS_FETCH_TIMEOUT_MS } from "SOURCE_MODULE";
const original = globalThis.setTimeout;
globalThis.setTimeout = (callback, delay, ...args) => original(callback, delay===MATTHEWS_FETCH_TIMEOUT_MS?2:0, ...args);
globalThis.fetch = async input => {
 const url=String(input);
 if(url==='https://www.matthews.com/sitemap.xml')return new Response(XML);
 if(!URLS.includes(url))throw new Error('Unexpected mocked transport');
 return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><h1 id="propertyTitle">Synthetic Property</h1><div id="propertyAddress">123 Main St, Tulsa, OK 74103</div></body></html>');
};
""".replace("XML", json.dumps(xml))
        .replace("URLS", json.dumps(urls))
        .replace("SOURCE_MODULE", (Path(worker.HERE) / "sources/matthews.ts").as_uri())
    )
    returned = [
        (
            i["claimId"],
            "matthews",
            i["externalId"],
            i["url"],
            "changed",
            "0",
            i["claimedAt"],
        )
        for i in items
    ]
    monkeypatch.setattr(
        worker,
        "_psql_query",
        lambda db, sql: returned if "RETURNING" in sql else [(str(expected),)],
    )
    mutations, ingest_calls, child_envs = [], [], []
    monkeypatch.setattr(worker, "_psql_exec", lambda db, sql: mutations.append(sql))
    real_run = subprocess.run

    def native(argv, **kwargs):
        if "collect.ts" in argv:
            child_envs.append(kwargs["env"])
            result = real_run(
                [
                    "/opt/node-v24.21.0-linux-x64/bin/node",
                    "--import",
                    "tsx",
                    "--import",
                    str(preload),
                    "collect.ts",
                    *argv[argv.index("collect.ts") + 1 :],
                ],
                cwd=worker.HERE,
                env=kwargs["env"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            return result
        assert str(argv[1]).endswith("cre_ingest.py")
        ingest_calls.append(argv)
        previous = sys.argv
        try:
            sys.argv = [
                "cre_ingest.py",
                *argv[2:],
                "--dry-run",
                "--keep-artifacts",
                str(private / "sql"),
            ]
            ingest.main()  # Real validator/to_row/build_sql; only native/DB IO mocked.
        finally:
            sys.argv = previous
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", native)
    monkeypatch.setenv("DATABASE_URL", "credential-canary")
    monkeypatch.setenv("PGPASSWORD", "credential-canary")
    args = SimpleNamespace(
        batch=50,
        source="matthews",
        env_file=None,
        dry_run=False,
        expected_db_target_sha256="a" * 64,
        matthews_targeted_refresh=True,
        work_seconds=600,
        guarded_run_id=run_id,
    )
    assert worker.run(args) == 0
    assert len(ingest_calls) == bool(expected)
    assert all(
        "DATABASE_URL" not in env and "PGPASSWORD" not in env for env in child_envs
    )
    (output,) = private.glob("enriched-*.json")
    artifact = json.loads(output.read_text())
    assert artifact["runMeta"]["mode"] == "enrich"
    assert artifact["sources"][0]["requested"] == 2
    assert artifact["sources"][0]["listingsCollected"] == expected
    assert len(artifact["listings"]) == expected
    if expected:
        sql = (private / "sql" / "ingest.sql").read_text()
        assert "targeted Matthews identity, eligibility or claim tenure changed" in sql
        assert "full_ingest_present" not in sql
        assert "DELETE FROM credeals.cre_enrichment_queue" in mutations[0]
    if expected < 2:
        assert "attempts=attempts+1" in mutations[-1]
    if expected == 0:
        assert not (private / "sql").exists()
        assert all(
            "DELETE FROM credeals.cre_enrichment_queue" not in m for m in mutations
        )
