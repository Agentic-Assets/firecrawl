"""Owned Unix-socket PG17 fixture only; never reads application environment."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
import time

os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cre_ingest as ingest  # noqa: E402 - explicit standalone collector root

out = Path(sys.argv[1]).resolve()
out.mkdir(mode=0o700)
cluster = Path(tempfile.mkdtemp(prefix="daum-subset-owned-pg-"))
sock = cluster / "socket"
sock.mkdir(mode=0o700)
bin = Path("/usr/lib/postgresql/16/bin")
env = {"PATH": str(bin) + ":/usr/bin:/bin", "HOME": str(cluster), "LANG": "C.UTF-8"}
checks, transcript = [], []
started = False
container = None
owner = uuid.uuid4().hex
label = "daum-subset.namespace-owner"
image = None


def cmd(argv, **kwargs):
    return subprocess.run(
        [str(v) for v in argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=kwargs.pop("timeout", 40),
        **kwargs,
    )


def sql(text, db="postgres", check=True):
    p = cmd(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-qAt",
            "-U",
            "postgres",
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        input="SET statement_timeout='20s'; SET lock_timeout='2s';\n" + text,
    )
    transcript.append(
        {
            "database": db,
            "sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "rc": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    )
    if check and p.returncode:
        raise AssertionError(p.stderr)
    return p


def row(index, *, fresh=False):
    prefix = "fresh-" if fresh else ""
    key = f"property/Synthetic-{index}"
    url = f"https://daumcommercial.com/{key}/"
    return ingest.to_row(
        {
            "sourceKey": "daum-commercial",
            "id": key,
            "url": url,
            "canonicalUrl": url,
            "name": "Synthetic",
            "transactionType": "Sale",
            "daumPostId": str(50000 + index),
            "photos": [f"https://example.test/{prefix}photo.jpg"],
            "brokerIds": [0],
            "documents": [
                {"url": f"https://example.test/{prefix}doc.pdf", "docType": "brochure"}
            ],
            "media": [{"url": f"https://example.test/{prefix}tour", "mediaType": "video"}],
            "links": [{"url": f"https://example.test/{prefix}site", "linkType": "other"}],
        },
        {0: {"name": prefix + "Fixture broker", "email": prefix + "fixture@example.invalid"}},
        "2026-10-16T00:00:00Z",
    )


def generated(rows, claims=None):
    return ingest.build_sql(
        rows,
        [
            {
                "slug": "daum-commercial",
                "discovered": len(rows),
                "saved": len(rows),
                "errors": 0,
                "notes": "owned synthetic fixture",
            }
        ],
        "2026-10-16T00:00:00Z",
        set(),
        daum_verified_subset_claims=claims,
    )


members = sorted(
    [
        {
            "native_id": str(50000 + i),
            "external_id": f"property/Synthetic-{i}",
            "source_url": f"https://daumcommercial.com/property/Synthetic-{i}/",
            "emitted": i < 100,
        }
        for i in range(102)
    ],
    key=lambda m: m["source_url"],
)
claims = {
    "schema": "daum-verified-subset-v1",
    "attempt_id": "e" * 32,
    "binding": {
        "scope": "daum_verified_subset_v1",
        "inventory_count": 102,
        "emitted_count": 100,
        "held_count": 2,
    },
    "members": members,
}


try:
    image = json.loads(
        cmd(["docker", "image", "inspect", "postgres:17"], check=True).stdout
    )[0]["Id"]
    container = cmd(
        [
            "docker",
            "create",
            "--pull",
            "never",
            "--name",
            "daum-subset-pg-" + owner,
            "--label",
            label + "=" + owner,
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=256m",
            "--tmpfs",
            "/var/run/postgresql:rw,size=16m",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            image,
        ],
        check=True,
    ).stdout.strip()
    assert re.fullmatch("[a-f0-9]{64}", container)
    fixture = json.loads(cmd(["docker", "inspect", container], check=True).stdout)[0]
    assert fixture["Config"]["Labels"][label] == owner and fixture["Image"] == image
    assert (
        fixture["HostConfig"]["NetworkMode"] == "none"
        and not fixture["NetworkSettings"]["Ports"]
        and not fixture["Mounts"]
    )
    assert (
        fixture["HostConfig"]["Memory"] == 512 * 1024**2
        and fixture["HostConfig"]["NanoCpus"] == 1000000000
    )
    (out / "fixture.json").write_text(json.dumps(fixture))
    cmd(["docker", "start", container], check=True)
    started = True
    for _ in range(30):
        if (
            cmd(
                ["docker", "exec", container, "pg_isready", "-U", "postgres"], timeout=5
            ).returncode
            == 0
        ):
            break
        time.sleep(1)
    else:
        raise AssertionError("owned fixture did not become ready")
    directory = ROOT.parent / "sql"
    schema = re.sub(
        r"^\\i (\S+)\s*$",
        lambda m: (directory / m[1]).read_text(),
        (directory / "000_run_all.sql").read_text(),
        flags=re.M,
    )
    schema += "\n" + "\n".join(
        [
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4306",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text(),
        ]
    )
    cases = [
        "baseline_present",
        "deleted_parent",
        "inactive_parent",
        "terminal_parent",
        "null_parent_status",
        "soft_deleted_index",
        "null_soft_deleted",
        "absent_index",
        "unknown_presence",
        "held_native_collision",
        "held_path_collision",
        "held_index_collision",
        "same",
        "fresh_emitted_children",
        "missing_index",
        "new",
        "missing_old_native",
        "null_old_native",
    ]
    for i, case in enumerate(cases):
        db = "fixture_" + str(i)
        sql("CREATE DATABASE " + db + ";")
        sql(schema, db)
        sql(generated([row(index) for index in range(102)]), db)
        sql(
            "INSERT INTO credeals.cre_listing_om_facts (listing_id,fact_key,fact_value_text,source_doc_url,parser_version) SELECT id,'fixture','protected','https://example.test/om.pdf','fixture/1' FROM credeals.cre_listings;",
            db,
        )
        if case == "baseline_present":
            sql(
                "INSERT INTO credeals.cre_source_baseline(source_key,median_active_rows) VALUES ('daum-commercial',100);",
                db,
            )
        if case == "deleted_parent":
            sql(
                "UPDATE credeals.cre_listings SET deleted_at=now() WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case in {"inactive_parent", "terminal_parent"}:
            sql(
                "UPDATE credeals.cre_listings SET status='"
                + ("inactive" if case == "inactive_parent" else "sold")
                + "' WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case == "null_parent_status":
            sql(
                "ALTER TABLE credeals.cre_listings ALTER COLUMN status DROP NOT NULL;",
                db,
            )
            sql(
                "UPDATE credeals.cre_listings SET status=NULL WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case in {"soft_deleted_index", "null_soft_deleted"}:
            sql(
                "UPDATE credeals.cre_source_index SET soft_deleted="
                + ("true" if case == "soft_deleted_index" else "NULL")
                + " WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case == "unknown_presence":
            sql(
                "ALTER TABLE credeals.cre_source_index ALTER COLUMN observation_present DROP NOT NULL;",
                db,
            )
        if case in {"absent_index", "unknown_presence"}:
            sql(
                "UPDATE credeals.cre_source_index SET observation_present="
                + ("false" if case == "absent_index" else "NULL")
                + " WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case == "held_native_collision":
            sql(
                "UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}','\"50100\"') WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case == "held_path_collision":
            sql(
                "UPDATE credeals.cre_listings SET source_url='https://daumcommercial.com/property/Wrong/' WHERE external_id='property/Synthetic-100';",
                db,
            )
        if case == "held_index_collision":
            sql(
                "UPDATE credeals.cre_source_index SET url='https://daumcommercial.com/property/Wrong/' WHERE external_id='property/Synthetic-100';",
                db,
            )
        if case == "missing_index":
            sql(
                "DELETE FROM credeals.cre_source_index WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case == "new":
            sql(
                "DELETE FROM credeals.cre_source_index WHERE external_id='property/Synthetic-0'; DELETE FROM credeals.cre_listings WHERE external_id='property/Synthetic-0';",
                db,
            )
        if case == "missing_old_native":
            sql("UPDATE credeals.cre_listings SET raw_data=raw_data-'daumPostId';", db)
        if case == "null_old_native":
            sql(
                "UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}','null');",
                db,
            )
        tables = sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='credeals' ORDER BY tablename;",
            db,
        ).stdout.splitlines()

        def snapshot():
            parts = [
                "SELECT '"
                + t
                + "' name,md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) hash FROM credeals."
                + t
                + " t"
                for t in tables
            ]
            return sql(
                "SELECT jsonb_object_agg(name,hash)::text FROM ("
                + " UNION ALL ".join(parts)
                + ")s;",
                db,
            ).stdout.strip()

        def protected():
            parts = [
                "SELECT 'parents' name,jsonb_agg(to_jsonb(t) ORDER BY t.id)::text value FROM credeals.cre_listings t WHERE external_id IN ('property/Synthetic-100','property/Synthetic-101')",
                "SELECT 'index' name,jsonb_agg(to_jsonb(t) ORDER BY t.id)::text value FROM credeals.cre_source_index t WHERE external_id IN ('property/Synthetic-100','property/Synthetic-101')",
            ]
            for table in [
                "cre_listing_images",
                "cre_listing_contacts",
                "cre_listing_documents",
                "cre_listing_media",
                "cre_listing_links",
            ]:
                parts.append(
                    "SELECT '"
                    + table
                    + "' name,jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text value FROM credeals."
                    + table
                    + " t JOIN credeals.cre_listings l ON l.id=t.listing_id WHERE l.external_id IN ('property/Synthetic-100','property/Synthetic-101')"
                )
            for table in ["cre_listing_om_facts", "cre_source_baseline"]:
                parts.append(
                    "SELECT '"
                    + table
                    + "' name,jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text value FROM credeals."
                    + table
                    + " t"
                )
            return sql(
                "SELECT jsonb_object_agg(name,value)::text FROM ("
                + " UNION ALL ".join(parts)
                + ")s;",
                db,
            ).stdout.strip()

        def emitted_child_content():
            result = {}
            for table in ["cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"]:
                result[table] = json.loads(sql(
                    "SELECT jsonb_object_agg(external_id, children)::text FROM ("
                    "SELECT l.external_id, jsonb_agg(to_jsonb(c)-'id'-'created_at'-'scraped_at' "
                    "ORDER BY (to_jsonb(c)-'id'-'created_at'-'scraped_at')::text) children "
                    "FROM credeals." + table + " c JOIN credeals.cre_listings l "
                    "ON l.id=c.listing_id WHERE l.external_id NOT IN "
                    "('property/Synthetic-100','property/Synthetic-101') GROUP BY l.external_id)s;", db
                ).stdout)
            return result

        children_before = emitted_child_content() if case == "fresh_emitted_children" else None
        before = snapshot()
        held_before = protected()
        lifecycle = "SELECT jsonb_agg(jsonb_build_array(l.external_id,l.status,l.deleted_at,si.soft_deleted,si.observation_present,si.presence_generation,si.presence_changed_at) ORDER BY l.external_id)::text FROM credeals.cre_listings l JOIN credeals.cre_source_index si USING(brokerage_id,external_id) WHERE l.external_id NOT IN ('property/Synthetic-100','property/Synthetic-101');"
        life_before = sql(lifecycle, db).stdout
        text = generated([row(index, fresh=case == "fresh_emitted_children" and index == 0) for index in range(100)], claims)
        (out / (case + ".sql")).write_text(text)
        result = sql(text, db, check=False)
        negative = case not in {
            "same",
            "fresh_emitted_children",
            "missing_index",
            "new",
            "missing_old_native",
            "null_old_native",
        }
        unchanged = snapshot() == before
        held_unchanged = protected() == held_before
        lifecycle_unchanged = sql(lifecycle, db).stdout == life_before
        checks.append(
            {
                "case": case,
                "expected_refusal": negative,
                "refused": result.returncode != 0,
                "all_tables_unchanged": unchanged,
                "tables": len(tables),
                "held_children_index_om_baseline_unchanged": held_unchanged,
                "lifecycle_unchanged": lifecycle_unchanged,
            }
        )
        if negative:
            assert result.returncode != 0 and ("DAUM" in result.stderr), (
                case,
                result.stderr,
            )
            assert unchanged, case + " partially mutated tables"
        else:
            assert result.returncode == 0, (case, result.stderr)
            assert held_unchanged, case + " touched held child or protected table"
            if case == "fresh_emitted_children":
                children_after = emitted_child_content()
                deltas = {}
                for table, prior in children_before.items():
                    current = children_after[table]
                    assert set(prior) == set(current) and len(current) == 100, table
                    assert all(len(rows) == 1 for rows in prior.values()), table
                    assert all(len(rows) == 1 for rows in current.values()), table
                    changed = sorted(key for key in prior if prior[key] != current[key])
                    assert changed == ["property/Synthetic-0"], (table, changed)
                    deltas[table] = {"emitted_rows_before": 100, "emitted_rows_after": 100, "changed_identities": changed}
                checks[-1]["fresh_emitted_child_deltas"] = deltas

            if case not in {"new", "missing_index"}:
                assert lifecycle_unchanged, case
        sql("DROP DATABASE " + db + ";")
        sql("CHECKPOINT;")

    print(json.dumps({"checks": checks, "cluster": str(cluster), "settled": True}))
finally:
    removed = container is None
    if container:
        obj = json.loads(cmd(["docker", "inspect", container], check=True).stdout)[0]
        assert (
            obj["Id"] == container
            and obj["Config"]["Labels"][label] == owner
            and obj["Image"] == image
        )
        if started:
            cmd(
                ["docker", "stop", "--timeout", "10", container], check=True, timeout=20
            )
        cmd(["docker", "rm", container], check=True)
        removed = cmd(["docker", "inspect", container]).returncode != 0
        assert removed
    (out / "checks.json").write_text(json.dumps(checks, indent=2))
    (out / "transcript.json").write_text(json.dumps(transcript, indent=2))
    (out / "settlement.json").write_text(
        json.dumps(
            {
                "owned_container": container,
                "label": label,
                "owner": owner,
                "removed": removed,
            }
        )
    )
