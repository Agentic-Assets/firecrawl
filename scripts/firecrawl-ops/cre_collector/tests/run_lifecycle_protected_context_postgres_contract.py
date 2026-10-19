"""AGENTIC-4360: generated lifecycle SQL against owned, network-none cached PG17.

Opt-in only; never accepts a database URL. Run from a genuine standalone Linux
QA clone with a private HOME and no database environment. No provider calls.
"""

import argparse
import decimal
import hashlib
import json
import os
import pwd
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COLLECTOR = HERE.parent
BROKERAGE = "40000000-0000-4000-8000-000000000001"
PARENTS = tuple(f"41000000-0000-4000-8000-{n:012d}" for n in range(1, 5))
NATIVE = tuple("006" + letter * 15 for letter in "ABCD")
URLS = tuple(
    "https://invest.jll.com/us/en/listings/retail/protected-fixture-" + letter
    for letter in "abab"
)
IDENTITIES = tuple(
    (parent, BROKERAGE, "investor:" + native, url)
    for parent, native, url in zip(PARENTS, NATIVE, URLS)
)
TARGET = "0" * 64

# Finite fixture, not 000_run_all: no geography loads, views or unrelated features.
# Columns used by generated lifecycle DML and the finite scope are real column
# names/types; extra marker values let every protected family prove drift.
DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages(id uuid PRIMARY KEY,slug text UNIQUE NOT NULL,name text);
CREATE TABLE credeals.cre_listings(
 id uuid PRIMARY KEY, brokerage_id uuid NOT NULL REFERENCES credeals.cre_brokerages,
 external_id text NOT NULL,source_url text,canonical_url text,transaction_type text,
 status text,deleted_at timestamptz,updated_at timestamptz DEFAULT now(),title text,
 raw_data jsonb, UNIQUE(brokerage_id,external_id));
CREATE TABLE credeals.cre_source_index(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),brokerage_id uuid NOT NULL,
 external_id text NOT NULL,source_key text,url text,soft_deleted boolean,
 observation_present boolean,presence_generation bigint,presence_changed_at timestamptz,
 last_enumerated_at timestamptz,UNIQUE(brokerage_id,external_id));
CREATE TABLE credeals.cre_scrape_jobs(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),brokerage_id uuid NOT NULL,
 artifact_run_key text,status text,started_at timestamptz DEFAULT now(),
 completed_at timestamptz,notes text,UNIQUE(brokerage_id,artifact_run_key));
CREATE TABLE credeals.cre_source_baseline(source_key text PRIMARY KEY,brokerage_slug text,
 last_active_rows integer);
CREATE TABLE credeals.cre_listing_events(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),listing_id uuid NOT NULL,brokerage_id uuid,
 scrape_job_id uuid,event_type text,field text,old_value text,new_value text,
 source_value text,source_url text,presence_generation bigint,detected_at timestamptz,
 reconciliation_provenance text,evidence_observed_at timestamptz,
 evidence_time_semantics text,reconciliation_evidence_sha256 text,
 UNIQUE(listing_id,event_type,presence_generation));
"""
CHILDREN = (
    "cre_listing_contacts", "cre_listing_documents", "cre_listing_images",
    "cre_listing_media", "cre_listing_links", "cre_listing_om_facts",
    "cre_listing_price_history",
)
ARCHIVES = ("cre_listing_contacts_archive", "cre_listing_documents_archive")
for _table in CHILDREN:
    DDL += f"""
CREATE TABLE credeals.{_table}(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 listing_id uuid NOT NULL REFERENCES credeals.cre_listings(id),marker text,
 precise numeric);
"""
for _table in ARCHIVES:
    DDL += f"""
CREATE TABLE credeals.{_table}(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 source_listing_id uuid NOT NULL,marker text);
"""
DDL += """
CREATE FUNCTION credeals.fixture_fail_completion() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
 IF NEW.status='completed' AND OLD.status='running'
    AND current_setting('cre_fixture.fail_finalize',true)='on' THEN
  RAISE EXCEPTION 'fixture finalization failure after lifecycle DML';
 END IF;
 RETURN NEW;
END;
$fn$;
CREATE TRIGGER fixture_fail_completion BEFORE UPDATE ON credeals.cre_scrape_jobs
 FOR EACH ROW EXECUTE FUNCTION credeals.fixture_fail_completion();
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, **kwargs):
    return subprocess.run(
        argv, text=True, encoding="utf-8", capture_output=True,
        timeout=kwargs.pop("timeout", 45), check=kwargs.pop("check", False), **kwargs,
    )


def assert_qa_checkout():
    if sys.platform != "linux" or os.getuid() == 0:
        raise ValueError("non-root Linux standalone QA checkout required")
    root = Path(run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"], check=True).stdout.strip())
    if not (root / ".git").is_dir() or (root / ".env").exists():
        raise ValueError("own .git directory and no root .env required")
    if run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], check=True).stdout.strip():
        raise ValueError("clean tracked QA checkout required")
    common = Path(run(["git", "-C", str(root), "rev-parse", "--git-common-dir"], check=True).stdout.strip())
    if not common.is_absolute():
        common = root / common
    if common.resolve() != (root / ".git").resolve():
        raise ValueError("shared Git common directory refused")
    if (common / "objects/info/alternates").exists():
        raise ValueError("Git object alternates refused")
    if any(p.stat().st_nlink != 1 for p in (common / "objects").rglob("*") if p.is_file()):
        raise ValueError("hard-linked Git objects refused")
    home = Path(os.environ.get("HOME", "")).resolve()
    if home == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() or not home.is_dir():
        raise ValueError("existing private HOME required")
    if home.stat().st_mode & 0o077:
        raise ValueError("HOME must be private")
    if any(os.environ.get(k) for k in (
        "DATABASE_URL", "POSTGRES_URL", "SUPABASE_DB_URL", "PGHOST", "PGSERVICE",
        "PGDATABASE", "PGPASSWORD", "DOCKER_HOST",
    )):
        raise ValueError("database/remote Docker environment refused")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--cpuset-cpus", default="0,1")
    args = parser.parse_args()
    checkout = assert_qa_checkout()
    os.umask(0o077)
    args.proof_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    sys.path.insert(0, str(COLLECTOR))
    import cre_reconcile_listing_lifecycle as lifecycle
    from cre_lifecycle_protected_context import _projection_sql, load_context

    owner = uuid.uuid4().hex
    container = None
    checks, transcript = [], []
    active_holder = None
    observed = datetime.now(timezone.utc).isoformat()

    def sql(text, *, check=True):
        result = run([
            "docker", "exec", "--user", "postgres", "-i", container,
            "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", "postgres",
            "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-f", "-",
        ], input=text, timeout=130)
        transcript.append({"sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def payload():
        return sql("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL timezone='UTC';\n"
                   + _projection_sql(IDENTITIES) + "; ROLLBACK;").stdout.strip()

    def reset():
        sql("DROP SCHEMA IF EXISTS credeals CASCADE;" + DDL)
        lit = lifecycle.sql_lit
        sql(f"INSERT INTO credeals.cre_brokerages VALUES('{BROKERAGE}','jll','Fixture');")
        for parent, brokerage, external, url in IDENTITIES:
            sql("INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,canonical_url,"
                "transaction_type,status,title,raw_data) VALUES("
                + ",".join(lit(x) for x in (parent, brokerage, external, url, url))
                + ",'sale','active','Fixture',"
                + lit('{"wide":9007199254740993.0000000000000001,"false":false,"null":null,"empty":[],"quote":"O\'Brien $$ \\\\ raw"}')
                + "::jsonb);")
        sql("INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url,soft_deleted,"
            "observation_present,presence_generation) SELECT brokerage_id,external_id,'jll-investor',source_url,false,true,0 FROM credeals.cre_listings;")
        for table in CHILDREN:
            sql(f"INSERT INTO credeals.{table}(listing_id,marker,precise) SELECT id,'original',9007199254740993.0000000000000001 FROM credeals.cre_listings;")
        for table in ARCHIVES:
            sql(f"INSERT INTO credeals.{table}(source_listing_id,marker) SELECT id,'original' FROM credeals.cre_listings;")
        sql(f"INSERT INTO credeals.cre_scrape_jobs(brokerage_id,artifact_run_key,status,notes) VALUES('{BROKERAGE}','fixture:prior','completed','original');"
            "INSERT INTO credeals.cre_source_baseline VALUES('jll','jll',4),('jll-investor','jll',2);"
            f"INSERT INTO credeals.cre_listing_events(listing_id,brokerage_id,event_type,source_value) VALUES('{PARENTS[2]}','{BROKERAGE}','price_change','original');")

    artifact = {
        "runMeta": {"mode": "full", "transactions": ["sale", "lease"],
                    "maxItemsPerSource": None, "startedAt": observed, "finishedAt": observed},
        "brokers": [], "totalListings": 2,
        "sources": [{"sourceKey": "jll-investor", "transaction": tx, "supported": True,
                     "listingsCollected": 2 if tx == "sale" else 0, "truncated": False}
                    for tx in ("sale", "lease")],
        "listings": [{"sourceKey": "jll-investor", "transactionMode": "sale", "id": NATIVE[n],
                      "url": URLS[n], "canonicalUrl": URLS[n], "transactionType": "Sale", "name": "Fixture"}
                     for n in (2, 3)],
    }
    artifact_path = args.proof_dir / "source.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    evidence = {"rows": [{
        "listing_id": PARENTS[n], "brokerage_id": BROKERAGE, "external_id": "investor:" + NATIVE[n],
        "listing_deleted": False, "listing_status": "active", "index_soft_deleted": False,
        "observation_present": True, "presence_generation": 0,
        "source": {"source_key": "jll-investor", "state": "absent", "url": URLS[n],
                   "observed_at": observed, "evidence_path": str(artifact_path.resolve()),
                   "evidence_sha256": sha(artifact_path)},
    } for n in (0, 1)]}

    def prepare(name):
        before = payload()
        envelope = {"schema": "investor-four-parent-current-context-v1", "observed_at": datetime.now(timezone.utc).isoformat(),
                    "read_only": "on", "isolation": "repeatable read", "server_version_num": sql("SHOW server_version_num;").stdout.strip(),
                    "expected_rows": 4, "matched_rows": 4, "complete_exact_identity_scope": True,
                    "evidence_time_semantics": "retained_database_evidence_observed_at", "source_acceptance": False}
        # Keep raw PostgreSQL JSONB numeric text; only the fixed envelope is encoded.
        snapshot = args.proof_dir / f"{name}-context.json"
        snapshot.write_text(json.dumps(envelope)[:-1] + ',"payload":' + before + "}\n", encoding="utf-8")
        pointer = {"snapshot_path": str(snapshot.resolve()), "snapshot_sha256": sha(snapshot),
                   "database_target_sha256": TARGET}
        plan = lifecycle.build_plan({**evidence, "protected_context": pointer})
        assert len(plan["actions"]) == 2 and not plan["skipped"]
        context = load_context(plan["protected_context"], plan["actions"])
        generated = lifecycle.build_apply_sql(plan["actions"], plan["plan_hash"], "cayman", "AGENTIC-4360", protected_context=context)
        (args.proof_dir / f"{name}.sql").write_text(generated, encoding="utf-8")
        return before, plan, generated

    def refused(name, mutation):
        reset()
        _, _, generated = prepare(name)
        sql(mutation)
        before = payload()
        result = sql(generated, check=False)
        assert result.returncode != 0 and "protected" in result.stderr.lower(), (name, result.stderr)
        assert payload() == before, name + " changed rows after refusal"
        checks.append({"case": name, "rc": result.returncode, "exact_rollback": True})

    try:
        endpoint = json.loads(run(["docker", "context", "inspect"], check=True).stdout)[0]["Endpoints"]["docker"]["Host"]
        assert endpoint.startswith(("unix://", "npipe://")), "nonlocal Docker endpoint refused"
        image = json.loads(run(["docker", "image", "inspect", "postgres:17"], check=True).stdout)[0]["Id"]
        created = run(["docker", "create", "--pull", "never", "--name", "cre4360-protected-" + owner,
                       "--label", "cre4360.protected.owner=" + owner, "--network", "none", "--cpus", "1",
                       "--cpuset-cpus", args.cpuset_cpus, "--memory", "256m", "--memory-swap", "256m",
                       "--pids-limit", "128", "--tmpfs", "/var/lib/postgresql/data:rw,size=128m",
                       "--tmpfs", "/var/run/postgresql:rw,size=16m", "--env", "POSTGRES_HOST_AUTH_METHOD=trust", image], check=True)
        container = created.stdout.strip()
        run(["docker", "start", container], check=True)
        info = json.loads(run(["docker", "inspect", container], check=True).stdout)[0]
        assert info["Image"] == image and info["Config"]["Labels"]["cre4360.protected.owner"] == owner
        assert info["HostConfig"]["NetworkMode"] == "none" and not info["NetworkSettings"]["Ports"] and not info["Mounts"]
        assert info["HostConfig"]["Memory"] == info["HostConfig"]["MemorySwap"] == 256 * 1024**2
        assert info["HostConfig"]["NanoCpus"] == 1_000_000_000 and info["HostConfig"]["PidsLimit"] == 128
        assert info["HostConfig"]["CpusetCpus"] == args.cpuset_cpus
        (args.proof_dir / "fixture.json").write_text(json.dumps({"container": container, "image": image,
            "network": "none", "memory": info["HostConfig"]["Memory"], "cpuset": args.cpuset_cpus,
            "checkout": str(checkout)}, indent=2), encoding="utf-8")
        for _ in range(80):
            ready = run(["docker", "exec", "--user", "postgres", container, "pg_isready", "-h", "127.0.0.1"])
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("fixture PostgreSQL did not become ready")

        reset()
        before, plan, generated = prepare("exact")
        sql(generated)
        after = json.loads(payload(), parse_float=decimal.Decimal)
        old = json.loads(before, parse_float=decimal.Decimal)
        untouched = set(old["objects"]) - {"cre_listings", "cre_source_index", "cre_listing_events", "cre_scrape_jobs"}
        assert all(old["objects"][table] == after["objects"][table] for table in untouched)
        assert old["catalog"] == after["catalog"]
        for table in ("cre_listings", "cre_source_index"):
            for row in old["objects"][table]:
                if row["external_id"] in {"investor:" + NATIVE[2], "investor:" + NATIVE[3]}:
                    assert row in after["objects"][table]
                    continue
                current = next(r for r in after["objects"][table] if r["external_id"] == row["external_id"])
                changed = ({"status", "deleted_at", "updated_at"} if table == "cre_listings" else
                           {"soft_deleted", "observation_present", "presence_generation", "presence_changed_at", "last_enumerated_at"})
                assert {k: v for k, v in row.items() if k not in changed} == {
                    k: v for k, v in current.items() if k not in changed}
                if table == "cre_listings":
                    assert current["status"] == "inactive" and datetime.fromisoformat(current["deleted_at"]) == datetime.fromisoformat(observed)
                else:
                    assert current["soft_deleted"] is True and current["observation_present"] is False and current["presence_generation"] == 1
                    assert all(datetime.fromisoformat(current[k]) == datetime.fromisoformat(observed)
                               for k in ("presence_changed_at", "last_enumerated_at"))
        for table in ("cre_scrape_jobs", "cre_listing_events"):
            assert all(row in after["objects"][table] for row in old["objects"][table])
        new_jobs = [r for r in after["objects"]["cre_scrape_jobs"] if r not in old["objects"]["cre_scrape_jobs"]]
        assert len(new_jobs) == 1 and new_jobs[0]["status"] == "completed" and new_jobs[0]["completed_at"]
        assert new_jobs[0]["artifact_run_key"] == "reconcile:v3:" + plan["plan_hash"]
        assert plan["plan_hash"] in new_jobs[0]["notes"]
        new_events = [r for r in after["objects"]["cre_listing_events"] if r not in old["objects"]["cre_listing_events"]]
        assert len(new_events) == 2 and all(r["event_type"] == "disappeared" and r["scrape_job_id"] == new_jobs[0]["id"] for r in new_events)
        assert {r["listing_id"] for r in new_events} == set(PARENTS[:2])
        assert all(r["presence_generation"] == 1 and r["reconciliation_evidence_sha256"] == sha(artifact_path)
                   and datetime.fromisoformat(r["evidence_observed_at"]) == datetime.fromisoformat(observed) for r in new_events)
        checks.append({"case": "exact_decimal_and_atomic_finalization", "rc": 0, "completed_jobs": 1})

        mutations = {
            "cre_listings": f"UPDATE credeals.cre_listings SET title='changed' WHERE id='{PARENTS[2]}';",
            "cre_brokerages": "UPDATE credeals.cre_brokerages SET name='changed';",
            "cre_source_index": "UPDATE credeals.cre_source_index SET url=url||'-changed';",
            "cre_source_baseline": "UPDATE credeals.cre_source_baseline SET last_active_rows=99;",
            "cre_scrape_jobs": "UPDATE credeals.cre_scrape_jobs SET notes='changed';",
            "cre_listing_events": "UPDATE credeals.cre_listing_events SET source_value='changed';",
        }
        mutations.update({table: f"UPDATE credeals.{table} SET marker='changed';" for table in CHILDREN + ARCHIVES})
        for table, mutation in mutations.items():
            refused("drift_" + table, mutation)
        refused("child_phantom", f"INSERT INTO credeals.cre_listing_images(listing_id,marker) VALUES('{PARENTS[2]}','phantom');")
        refused("child_deleted", f"DELETE FROM credeals.cre_listing_documents WHERE listing_id='{PARENTS[2]}';")
        refused("decimal_last_digit", "UPDATE credeals.cre_listing_om_facts SET precise=9007199254740993.0000000000000002;")
        refused("catalog_column", "ALTER TABLE credeals.cre_listing_images ADD COLUMN fixture_extra text;")
        refused("catalog_index", "CREATE INDEX fixture_marker ON credeals.cre_listing_images(marker);")
        refused("catalog_rls", "ALTER TABLE credeals.cre_listing_images ENABLE ROW LEVEL SECURITY;")
        refused("catalog_trigger_function", "CREATE OR REPLACE FUNCTION credeals.fixture_fail_completion() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END;';")

        reset()
        _, _, generated = prepare("outside_scope")
        sql(f"INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,title) VALUES('41000000-0000-4000-8000-000000000005','{BROKERAGE}','outside','Outside');"
            "INSERT INTO credeals.cre_listing_images(listing_id,marker) VALUES('41000000-0000-4000-8000-000000000005','outside');")
        outside_before = sql("SELECT jsonb_build_object('parent',to_jsonb(l),'image',to_jsonb(i)) FROM credeals.cre_listings l JOIN credeals.cre_listing_images i ON i.listing_id=l.id WHERE l.external_id='outside';").stdout
        sql(generated)
        assert sql("SELECT jsonb_build_object('parent',to_jsonb(l),'image',to_jsonb(i)) FROM credeals.cre_listings l JOIN credeals.cre_listing_images i ON i.listing_id=l.id WHERE l.external_id='outside';").stdout == outside_before
        checks.append({"case": "outside_parent_child_scope_unchanged", "rc": 0})

        reset()
        before, _, generated = prepare("finalizer_rollback")
        result = sql("SET cre_fixture.fail_finalize='on';\n" + generated, check=False)
        assert result.returncode != 0 and "fixture finalization failure" in result.stderr
        assert payload() == before
        checks.append({"case": "finalizer_rollback", "rc": result.returncode, "exact_rollback": True})

        reset()
        before, _, generated = prepare("lock_timeout")
        holder_sql = "SET application_name='protected-holder-" + owner + "'; BEGIN; LOCK TABLE credeals.cre_listing_images IN ROW EXCLUSIVE MODE; SELECT pg_sleep(5); ROLLBACK;"
        active_holder = subprocess.Popen(["docker", "exec", "--user", "postgres", "-i", container,
            "psql", "-XqAt", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        active_holder.stdin.write(holder_sql)
        active_holder.stdin.close()
        for _ in range(50):
            held = sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='protected-holder-" + owner + "' AND wait_event='PgSleep';").stdout.strip()
            if held == "1":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("owned holder never acquired its relation lock")
        result = sql(generated, check=False)
        assert result.returncode != 0 and "lock timeout" in result.stderr.lower()
        active_holder.wait(timeout=10)
        assert active_holder.returncode == 0
        active_holder = None
        assert payload() == before
        checks.append({"case": "relation_lock_timeout", "rc": result.returncode, "exact_rollback": True})

        reset()
        ordinary = lifecycle.build_plan(evidence)
        sql(lifecycle.build_apply_sql(ordinary["actions"], ordinary["plan_hash"], "cayman", "AGENTIC-4360"))
        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE artifact_run_key='reconcile:v3:" + ordinary["plan_hash"] + "';").stdout.strip() == "running"
        sql(lifecycle.build_finalize_sql(ordinary["actions"], ordinary["plan_hash"], "cayman", "AGENTIC-4360"))
        assert sql("SELECT status FROM credeals.cre_scrape_jobs WHERE artifact_run_key='reconcile:v3:" + ordinary["plan_hash"] + "';").stdout.strip() == "completed"
        checks.append({"case": "ordinary_split_finalization_unchanged", "rc": 0})
        assert len(checks) == 27, "contract case matrix did not complete"
    finally:
        if active_holder is not None:
            try:
                active_holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Removal below is limited to the exact independently checked
                # fixture owner, and also settles an owned docker-exec holder.
                pass
        (args.proof_dir / "checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
        (args.proof_dir / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
        if container:
            info = json.loads(run(["docker", "inspect", container], check=True).stdout)[0]
            assert info["Config"]["Labels"]["cre4360.protected.owner"] == owner
            removed = run(["docker", "rm", "-f", container])
            absent = run(["docker", "inspect", container])
            (args.proof_dir / "cleanup.json").write_text(json.dumps({"container": container,
                "remove_rc": removed.returncode, "absent": absent.returncode != 0}), encoding="utf-8")
            assert removed.returncode == 0 and absent.returncode != 0
            if active_holder is not None:
                active_holder.wait(timeout=10)
    print(json.dumps({"cases": len(checks), "verdict": "PASS"}))


if __name__ == "__main__":
    main()
