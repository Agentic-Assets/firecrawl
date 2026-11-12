"""Demonstrate legacy claim races in disposable network-isolated PostgreSQL.

Opt-in only, in an isolated no-hardlinks checkout. The first scenario executes
an actual listing write and commit, then simulates client response loss; it is
not a live cre_ingest subprocess. No production connection is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cre_enrich import build_claim_sql, build_complete_sql, build_release_sql
from enrich_atomic_fixture_sql import build_sql

IMAGE = "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"


def run(args, *, input=None, timeout=30, env=None):
    return subprocess.run(args, input=input, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, env=env).stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.execute_disposable_fixture:
        parser.error("explicit --execute-disposable-fixture required")
    token = uuid.uuid4().hex
    name = "cre-enrich-atomic-fixture-" + token
    root = Path(tempfile.mkdtemp(prefix="cre-enrich-atomic-"))
    sock = root / "socket"
    sock.mkdir(mode=0o777)
    os.chmod(sock, 0o777)
    container = None
    owned = []
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}

    def sql(text):
        return run(["/usr/bin/psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                    "-h", str(sock), "-p", "5432", "-U", "postgres", "-d", "postgres"],
                   input=text, env=env)

    try:
        container = run(["docker", "run", "-d", "--pull=never", "--name", name,
                         "--label", "cre.fixture=" + token, "--network", "none",
                         "--memory", "256m", "--cpus", "0.5", "--pids-limit", "48",
                         "--tmpfs", "/var/lib/postgresql/data:rw,size=96m",
                         "--mount", "type=bind,src=" + str(sock) + ",dst=/fixture-socket",
                         "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE,
                         "-c", "unix_socket_directories=/var/run/postgresql,/fixture-socket",
                         "-c", "listen_addresses="])
        info = json.loads(run(["docker", "inspect", container]))[0]
        assert info["Id"] == container and info["Config"]["Image"] == IMAGE
        assert info["Config"]["Labels"]["cre.fixture"] == token
        assert info["HostConfig"]["NetworkMode"] == "none" and not info["HostConfig"]["PortBindings"]
        assert len(info["Mounts"]) == 1 and info["Mounts"][0]["Source"] == str(sock)
        assert root.resolve() == root and root.stat().st_uid == os.getuid()
        until = time.monotonic() + 25
        while True:
            try:
                if ("PostgreSQL init process complete" in run(["docker", "logs", container])
                        and sql("SELECT inet_server_addr() IS NULL;") == "t"):
                    break
            except subprocess.CalledProcessError:
                pass
            if time.monotonic() >= until:
                raise RuntimeError("disposable database unavailable")
            time.sleep(.1)
        sql("""CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_enrichment_queue (
 id uuid PRIMARY KEY, brokerage_id uuid, source_key text, external_id text, url text,
 reason text, priority integer DEFAULT 1, enqueued_at timestamptz DEFAULT now(),
 claimed_at timestamptz, done_at timestamptz, attempts integer DEFAULT 0,last_error text);
CREATE TABLE credeals.cre_listings (external_id text PRIMARY KEY, title text);
INSERT INTO credeals.cre_listings VALUES ('a','before');
INSERT INTO credeals.cre_enrichment_queue
(id,source_key,external_id,url,reason,claimed_at)
VALUES ('11111111-1111-1111-1111-111111111111','cbre','a','https://example.invalid/a','changed',now());""")
        # The server really commits; the subsequent nonzero-client outcome is
        # simulated because the old worker only receives a subprocess exit code.
        sql("BEGIN; UPDATE credeals.cre_listings SET title='committed' WHERE external_id='a'; COMMIT;")
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='a';") == "committed"
        sql(build_release_sql(["11111111-1111-1111-1111-111111111111"], last_error="ingest rc=1"))
        assert sql("SELECT claimed_at IS NULL FROM credeals.cre_enrichment_queue WHERE external_id='a';") == "t"
        print("CONFIRMED baseline: committed synthetic listing + lost response releases claim")
        # New owner B successfully reclaims the hour-old row. Legacy owner A's
        # ID-only completion then deletes B's claim without comparing tenure.
        sql("UPDATE credeals.cre_enrichment_queue SET claimed_at=now()-interval '2 hours' WHERE external_id='a';")
        sql(build_claim_sql(1, source="cbre"))
        assert sql("SELECT claimed_at > now()-interval '1 minute' FROM credeals.cre_enrichment_queue WHERE external_id='a';") == "t"
        sql(build_complete_sql(["11111111-1111-1111-1111-111111111111"]))
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id='a';") == "0"
        print("CONFIRMED baseline: stale owner deletes successor claim")
        sql("""ALTER TABLE credeals.cre_listings ADD COLUMN brokerage_id uuid,
 ADD COLUMN source_url text, ADD COLUMN status text, ADD COLUMN deleted_at timestamptz,
 ADD COLUMN write_count integer NOT NULL DEFAULT 0;
DELETE FROM credeals.cre_listings;
CREATE UNIQUE INDEX fixture_listing_key ON credeals.cre_listings(brokerage_id,external_id);
CREATE UNIQUE INDEX fixture_queue_identity ON credeals.cre_enrichment_queue
 (brokerage_id,external_id,reason) NULLS NOT DISTINCT;
CREATE TABLE credeals.fixture_source_map(brokerage_id uuid,source_key text);
INSERT INTO credeals.fixture_source_map VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre');
CREATE TABLE credeals.fixture_atomic_receipts(attempt_id uuid PRIMARY KEY,request_hash text,decisions jsonb);
CREATE TABLE credeals.cre_source_index(brokerage_id uuid, external_id text,source_key text,
 url text, observation_present boolean,soft_deleted boolean, PRIMARY KEY(brokerage_id,external_id));
INSERT INTO credeals.cre_listings(brokerage_id,external_id,title,source_url,status)
 SELECT 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,x,'before',
   'https://example.invalid/'||x,'active' FROM unnest(ARRAY['a','b','c','d','e','f']) x;
INSERT INTO credeals.cre_source_index SELECT brokerage_id,external_id,'cbre',source_url,true,false
 FROM credeals.cre_listings;
UPDATE credeals.cre_listings SET source_url='https://example.invalid/new-b' WHERE external_id='b';
INSERT INTO credeals.cre_enrichment_queue
 (id,brokerage_id,source_key,external_id,url,reason,claimed_at,enqueued_at,attempts)
 SELECT (CASE x WHEN 'a' THEN '11111111-1111-1111-1111-111111111111'
                WHEN 'b' THEN '22222222-2222-2222-2222-222222222222'
                WHEN 'c' THEN '33333333-3333-3333-3333-333333333333'
                ELSE '44444444-4444-4444-4444-444444444444' END)::uuid,
 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,'cbre',x,'https://example.invalid/'||x,
 'changed',now()-interval '10 seconds',now()-interval '1 day',0
 FROM unnest(ARRAY['a','b','c','d']) x;
INSERT INTO credeals.cre_enrichment_queue
 (id,brokerage_id,source_key,external_id,url,reason,claimed_at,enqueued_at,attempts)
 VALUES
 ('55555555-5555-5555-5555-555555555555','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','e','https://example.invalid/e','new',now()-interval '10 seconds',now()-interval '1 day',0),
 ('66666666-6666-6666-6666-666666666666','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','e','https://example.invalid/e','changed',now()-interval '10 seconds',now()-interval '1 day',0),
 ('77777777-7777-7777-7777-777777777777','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','f','https://example.invalid/f','changed',now()-interval '2 hours',now()-interval '1 day',0);
UPDATE credeals.cre_enrichment_queue SET claimed_at=claimed_at+interval '1 second' WHERE external_id='c';""")
        # This small synthetic provider-format parser independently rederives an
        # identity from a captured byte payload. It is NOT an actual source
        # contract and is never accepted by the runtime worker.
        def synthetic_witness(raw, digest, expected_url, expected_id):
            if hashlib.sha256(raw).hexdigest() != digest:
                return False
            try:
                document = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return False
            return (isinstance(document, dict) and type(document.get("status")) is int
                    and document["status"] == 200 and document.get("final_url") == expected_url
                    and document.get("native_id") == expected_id)

        payload = b'{"status":200,"final_url":"https://example.invalid/a","native_id":"a"}'
        before = hashlib.sha256(payload).hexdigest()
        witness_a = synthetic_witness(payload, before, "https://example.invalid/a", "a")
        assert witness_a
        assert not synthetic_witness(payload + b' ', before, "https://example.invalid/a", "a")
        assert not synthetic_witness(payload, before, "https://example.invalid/a", "other-id")
        assert not synthetic_witness(payload, before, "https://example.invalid/directory", "a")
        bad_response = b'{"status":403,"final_url":"https://example.invalid/a","native_id":"a"}'
        assert not synthetic_witness(bad_response, hashlib.sha256(bad_response).hexdigest(),
                                     "https://example.invalid/a", "a")
        claims = []
        times = sql("SELECT claimed_at::text||'|'||enqueued_at::text FROM credeals.cre_enrichment_queue WHERE external_id='a';").split('|')
        e_times = sql("SELECT claimed_at::text||'|'||enqueued_at::text FROM credeals.cre_enrichment_queue WHERE id='55555555-5555-5555-5555-555555555555';").split('|')
        expired = sql("SELECT claimed_at::text||'|'||enqueued_at::text FROM credeals.cre_enrichment_queue WHERE external_id='f';").split('|')
        for letter, digit, reason, clock in [
                ('a','1','changed',times), ('b','2','changed',times), ('c','3','changed',times),
                ('d','4','changed',times), ('e','5','new',e_times), ('e','6','changed',e_times),
                ('f','7','changed',expired)]:
            claims.append({"claim_id": digit * 8 + "-" + digit * 4 + "-" + digit * 4 + "-" + digit * 4 + "-" + digit * 12,
                "brokerage_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "source_key": "cbre",
                "external_id": letter, "url": "https://example.invalid/" + letter, "reason": reason,
                "claimed_at": clock[0], "enqueued_at": clock[1], "attempts": 0,
                "detail_verified": witness_a if letter == "a" else letter != "d",
                "observed_native_id": letter, "title": "after-ATTEMPT-HASH" if letter == "a" else "after-" + digit})
        attempt = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        expired_before = sql("SELECT to_jsonb(q)::text FROM credeals.cre_enrichment_queue q WHERE external_id='f';")
        prototype = build_sql(claims, attempt)
        # No result printed before commit is accepted as proof. The fixture
        # discards this SQL client's stdout to simulate a lost acknowledgement.
        sql(prototype)
        assert hashlib.sha256(payload).hexdigest() == before
        receipt = sql("SELECT value||':'||count(*) FROM credeals.fixture_atomic_receipts r, jsonb_each_text(r.decisions) d WHERE attempt_id='" + attempt + "' GROUP BY value ORDER BY value;")
        assert receipt.splitlines() == ["accepted:1", "held:4", "superseded:1", "unverified:1"], receipt
        # Independent fresh connection: exact identities and immutable input hash,
        # not just the same aggregate counts.
        saved = sql("SELECT decisions::text || '|' || request_hash FROM credeals.fixture_atomic_receipts WHERE attempt_id='"+attempt+"';")
        decisions_raw, saved_hash = saved.rsplit('|', 1)
        expected_hash = hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert saved_hash == expected_hash
        outcomes = {row["claim_id"]: ("accepted" if row["external_id"] == "a" else
                    "superseded" if row["external_id"] == "c" else "unverified" if row["external_id"] == "f" else "held") for row in claims}
        assert json.loads(decisions_raw) == outcomes
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id IN ('b','d','e') AND claimed_at>now()-interval '1 minute' AND attempts=0;") == "4"
        assert sql("SELECT string_agg(external_id||':'||title||':'||write_count,',' ORDER BY external_id) FROM credeals.cre_listings;") == "a:after-ATTEMPT-HASH:1,b:before:0,c:before:0,d:before:0,e:before:0,f:before:0"
        assert sql("SELECT string_agg(external_id,',' ORDER BY external_id) FROM credeals.cre_enrichment_queue;") == "b,c,d,e,e,f"
        try:
            sql(prototype)
            raise AssertionError("a committed attempt must refuse replay")
        except subprocess.CalledProcessError:
            pass
        assert sql("SELECT write_count FROM credeals.cre_listings WHERE external_id='a';") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id='e';") == "2"
        assert sql("SELECT claimed_at > now()-interval '1 minute' FROM credeals.cre_enrichment_queue WHERE external_id='c';") == "t"
        print("CONFIRMED synthetic prototype: one committed, four held, expired claim untouched, one successor untouched; receipt replay refused")
        # Held oldest claims are cooled without retry consumption, so a new
        # eligible peer is next even when it has the same priority.
        sql("""INSERT INTO credeals.cre_enrichment_queue
(id,brokerage_id,source_key,external_id,url,reason,claimed_at,enqueued_at,attempts)
VALUES ('99999999-9999-9999-9999-999999999999','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','g','https://example.invalid/g','changed',NULL,now(),0);""")
        assert sql("SELECT to_jsonb(q)::text FROM credeals.cre_enrichment_queue q WHERE external_id='f';") == expired_before
        # A stale worker cannot renew an expired claim; a new owner may reclaim
        # it, after which the unclaimed equal-priority healthy peer proceeds.
        sql(build_claim_sql(1, source="cbre"))
        assert sql("SELECT claimed_at > now()-interval '1 minute' AND attempts=0 FROM credeals.cre_enrichment_queue WHERE external_id='f';") == "t"
        assert sql("SELECT claimed_at IS NULL FROM credeals.cre_enrichment_queue WHERE external_id='g';") == "t"
        sql(build_claim_sql(1, source="cbre"))
        assert sql("SELECT claimed_at IS NOT NULL AND attempts=0 FROM credeals.cre_enrichment_queue WHERE external_id='g';") == "t"
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id IN ('b','d','e','f') AND attempts=0;") == "5"
        # A second, unrequested queue reason for the same parent is still a
        # conflict; only the requested exact claim may be cooled.
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,title,source_url,status)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','z','before','https://example.invalid/z','active');
INSERT INTO credeals.cre_source_index VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','z','cbre','https://example.invalid/z',true,false);
INSERT INTO credeals.cre_enrichment_queue(id,brokerage_id,source_key,external_id,url,reason,claimed_at,enqueued_at,attempts)
VALUES ('aaaaaaaa-1111-1111-1111-111111111111','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','z','https://example.invalid/z','new',now()-interval '10 seconds',now()-interval '1 day',0),
('aaaaaaaa-2222-2222-2222-222222222222','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','z','https://example.invalid/z','changed',NULL,now()-interval '1 day',0);""")
        ztimes=sql("SELECT claimed_at::text||'|'||enqueued_at::text FROM credeals.cre_enrichment_queue WHERE reason='new' AND external_id='z';").split('|')
        zclaim={"claim_id":"aaaaaaaa-1111-1111-1111-111111111111","brokerage_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","source_key":"cbre","external_id":"z","url":"https://example.invalid/z","reason":"new","claimed_at":ztimes[0],"enqueued_at":ztimes[1],"attempts":0,"detail_verified":True,"observed_native_id":"z","title":"after-z"}
        sql(build_sql([zclaim],"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='z';") == "before"
        assert sql("SELECT claimed_at IS NULL AND attempts=0 FROM credeals.cre_enrichment_queue WHERE reason='changed' AND external_id='z';") == "t"
        assert sql("SELECT decisions->>'aaaaaaaa-1111-1111-1111-111111111111' FROM credeals.fixture_atomic_receipts WHERE attempt_id='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';") == "held"
        print("CONFIRMED held cooldown admits next peer; unrequested competing reason holds")
        # Rollback after the listing UPDATE but before receipt publication must
        # leave the listing/claim intact and must not produce a receipt.
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,title,source_url,status)
 VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','i','before','https://example.invalid/i','active');
 INSERT INTO credeals.cre_source_index VALUES
 ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','i','cbre','https://example.invalid/i',true,false);
 INSERT INTO credeals.cre_enrichment_queue(id,brokerage_id,source_key,external_id,url,reason,claimed_at,enqueued_at,attempts)
 VALUES ('88888888-8888-8888-8888-888888888888','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','i','https://example.invalid/i','changed',now()-interval '10 seconds',now()-interval '1 day',0);""")
        times_i = sql("SELECT claimed_at::text||'|'||enqueued_at::text FROM credeals.cre_enrichment_queue WHERE external_id='i';").split('|')
        claim_i = {"claim_id":"88888888-8888-8888-8888-888888888888", "brokerage_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                   "source_key":"cbre", "external_id":"i", "url":"https://example.invalid/i", "reason":"changed",
                   "claimed_at":times_i[0], "enqueued_at":times_i[1], "attempts":0,
                   "detail_verified":True, "observed_native_id":"i", "title":"after-i"}
        rollback_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        rollback_sql = build_sql([claim_i], rollback_id).replace(
            "INSERT INTO credeals.fixture_atomic_receipts", "SELECT 1/0;\nINSERT INTO credeals.fixture_atomic_receipts")
        try:
            sql(rollback_sql)
            raise AssertionError("injected failure must abort")
        except subprocess.CalledProcessError:
            pass
        assert sql("SELECT title||':'||write_count FROM credeals.cre_listings WHERE external_id='i';") == "before:0"
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id='i';") == "1"
        assert sql("SELECT count(*) FROM credeals.fixture_atomic_receipts WHERE attempt_id='"+rollback_id+"';") == "0"
        # Reusing an attempt ID for changed input must not modify the committed
        # outcome or create a new write.
        altered = [dict(row) for row in claims]
        altered[0]["title"] = "forged-replay"
        try:
            sql(build_sql(altered, attempt))
            raise AssertionError("attempt identity reuse must fail")
        except subprocess.CalledProcessError:
            pass
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='a';") == "after-ATTEMPT-HASH"
        # A trigger that silently suppresses a write must make the RETURNING
        # identity check abort rather than mint an accepted receipt.
        sql("""CREATE FUNCTION credeals.fixture_suppress() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF OLD.external_id='i' THEN RETURN NULL; END IF; RETURN NEW; END $$;
CREATE TRIGGER fixture_suppress BEFORE UPDATE ON credeals.cre_listings
 FOR EACH ROW EXECUTE FUNCTION credeals.fixture_suppress();""")
        suppressed_id = "dddddddd-dddd-dddd-dddd-ddddddddddde"
        try:
            sql(build_sql([claim_i], suppressed_id))
            raise AssertionError("suppressed listing update must fail exact readback")
        except subprocess.CalledProcessError:
            pass
        assert sql("SELECT title||':'||write_count FROM credeals.cre_listings WHERE external_id='i';") == "before:0"
        assert sql("SELECT count(*) FROM credeals.fixture_atomic_receipts WHERE attempt_id='"+suppressed_id+"';") == "0"
        sql("DROP TRIGGER fixture_suppress ON credeals.cre_listings; DROP FUNCTION credeals.fixture_suppress();")
        print("CONFIRMED synthetic rollback and suppressed write produce no receipt")
        # Explicitly demonstrate the unresolved phantom: an external writer
        # ignores both advisory locks and inserts a same-URL owner after the
        # held/accepted snapshot. Never promote this prototype on this result.
        phantom_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        slow = build_sql([claim_i], phantom_id).replace(
            "CREATE TEMP TABLE applied", "SELECT pg_sleep(3);\nCREATE TEMP TABLE applied")
        app = "cre-fixture-phantom-" + token
        child_env = dict(env, PGAPPNAME=app)
        proc = subprocess.Popen(["/usr/bin/psql","-X","-qAt","-v","ON_ERROR_STOP=1",
             "-h",str(sock),"-p","5432","-U","postgres","-d","postgres"],
             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
             text=True, env=child_env, start_new_session=True)
        owned.append(proc)
        proc.stdin.write(slow)
        proc.stdin.close()
        proc.stdin = None
        until = time.monotonic()+10
        while sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='"+app+"' AND wait_event='PgSleep';") != "1":
            if proc.poll() is not None or time.monotonic()>until:
                raise RuntimeError("prototype did not reach phantom race barrier")
            time.sleep(.05)
        sql("INSERT INTO credeals.cre_listings(brokerage_id,external_id,title,source_url,status) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','intruder','before','https://example.invalid/i','active');")
        sql("INSERT INTO credeals.cre_enrichment_queue(id,brokerage_id,source_key,external_id,url,reason,claimed_at,enqueued_at,attempts) VALUES ('aaaaaaaa-3333-3333-3333-333333333333','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','cbre','i','https://example.invalid/i','new',NULL,now(),0);")
        stdout, stderr = proc.communicate(timeout=10)
        if proc.returncode != 0:
            raise RuntimeError("phantom fixture process failed: "+stderr)
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='i';") == "after-i"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE source_url='https://example.invalid/i';") == "2"
        assert sql("SELECT count(*) FROM credeals.cre_enrichment_queue WHERE external_id='i' AND reason='new';") == "1"
        assert sql("SELECT decisions->>'88888888-8888-8888-8888-888888888888' FROM credeals.fixture_atomic_receipts WHERE attempt_id='"+phantom_id+"';") == "accepted"
        print("CONFIRMED OPEN BLOCKER: uncooperative same-URL parent and competing-reason queue phantoms bypass predicates")
    finally:
        for proc in owned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if container:
            try:
                info = json.loads(run(["docker", "inspect", container]))[0]
                if info["Id"] != container or info["Config"]["Labels"].get("cre.fixture") != token:
                    raise RuntimeError("refusing fixture cleanup: identity changed")
                run(["docker", "rm", "-f", container])
            except subprocess.CalledProcessError as exc:
                raise RuntimeError("failed to verify or remove disposable database") from exc
        if root.resolve() != root or root.stat().st_uid != os.getuid():
            raise RuntimeError("refusing fixture directory cleanup")
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
