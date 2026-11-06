"""Owned Unix-socket PG17 fixture only; never reads application environment."""

import hashlib
import copy
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
import cre_validate as validate  # noqa: E402

if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] not in ("--pyramid", "--pyramid-benchmark", "--unknown")):
    raise ValueError("usage: run_daum_subset_postgres_contract.py NEW_PROOF_DIR [--pyramid|--pyramid-benchmark|--unknown]")
unknown_mode = len(sys.argv) == 3 and sys.argv[2] == "--unknown"
pyramid_mode = len(sys.argv) == 3 and not unknown_mode
benchmark_mode = len(sys.argv) == 3 and sys.argv[2] == "--pyramid-benchmark"
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


def row(
    index, *, fresh=False, transaction="Sale", transaction_mode=None, name="Synthetic"
):
    prefix = "fresh-" if fresh else ""
    key = f"property/Synthetic-{index}"
    url = f"https://daumcommercial.com/{key}/"
    return ingest.to_row(
        {
            "sourceKey": "daum-commercial",
            "id": key,
            "url": url,
            "canonicalUrl": url,
            "name": name,
            "transactionType": transaction,
            **(
                {"transactionMode": transaction_mode}
                if transaction_mode is not None
                else {}
            ),
            "daumPostId": str(50000 + index),
            "photos": [f"https://example.test/{prefix}photo.jpg"],
            "brokerIds": [0],
            "documents": [
                {"url": f"https://example.test/{prefix}doc.pdf", "docType": "brochure"}
            ],
            "media": [
                {"url": f"https://example.test/{prefix}tour", "mediaType": "video"}
            ],
            "links": [
                {"url": f"https://example.test/{prefix}site", "linkType": "other"}
            ],
        },
        {
            0: {
                "name": prefix + "Fixture broker",
                "email": prefix + "fixture@example.invalid",
            }
        },
        "2026-10-16T00:00:00Z",
    )


def generated(rows, claims=None):
    if claims is not None and "identityMembers" not in claims:
        from cre_daum_current_inventory import _sha
        claims = copy.deepcopy(claims)
        identity = [{key: member[key] for key in ("native_id", "external_id", "source_url")}
                    for member in claims["members"]]
        partition = [{**member, "conflicted": False} for member in identity]
        claims["identityMembers"] = identity
        claims["identityPartition"] = partition
        claims["binding"]["identity_partition_sha256"] = _sha(partition)
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
title_observations_v2 = [["50100", 1, "empty"]]
claims_v2 = {
    "schema": "daum-verified-subset-v2",
    "attempt_id": "f" * 32,
    "subset_policy": "tenure-and-empty-title-v2",
    "titleObservations": title_observations_v2,
    "binding": {
        "scope": "daum_verified_subset_v2",
        "subset_policy": "tenure-and-empty-title-v2",
        "inventory_count": 102,
        "emitted_count": 100,
        "held_count": 2,
        "title_observations_sha256": hashlib.sha256(
            json.dumps(
                title_observations_v2,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    },
    "members": members,
}
assert len(members) == len({member["native_id"] for member in members}) == 102
assert len({member["external_id"] for member in members}) == 102
assert sum(member["emitted"] for member in members) == 100
assert sum(not member["emitted"] for member in members) == 2


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
    # Exercise the real generated transaction with one changed path sharing a
    # native ID and an already source-held conflicting member.  All rows and
    # target state here are synthetic and live only in this owned container.
    from cre_daum_current_inventory import _sha
    from cre_daum_identity_sql import identity_partition_query
    db = "identity_partition"
    sql("CREATE DATABASE " + db + ";")
    sql(schema, db)
    sql(generated([row(i) for i in range(103)]), db)
    sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}', '\"50000\"') "
        "WHERE external_id='property/Synthetic-102';", db)
    ids = [{key: item[key] for key in ("native_id", "external_id", "source_url")} for item in members]
    result = sql(identity_partition_query(ids) + ";", db).stdout.strip().split("|", 1)
    assert result[0] == "102"
    part = json.loads(result[1])
    assert {item["native_id"] for item in part if item["conflicted"]} == {"50000", "50102"}
    bound_claims = copy.deepcopy(claims)
    bound_claims.update(identityMembers=ids, identityPartition=part)
    bound_claims["binding"].update(identity_partition_sha256=_sha(part), source_emitted_count=100,
                                    source_held_count=2, emitted_count=99, held_count=3)
    before = sql("SELECT jsonb_agg(to_jsonb(l) ORDER BY external_id)::text FROM credeals.cre_listings l "
                 "WHERE external_id IN ('property/Synthetic-0','property/Synthetic-102');", db).stdout
    incoming_identity = [row(i, fresh=True, name="Changed" if i == 1 else "Synthetic") for i in range(100)]
    sql(generated(incoming_identity, bound_claims), db)
    after = sql("SELECT jsonb_agg(to_jsonb(l) ORDER BY external_id)::text FROM credeals.cre_listings l "
                "WHERE external_id IN ('property/Synthetic-0','property/Synthetic-102');", db).stdout
    assert after == before
    assert sql("SELECT status || '|' || listings_saved FROM credeals.cre_scrape_jobs WHERE status='partial' "
               "ORDER BY completed_at DESC LIMIT 1;", db).stdout.strip() == "partial|99"
    assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='property/Synthetic-1';", db).stdout.strip() == "Changed"
    stale = copy.deepcopy(bound_claims)
    sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}', '\"50102\"') "
        "WHERE external_id='property/Synthetic-102';", db)
    refused = sql(generated([row(i, fresh=True) for i in range(100)], stale), db, check=False)
    assert refused.returncode and "DAUM identity partition changed" in refused.stderr
    checks.append("identity_partition_conflict_peer_and_stale")
    sql("DROP DATABASE " + db + ";")
    if unknown_mode:
        # Source producer -> authenticated read-only probe -> both gates ->
        # production staging SQL -> real-schema DB -> freshness readback.
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "tests"))
        import cre_checkpoint_refresh as checkpoint
        import cre_current_inventory_admission as admission
        import cre_daum_current_inventory as daum
        import test_essex_current_inventory as helpers
        from test_daum_current_inventory import produce
        from test_daum_verified_subset import envelope

        db = "fallback_producer"
        sql("CREATE DATABASE " + db + ";")
        sql(schema, db)
        source_seed = [row(index) for index in range(101)]
        for seed_row in source_seed:
            for collection in ("images", "contacts", "documents", "media", "links"):
                seed_row[collection] = []
        sql(generated(source_seed), db)
        sql("DELETE FROM credeals.cre_source_index WHERE external_id='property/Synthetic-100'; "
            "DELETE FROM credeals.cre_listings WHERE external_id='property/Synthetic-100';", db)
        producer_dir = out / "offline-producer"
        producer_dir.mkdir(mode=0o700)
        produced = produce(producer_dir, total=101, empty_title=True, corroborated_title=True,
                           with_spaces=True, subset_policy="tenure-and-empty-title-v2")
        helpers.SOURCE = "daum-commercial"
        prepared = helpers.setup(producer_dir, produced)
        manifest = prepared[3]
        manifest["config"]["current_inventory_admission"] = admission.subset_configuration("tenure-and-empty-title-v2")
        fake_url = "postgresql://postgres@fixture.invalid/fallback_producer"
        target = ingest.database_target_fingerprint_from_url(fake_url)
        manifest["preflight"]["database_target"] = target

        def probe_psql(_psql, _db_url, script, **_kwargs):
            result = cmd(["docker", "exec", "-i", container, "psql", "-X", "-q", "-A",
                          "-F", "\t", "-P", "footer=off", "-U", "postgres", "-d", db,
                          "-v", "ON_ERROR_STOP=1", "-f", "-"], input=script)
            assert result.returncode == 0, result.stderr
            return result

        from cre_daum_fallback_partition import run_identity_probe
        with (
            patch.object(ingest, "load_db_url", return_value=(fake_url, None)),
            patch.object(ingest, "find_psql", return_value="fixture-psql"),
            patch.object(validate, "_run_psql_script", side_effect=probe_psql),
            patch.object(checkpoint, "git_identity", return_value=(manifest["collector_git_sha"], False)),
        ):
            manifest["sources"]["daum-commercial"]["daum_fallback_probe"] = run_identity_probe(
                prepared[1], manifest["sources"]["daum-commercial"]["artifact"]["sha256"],
                manifest["run_id"], manifest["collector_git_sha"], target["value"], None)
        source_envelope = envelope(prepared)
        source_claims = daum.ingest_subset_context(
            prepared[1], source_envelope, current_code_sha=manifest["collector_git_sha"],
            current_git_dirty=False, expected_database_target_sha256=target["value"], attempt_id="e" * 32)
        staged = {}
        for listing in prepared[2]["listings"]:
            mapped = ingest.to_row(listing, {}, prepared[2]["runMeta"]["finishedAt"])
            key = mapped["external_id"]
            staged[key] = ingest.merge_rows(staged[key], mapped) if key in staged else mapped
        source_meta = [{"slug": "daum-commercial", "discovered": len(staged), "saved": len(staged),
                        "errors": 0, "notes": "offline-producer exact proof"}]
        source_sql = ingest.build_sql(list(staged.values()), source_meta, manifest["started_at"], set(),
                                      finished_at=prepared[2]["runMeta"]["finishedAt"],
                                      daum_verified_subset_claims=source_claims)
        sql(source_sql, db)
        fallback_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                     "WHERE external_id='property/Synthetic-100';", db).stdout)
        import base64
        source_saved = json.loads(base64.b64decode(fallback_raw["daumTitleFallbackHistory"][0]["rawBase64"]))
        assert sql("SELECT address IS NULL FROM credeals.cre_listings "
                   "WHERE external_id='property/Synthetic-100';", db).stdout.strip() == "t"
        assert source_saved["daumTitleFallbackDetail"]["city"] == "Phoenix"
        assert "$6,600 per month" in source_saved["daumAvailableSpaceObservations"][0]["text"]
        generations = [line.split("|") for line in sql(validate.QUERIES["freshness_generations"], db).stdout.splitlines()]
        generation_rows = [line for line in generations if line[0] == "daum-commercial" and line[3] == manifest["run_id"]]
        assert len(generation_rows) == 1 and generation_rows[0][6] == "101", generation_rows
        checks.append("fallback_offline_producer_to_real_db_readback")

        # Exercise the actual fallback ingest SQL against owned real-schema DBs.
        from cre_daum_current_inventory import _sha
        from cre_daum_fallback_partition import identity_probe_sql
        for fallback_case in ("existing", "new", "terminal", "conflict"):
            db = "fallback_" + fallback_case
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db)
            sql(generated([row(index) for index in range(103)]), db)
            key = "property/Synthetic-0"
            url = "https://daumcommercial.com/property/Synthetic-0/"
            if fallback_case == "new":
                sql("DELETE FROM credeals.cre_source_index WHERE external_id='property/Synthetic-0'; "
                    "DELETE FROM credeals.cre_listings WHERE external_id='property/Synthetic-0';", db)
            if fallback_case == "terminal":
                sql("UPDATE credeals.cre_listings SET status='withdrawn' WHERE external_id='property/Synthetic-0';", db)
            if fallback_case == "conflict":
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}',"
                    "'\"50000\"') WHERE external_id='property/Synthetic-102';", db)
            prior_parent = sql("SELECT coalesce((to_jsonb(l)-'raw_data'-'updated_at')::text,'null') "
                               "FROM credeals.cre_listings l WHERE external_id='property/Synthetic-0';", db).stdout.strip()
            child_tables = ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents",
                            "cre_listing_media", "cre_listing_links")
            def fallback_children():
                return {table: sql("SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY c.id)::text,'[]') "
                                   "FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id "
                                   "WHERE l.external_id='property/Synthetic-0';", db).stdout.strip()
                        for table in child_tables}
            def fallback_index():
                return sql("SELECT coalesce(to_jsonb(si)::text,'null') FROM credeals.cre_source_index si "
                           "WHERE external_id='property/Synthetic-0';", db).stdout.strip()
            prior_children = fallback_children()
            prior_index = fallback_index()
            candidate = row(0, fresh=True, transaction="Lease", name="Observed fallback")
            candidate["raw_data"].update({
                "daumTitleFallback": {"cardTitle": "Observed fallback", "documentTitle": "Observed fallback – Daum Commercial"},
                "daumSearchCard": {"title": "Observed fallback", "url": url},
                "daumAvailableSpaceObservations": [{"text": "95,151 SF $1.15/SF/month", "html": "<li>95,151 SF</li>"}],
                "preserveChildCollections": True, "detailObservedWithChildPreservation": True,
                "inventoryObservedAt": "2026-10-17T00:00:00Z", "detailObservedAt": "2026-10-17T00:00:01Z",
                "freshnessProvenance": {"generationId": "fallback-generation", "detailScope": "detail_page",
                                        "cacheDisposition": "live", "method": "daum_direct_detail_html",
                                        "identityMethod": "wordpress_shortlink_post_id"},
            })
            for child in candidate["images"] + candidate["contacts"]:
                child.pop("isPrimary", None)
            fallback_members = [{"native_id": "50000", "external_id": key, "source_url": url,
                                 "inventory_at": "2026-10-17T00:00:00Z", "detail_at": "2026-10-17T00:00:01Z"}]
            probe = sql(identity_probe_sql(fallback_members), db).stdout.strip().split("|", 2)
            assert probe[:2] == ["1", "1"]
            partition = json.loads(probe[2])
            assert partition[0]["hold_reason"] == {"terminal": "existing_parent_unqualified", "conflict": "identity_conflict"}.get(fallback_case)
            fallback_claims = copy.deepcopy(claims_v2)
            fallback_claims["titleFallbackObservations"] = [
                {"postId": "50000", "cardTitle": "Observed fallback", "documentTitle": "Observed fallback – Daum Commercial"}]
            fallback_claims["fallbackMembers"] = fallback_members
            fallback_claims["fallbackPartition"] = partition
            fallback_claims["binding"]["title_fallback_observations_sha256"] = _sha(fallback_claims["titleFallbackObservations"])
            fallback_claims["binding"]["fallback_partition_sha256"] = _sha(partition)
            incoming = [candidate] + [row(index) for index in range(1, 100)]
            # Provider data may use the same word under a non-authoritative
            # nested key; it is not a DAUM fallback marker.
            incoming[1]["raw_data"]["providerNotes"] = {"daumTitleFallback": "ordinary source value"}
            result = sql(generated(incoming, fallback_claims), db, check=False)
            assert result.returncode == 0, result.stderr
            job = sql("SELECT status || '|' || listings_saved::text FROM credeals.cre_scrape_jobs "
                      "WHERE status='partial';", db).stdout.strip()
            assert job == ("partial|99" if fallback_case in {"terminal", "conflict"} else "partial|100"), job
            current_parent = sql("SELECT (to_jsonb(l)-'raw_data'-'updated_at')::text "
                                 "FROM credeals.cre_listings l WHERE external_id='property/Synthetic-0';", db).stdout.strip()
            current_children = fallback_children()
            if fallback_case in ("existing", "terminal", "conflict"):
                assert current_parent == prior_parent, (fallback_case, current_parent, prior_parent)
                assert current_children == prior_children
                assert fallback_index() == prior_index
            raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                 "WHERE external_id='property/Synthetic-0';", db).stdout)
            if fallback_case not in {"terminal", "conflict"}:
                import base64
                history = raw["daumTitleFallbackHistory"]
                assert len(history) == 1
                assert json.loads(base64.b64decode(history[0]["rawBase64"])) == candidate["raw_data"]
                assert validate.persisted_freshness_provenance(raw, source_key="daum-commercial")["generation_id"] == "fallback-generation"
                freshness = sql(validate.QUERIES["freshness_generations"], db).stdout
                assert "daum-commercial|strict_detail|current_strict_detail|fallback-generation|" in freshness
                # A later ordinary upsert must retain the original fallback bytes.
                sql(generated([row(index) for index in range(100)]), db)
                raw_after = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                           "WHERE external_id='property/Synthetic-0';", db).stdout)
                assert raw_after["daumTitleFallbackHistory"] == history
                after_full_parent = sql("SELECT (to_jsonb(l)-'raw_data'-'updated_at')::text "
                                        "FROM credeals.cre_listings l WHERE external_id='property/Synthetic-0';", db).stdout.strip()
                after_full_children = fallback_children()
                after_full_index = fallback_index()
                stale_members = [{**fallback_members[0], "inventory_at": "2026-10-15T00:00:00Z",
                                  "detail_at": "2026-10-15T00:00:01Z"}]
                stale_partition = json.loads(sql(identity_probe_sql(stale_members), db).stdout.strip().split("|", 2)[2])
                assert stale_partition[0]["hold_reason"] == "newer_target_observation"
                later = copy.deepcopy(candidate)
                later["raw_data"]["daumAvailableSpaceObservations"][0]["text"] = "Later source observation"
                later["raw_data"]["inventoryObservedAt"] = "2026-10-18T00:00:00Z"
                later["raw_data"]["detailObservedAt"] = "2026-10-18T00:00:01Z"
                later["raw_data"]["freshnessProvenance"]["generationId"] = "later-generation"
                later_members = [{**fallback_members[0], "inventory_at": "2026-10-18T00:00:00Z",
                                  "detail_at": "2026-10-18T00:00:01Z"}]
                for mutation in (
                    "jsonb_set(raw_data,'{latestDaumTitleFallbackObservation}','5'::jsonb)",
                    "jsonb_set(raw_data,'{latestDaumTitleFallbackObservation,unknownField}','\"keep\"'::jsonb)",
                    "jsonb_set(raw_data,'{latestDaumTitleFallbackObservation,freshnessProvenance,method}','null'::jsonb)",
                    "jsonb_set(raw_data,'{latestDaumTitleFallbackObservation,detailObservedAt}','\"2026-01-99T00:00:00Z\"'::jsonb)",
                    "jsonb_set(raw_data,'{daumFreshnessOwner}','\"unrecognized\"'::jsonb)",
                    "raw_data - 'daumTitleFallbackHistory'",
                ):
                    invalid_probe = sql("BEGIN; UPDATE credeals.cre_listings SET raw_data=" + mutation
                                        + " WHERE external_id='property/Synthetic-0';"
                                        + identity_probe_sql(later_members) + "; ROLLBACK;", db).stdout.strip().split("|", 2)
                    assert json.loads(invalid_probe[2])[0]["hold_reason"] == "existing_evidence_unqualified"
                later_probe = sql(identity_probe_sql(later_members), db).stdout.strip().split("|", 2)
                later_partition = json.loads(later_probe[2])
                assert later_partition[0]["hold_reason"] is None
                later_claims = copy.deepcopy(fallback_claims)
                later_claims["fallbackMembers"] = later_members
                later_claims["fallbackPartition"] = later_partition
                later_claims["binding"]["fallback_partition_sha256"] = _sha(later_partition)
                sql(generated([later] + [row(index) for index in range(1, 100)], later_claims), db)
                later_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                           "WHERE external_id='property/Synthetic-0';", db).stdout)
                assert len(later_raw["daumTitleFallbackHistory"]) == 2
                assert later_raw["daumTitleFallbackHistory"][0] == history[0]
                assert json.loads(base64.b64decode(later_raw["daumTitleFallbackHistory"][1]["rawBase64"])) == later["raw_data"]
                assert validate.persisted_freshness_provenance(later_raw, source_key="daum-commercial")["generation_id"] == "later-generation"
                assert sql("SELECT (to_jsonb(l)-'raw_data'-'updated_at')::text FROM credeals.cre_listings l "
                           "WHERE external_id='property/Synthetic-0';", db).stdout.strip() == after_full_parent
                assert fallback_children() == after_full_children
                assert fallback_index() == after_full_index
                replay_partition = json.loads(sql(identity_probe_sql(later_members), db).stdout.strip().split("|", 2)[2])
                assert replay_partition[0]["hold_reason"] == "newer_target_observation"
                replay_claims = copy.deepcopy(later_claims)
                replay_claims["fallbackPartition"] = replay_partition
                replay_claims["binding"]["fallback_partition_sha256"] = _sha(replay_partition)
                replay = sql(generated([later] + [row(index) for index in range(1, 100)], replay_claims), db, check=False)
                assert replay.returncode and "DAUM fallback artifact already committed" in replay.stderr
                assert sql("SELECT status || '|' || listings_saved::text FROM credeals.cre_scrape_jobs "
                           "WHERE status='partial' ORDER BY completed_at DESC LIMIT 1;", db).stdout.strip() == "partial|100"
                replay_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings "
                                            "WHERE external_id='property/Synthetic-0';", db).stdout)
                assert replay_raw["daumTitleFallbackHistory"] == later_raw["daumTitleFallbackHistory"]
            else:
                assert "daumTitleFallbackHistory" not in raw
                replay = sql(generated(incoming, fallback_claims), db, check=False)
                assert replay.returncode and "DAUM fallback artifact already committed" in replay.stderr
                assert sql("SELECT status || '|' || listings_saved::text FROM credeals.cre_scrape_jobs "
                           "WHERE status='partial';", db).stdout.strip() == "partial|99"
            checks.append("fallback_actual_ingest_" + fallback_case)
        unknown_claims = copy.deepcopy(claims_v2)
        for member in unknown_claims["members"]:
            if member["native_id"] == "50101":
                member["emitted"] = True
        unknown_claims["binding"].update(emitted_count=101, held_count=1)
        for case in ("unknown_new", "unknown_new_no_license", "unknown_existing", "unknown_existing_no_license", "unknown_conflict", "unknown_baseline"):
            db = "fixture_" + case
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db)
            sql(generated([row(index) for index in range(102)]), db)
            if case == "unknown_existing":
                from cre_daum_fallback_partition import identity_probe_sql
                members_probe = [
                    {"native_id": "50000", "external_id": "property/Synthetic-0",
                     "source_url": "https://daumcommercial.com/property/Synthetic-0/"},
                    {"native_id": "59999", "external_id": "property/Synthetic-new",
                     "source_url": "https://daumcommercial.com/property/Synthetic-new/"},
                ]
                for member in members_probe:
                    member.update(inventory_at="2026-10-17T00:00:00Z", detail_at="2026-10-17T00:00:01Z")
                probe_query = identity_probe_sql(members_probe)
                def probe(prefix=""):
                    result = sql("BEGIN;" + prefix + probe_query + "; ROLLBACK;", db).stdout.strip().split("|", 2)
                    assert result[:2] == ["2", "2"]
                    return {item["native_id"]: item for item in json.loads(result[2])}
                normal = probe()
                assert normal["50000"]["hold_reason"] is None, normal
                missing_canonical = probe("UPDATE credeals.cre_listings SET canonical_url=NULL WHERE external_id='property/Synthetic-0';")
                assert missing_canonical["50000"]["hold_reason"] is None
                assert normal["59999"]["hold_reason"] is None, normal
                from cre_daum_fallback_partition import transaction_guard_sql
                partition = [normal[key] for key in sorted(normal, key=lambda key: normal[key]["external_id"])]
                guard = transaction_guard_sql(members_probe, partition)
                sql("BEGIN;" + guard + "ROLLBACK;", db)
                stale = sql("BEGIN; UPDATE credeals.cre_listings SET status='withdrawn' WHERE external_id='property/Synthetic-0';" + guard + "ROLLBACK;", db, check=False)
                assert stale.returncode and "DAUM fallback partition changed" in stale.stderr
                # A cooperating lifecycle writer commits after the initial probe.
                # The admission transaction waits on the shared advisory lock,
                # then must refuse its stale parent preimage.
                writer = subprocess.Popen(
                    ["docker", "exec", "-i", container, "psql", "-X", "-qAt",
                     "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1"],
                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1,
                )
                writer.stdin.write(
                    "BEGIN;\n" + ingest.lifecycle_transaction_lock_sql()
                    + "\nUPDATE credeals.cre_listings SET status='withdrawn' "
                    "WHERE external_id='property/Synthetic-0';\n\\echo WRITER_READY\n"
                    "SELECT pg_sleep(4);\nCOMMIT;\n"
                )
                writer.stdin.flush()
                for line in writer.stdout:
                    if line.strip() == "WRITER_READY":
                        break
                else:
                    raise AssertionError("cooperating writer did not acquire lock")
                reader = subprocess.Popen(
                    ["docker", "exec", "-i", container, "psql", "-X", "-qAt",
                     "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1"],
                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1,
                )
                reader.stdin.write(
                    "BEGIN; SET LOCAL lock_timeout='6s'; SELECT pg_backend_pid();\n"
                    + ingest.lifecycle_transaction_lock_sql() + guard + "ROLLBACK;\n"
                )
                reader.stdin.flush()
                reader_pid = int(reader.stdout.readline().strip())
                deadline = time.monotonic() + 3
                observed_waiter = False
                while time.monotonic() < deadline:
                    observed_waiter = sql(
                        "SELECT count(*) FROM pg_stat_activity WHERE pid="
                        + str(reader_pid) + " AND wait_event_type='Lock' AND wait_event='advisory';", db
                    ).stdout.strip() == "1"
                    if observed_waiter:
                        break
                    time.sleep(0.05)
                assert observed_waiter, "reader never observed waiting on lifecycle lock"
                writer.stdin.close()
                assert writer.wait(timeout=10) == 0, writer.stderr.read()
                reader.stdin.close()
                assert reader.wait(timeout=10) != 0
                assert "DAUM fallback partition changed" in reader.stderr.read()
                sql("UPDATE credeals.cre_listings SET status='active' WHERE external_id='property/Synthetic-0';", db)
                checks.append("fallback_two_session_stale_preimage")
                terminal = probe("UPDATE credeals.cre_listings SET status='withdrawn' WHERE external_id='property/Synthetic-0';")
                assert terminal["50000"]["hold_reason"] == "existing_parent_unqualified"
                assert terminal["59999"]["hold_reason"] is None
                assert terminal["50000"]["preimage"] != normal["50000"]["preimage"]
                newer_parent = probe("UPDATE credeals.cre_listings SET scraped_at='2026-10-18T00:00:00Z' WHERE external_id='property/Synthetic-0';")
                assert newer_parent["50000"]["hold_reason"] == "newer_target_observation"
                assert newer_parent["59999"]["hold_reason"] is None
                newer_inventory = probe("UPDATE credeals.cre_source_index SET last_enumerated_at='2026-10-18T00:00:00Z' WHERE external_id='property/Synthetic-0';")
                assert newer_inventory["50000"]["hold_reason"] == "newer_target_observation"
                assert newer_inventory["59999"]["hold_reason"] is None
                sql("UPDATE credeals.cre_listings SET status='withdrawn' WHERE external_id='property/Synthetic-0';", db)
                held_now = probe()
                held_partition = [held_now[key] for key in sorted(held_now, key=lambda key: held_now[key]["external_id"])]
                held_guard = transaction_guard_sql(members_probe, held_partition, filter_src=True)
                stage = """
CREATE TEMP TABLE _src ON COMMIT DROP AS
SELECT brokerage_id, external_id, source_url, raw_data, 'daum-commercial'::text AS slug FROM credeals.cre_listings
 WHERE external_id='property/Synthetic-0'
UNION ALL SELECT id, 'property/Synthetic-new',
  'https://daumcommercial.com/property/Synthetic-new/',
  jsonb_build_object('sourceKey','daum-commercial','daumPostId','59999'), 'daum-commercial'
 FROM credeals.cre_brokerages WHERE slug='daum-commercial'
UNION ALL SELECT id, 'foreign', 'https://example.invalid/foreign',
  jsonb_build_object('sourceKey','savills','daumTitleFallback','unrelated'), 'savills'
 FROM credeals.cre_brokerages WHERE slug='savills';
"""
                held_result = sql("BEGIN;" + stage + held_guard
                                  + "SELECT external_id FROM _src ORDER BY external_id; ROLLBACK;", db)
                assert held_result.stdout.strip().splitlines() == ["foreign", "property/Synthetic-new"]
                rogue = """
INSERT INTO _src
SELECT id, 'property/rogue', 'https://daumcommercial.com/property/rogue/',
 jsonb_build_object('sourceKey','daum-commercial','primary',
   jsonb_build_object('secondary_pass',jsonb_build_object('daumTitleFallback','unbound'))),
 'daum-commercial'
FROM credeals.cre_brokerages WHERE slug='daum-commercial';
"""
                refused = sql("BEGIN;" + stage + rogue + held_guard + "ROLLBACK;", db, check=False)
                assert refused.returncode and "DAUM fallback staging differs" in refused.stderr
                sql("UPDATE credeals.cre_listings SET status='active' WHERE external_id='property/Synthetic-0';", db)
                checks.append("fallback_held_filtered_valid_peer_retained")
                conflict = probe("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}'," + "'\"50000\"') WHERE external_id='property/Synthetic-1';")
                assert conflict["50000"]["hold_reason"] == "identity_conflict"
                assert conflict["59999"]["hold_reason"] is None
                checks.append("fallback_probe_existing_new_terminal_conflict")
            sql("UPDATE credeals.cre_listings SET transaction_type='lease', "
                "raw_data=raw_data || '{\"priorEvidence\":\"keep-me\","
                "\"inventoryObservedAt\":\"2026-10-15T00:00:00Z\","
                "\"detailObservedAt\":\"2026-10-15T00:00:01Z\","
                "\"freshnessProvenance\":{\"generationId\":\"old-generation\","
                "\"detailScope\":\"detail_page\",\"cacheDisposition\":\"live\"}}'::jsonb "
                "WHERE external_id='property/Synthetic-101';", db)
            if case.endswith("_no_license"):
                sql("ALTER TABLE credeals.cre_listing_contacts DROP COLUMN license;", db)
            if case.startswith("unknown_new"):
                sql("DELETE FROM credeals.cre_source_index WHERE external_id='property/Synthetic-101'; "
                    "DELETE FROM credeals.cre_listings WHERE external_id='property/Synthetic-101';", db)
            if case == "unknown_baseline":
                sql("INSERT INTO credeals.cre_source_baseline(source_key,median_active_rows) "
                    "VALUES ('daum-commercial',100);", db)
            if case == "unknown_conflict":
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{daumPostId}',"
                    "'\"50101\"') WHERE external_id='property/Synthetic-0';", db)
            prior_raw = None if case.startswith("unknown_new") else json.loads(sql(
                "SELECT raw_data::text FROM credeals.cre_listings "
                "WHERE external_id='property/Synthetic-101';", db
            ).stdout)
            unknown = row(101, fresh=True, name="Observed current title")
            unknown["transaction_type"] = None
            for child in unknown["images"] + unknown["contacts"]:
                child.pop("isPrimary", None)
            unknown["raw_data"].update({
                "daumUnknownTenure": True,
                "daumAvailableSpaceObservations": [
                    {"html": "<li>$0 PSF <p>$6,600 per month</p></li>",
                     "text": "$0 PSF $6,600 per month"}
                ],
                "preserveChildCollections": True,
                "detailObservedWithChildPreservation": True,
                "daumTenureEvidence": {"inventory": "unknown", "detail": "unknown", "resolved": "unknown"},
                "inventoryObservedAt": "2026-10-16T00:00:00Z",
                "detailObservedAt": "2026-10-16T00:00:01Z",
                "freshnessProvenance": {"generationId": "new-generation", "detailScope": "detail_page", "cacheDisposition": "live"},
            })
            if case.startswith("unknown_existing"):
                sql("UPDATE credeals.cre_listing_contacts SET title='Prior title', "
                    "phone=NULL, is_primary=true WHERE listing_id=(SELECT id FROM "
                    "credeals.cre_listings WHERE external_id='property/Synthetic-101'); "
                    "INSERT INTO credeals.cre_listing_contacts "
                    "(listing_id,name,email,title,phone,is_primary) SELECT listing_id, "
                    "'Second old name',email,'Second prior title',NULL,false "
                    "FROM credeals.cre_listing_contacts WHERE listing_id=(SELECT id FROM "
                    "credeals.cre_listings WHERE external_id='property/Synthetic-101');", db)
                unknown["contacts"] = [
                    {"name": "New name", "email": "fixture@example.invalid",
                     "title": "New title", "phone": "555-0101", "isPrimary": True},
                    {"name": "New contact", "email": "new@example.invalid", "title": "first", "isPrimary": True},
                    {"name": "Different observed name", "email": "NEW@example.invalid", "title": "second", "isPrimary": True},
                    {"name": "Phone match", "phone": "555-0123", "title": "first", "isPrimary": True},
                    {"name": "PHONE MATCH", "phone": "555-0123", "title": "second", "isPrimary": True},
                ]
                unknown["raw_data"]["contactsDetailed"] = copy.deepcopy(unknown["contacts"])
            if case.startswith("unknown_new"):
                unknown["contacts"] = [
                    {"name": "First", "email": "first@example.invalid", "isPrimary": True},
                    {"name": "Second", "email": "second@example.invalid", "isPrimary": True},
                    {"name": "Duplicate", "email": "FIRST@example.invalid", "isPrimary": True},
                    {"name": "Phone", "phone": "555-0123", "isPrimary": True},
                    {"name": "PHONE", "phone": "555-0123", "isPrimary": True},
                    {"name": "Amy", "email": "bridge-a@example.invalid", "phone": "111"},
                    {"name": "Bea", "email": "bridge-a@example.invalid", "phone": "222"},
                    {"name": "Bea", "email": "bridge-c@example.invalid", "phone": "222"},
                ]
                unknown["raw_data"]["contactsDetailed"] = copy.deepcopy(unknown["contacts"])
            child_tables = ("cre_listing_images", "cre_listing_contacts",
                            "cre_listing_documents", "cre_listing_media", "cre_listing_links")
            def children(table):
                return json.loads(sql(
                    "SELECT coalesce(jsonb_agg(to_jsonb(c)-'scraped_at' ORDER BY c.id)::text,'[]') "
                    "FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id "
                    "WHERE l.external_id='property/Synthetic-101';", db
                ).stdout)
            before_children = {table: children(table) for table in child_tables}
            before_rows = sql("SELECT jsonb_agg(to_jsonb(l) ORDER BY l.id)::text FROM credeals.cre_listings l;", db).stdout
            result = sql(generated([row(index) for index in range(100)] + [unknown], unknown_claims), db, check=False)
            if case in {"unknown_conflict", "unknown_baseline"}:
                assert result.returncode != 0 and sql("SELECT jsonb_agg(to_jsonb(l) ORDER BY l.id)::text FROM credeals.cre_listings l;", db).stdout == before_rows
            else:
                assert result.returncode == 0, result.stderr
                state = sql("SELECT coalesce(transaction_type,'<null>') || '|' || "
                            "(raw_data ? 'latestDaumUnknownTenureObservation')::text "
                            "FROM credeals.cre_listings WHERE external_id='property/Synthetic-101';", db).stdout.strip()
                assert state == ("lease|true" if case.startswith("unknown_existing") else "<null>|false"), state
                if case.startswith("unknown_new"):
                    contacts = children("cre_listing_contacts")
                    assert not any(c["is_primary"] for c in contacts)
                    assert len(contacts) == 5
                    assert {c["email"] for c in contacts} >= {
                        "bridge-a@example.invalid", "bridge-c@example.invalid"
                    }
                    raw_after = json.loads(sql(
                        "SELECT raw_data::text FROM credeals.cre_listings "
                        "WHERE external_id='property/Synthetic-101';", db
                    ).stdout)
                    assert raw_after["contactsDetailed"] == unknown["contacts"]
                    assert raw_after["daumAvailableSpaceObservations"] == unknown["raw_data"]["daumAvailableSpaceObservations"]
                    sql(generated([row(index) for index in range(100)] + [unknown], unknown_claims), db)
                    assert len(children("cre_listing_contacts")) == 5
                if case.startswith("unknown_existing"):
                    raw_after = json.loads(sql(
                        "SELECT raw_data::text FROM credeals.cre_listings "
                        "WHERE external_id='property/Synthetic-101';", db
                    ).stdout)
                    latest = raw_after.pop("latestDaumUnknownTenureObservation")
                    assert latest == unknown["raw_data"]
                    assert latest["daumAvailableSpaceObservations"] == unknown["raw_data"]["daumAvailableSpaceObservations"]
                    assert raw_after == prior_raw
                    freshness_rows = sql(validate.QUERIES["freshness_generations"], db).stdout
                    assert "daum-commercial|strict_detail|current_strict_detail|new-generation|" in freshness_rows, freshness_rows
                    assert "old-generation" not in freshness_rows, freshness_rows
                    assert validate.persisted_freshness_provenance(
                        {**prior_raw, "latestDaumUnknownTenureObservation": unknown["raw_data"]},
                        source_key="daum-commercial",
                    )["generation_id"] == "new-generation"
                    other_source_payload = validate._freshness_payload_sql("l", source_key_expr="'other'")
                    assert sql(
                        "SELECT (" + other_source_payload + ")->'freshnessProvenance'->>'generationId' "
                        "FROM credeals.cre_listings l WHERE external_id='property/Synthetic-101';", db
                    ).stdout.strip() == "old-generation"
                    daum_payload = validate._freshness_payload_sql("l", source_key_expr="'daum-commercial'")
                    for field, value in (("sourceKey", '"other"'), ("daumUnknownTenure", "false")):
                        tampered = (
                            "jsonb_set(raw_data, '{latestDaumUnknownTenureObservation,"
                            + field + "}', '" + value + "'::jsonb)"
                        )
                        assert sql(
                            "SELECT (" + daum_payload + ")->'freshnessProvenance'->>'generationId' "
                            "FROM (SELECT " + tampered + " AS raw_data FROM credeals.cre_listings "
                            "WHERE external_id='property/Synthetic-101') l;", db
                        ).stdout.strip() == "old-generation"
                    assert sql("SELECT title FROM credeals.cre_listings "
                               "WHERE external_id='property/Synthetic-101';", db).stdout.strip() == "Observed current title"
                    contacts = children("cre_listing_contacts")
                    for old_contact in before_children["cre_listing_contacts"]:
                        matched = next(c for c in contacts if c["id"] == old_contact["id"])
                        assert matched["name"] == old_contact["name"]
                        assert matched["title"] == old_contact["title"]
                        assert matched["phone"] == "555-0101"
                        assert matched["is_primary"] == old_contact["is_primary"]
                    assert len(contacts) == 4
                    assert sum(c["is_primary"] is True for c in contacts) == 1
                    assert next(c for c in contacts if c["email"] == "new@example.invalid")["title"] == "first"
                    sql(generated([row(index) for index in range(100)] + [unknown], unknown_claims), db)
                    assert len(children("cre_listing_contacts")) == 4
                    for table in child_tables:
                        if table != "cre_listing_contacts":
                            assert all(item in children(table) for item in before_children[table]), table
            checks.append({"case": case, "passed": True})
    if unknown_mode:
        db = "fixture_unknown_existing"
        # Ordinary DAUM partial rows keep the pre-existing additive precedence.
        known = row(0, fresh=True)
        known["contacts"] = [{"name": "Ordinary new name", "email": "fixture@example.invalid"}]
        known["raw_data"]["preserveChildCollections"] = True
        known["raw_data"]["daumUnknownTenure"] = False
        result = sql(generated([known]), db, check=False)
        assert result.returncode == 0, result.stderr
        assert sql(
            "SELECT name FROM credeals.cre_listing_contacts WHERE listing_id="
            "(SELECT id FROM credeals.cre_listings WHERE external_id='property/Synthetic-0');", db
        ).stdout.strip() == "Ordinary new name"
        checks.append({"case": "ordinary_daum_additive", "passed": True})

        # A foreign brokerage cannot activate DAUM precedence through JSON.
        other = ingest.to_row(
            {"sourceKey": "transwestern", "id": "contact-control",
             "url": "https://example.test/contact-control", "name": "Control",
             "transactionType": "Sale"}, {}, "2026-10-16T00:00:00Z"
        )
        other["contacts"] = [{"name": "Other old", "email": "other@example.invalid"}]
        def generated_other():
            return ingest.build_sql([other], [], "2026-10-16T00:00:00Z", set())
        sql(generated_other(), db)
        other["contacts"][0]["name"] = "Other new"
        other["raw_data"].update({
            "sourceKey": "transwestern",
            "daumUnknownTenure": True,
            "preserveChildCollections": True,
        })
        sql(generated_other(), db)
        assert sql(
            "SELECT c.name FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings l "
            "ON l.id=c.listing_id JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id "
            "WHERE b.slug='transwestern' AND l.external_id='contact-control';", db
        ).stdout.strip() == "Other new"
        checks.append({"case": "foreign_spoof", "passed": True})

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
        "v2_held_lease_empty_title",
        "v2_held_missing_index",
        "v2_held_native_collision",
    ]
    for i, case in enumerate([] if pyramid_mode or unknown_mode else cases):
        db = "fixture_" + str(i)
        sql("CREATE DATABASE " + db + ";")
        sql(schema, db)
        sql(
            generated(
                [
                    row(
                        index,
                        transaction=(
                            "Lease"
                            if case.startswith("v2_") and index == 100
                            else "Sale"
                        ),
                        transaction_mode=(
                            "lease" if case.startswith("v2_") and index == 100 else None
                        ),
                        name=(
                            ""
                            if case.startswith("v2_") and index == 100
                            else "Synthetic"
                        ),
                    )
                    for index in range(102)
                ]
            ),
            db,
        )
        held_fixture_verified = False
        if case.startswith("v2_"):
            held_fixture = sql(
                "SELECT coalesce(transaction_type, '<null>') || '|' "
                "|| coalesce(title, '<null>') "
                "FROM credeals.cre_listings WHERE external_id='property/Synthetic-100';",
                db,
            ).stdout.strip()
            assert held_fixture == "lease|<null>", (case, held_fixture)
            held_fixture_verified = True
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
        if case in {"held_native_collision", "v2_held_native_collision"}:
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
        if case in {"missing_index", "v2_held_missing_index"}:
            sql(
                "DELETE FROM credeals.cre_source_index WHERE external_id='property/Synthetic-"
                + ("100" if case == "v2_held_missing_index" else "0")
                + "';",
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
        protected_tables = {
            "cre_listing_events",
            "cre_listing_om_facts",
            "cre_listing_price_history",
            "cre_source_baseline",
        } | {table for table in tables if table.endswith("_archive")}

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
            for table in [
                "cre_listing_images",
                "cre_listing_contacts",
                "cre_listing_documents",
                "cre_listing_media",
                "cre_listing_links",
            ]:
                result[table] = json.loads(
                    sql(
                        "SELECT jsonb_object_agg(external_id, children)::text FROM ("
                        "SELECT l.external_id, jsonb_agg(to_jsonb(c)-'id'-'created_at'-'scraped_at' "
                        "ORDER BY (to_jsonb(c)-'id'-'created_at'-'scraped_at')::text) children "
                        "FROM credeals." + table + " c JOIN credeals.cre_listings l "
                        "ON l.id=c.listing_id WHERE l.external_id NOT IN "
                        "('property/Synthetic-100','property/Synthetic-101') GROUP BY l.external_id)s;",
                        db,
                    ).stdout
                )
            return result

        children_before = (
            emitted_child_content() if case == "fresh_emitted_children" else None
        )
        before = json.loads(snapshot())
        held_before = protected()
        lifecycle = "SELECT jsonb_agg(jsonb_build_array(l.external_id,l.status,l.deleted_at,si.soft_deleted,si.observation_present,si.presence_generation,si.presence_changed_at) ORDER BY l.external_id)::text FROM credeals.cre_listings l JOIN credeals.cre_source_index si USING(brokerage_id,external_id) WHERE l.external_id NOT IN ('property/Synthetic-100','property/Synthetic-101');"
        life_before = sql(lifecycle, db).stdout
        text = generated(
            [
                row(index, fresh=case == "fresh_emitted_children" and index == 0)
                for index in range(100)
            ],
            claims_v2 if case.startswith("v2_") else claims,
        )
        (out / (case + ".sql")).write_text(text)
        result = sql(text, db, check=False)
        negative = case not in {
            "same",
            "fresh_emitted_children",
            "missing_index",
            "new",
            "missing_old_native",
            "null_old_native",
            "v2_held_lease_empty_title",
            "v2_held_missing_index",
        }
        after = json.loads(snapshot())
        changed_tables = sorted(
            table for table in before if before[table] != after[table]
        )
        unchanged = before == after
        protected_tables_unchanged = all(
            before[table] == after[table] for table in protected_tables
        )
        held_unchanged = protected() == held_before
        lifecycle_unchanged = sql(lifecycle, db).stdout == life_before
        checks.append(
            {
                "case": case,
                "expected_refusal": negative,
                "refused": result.returncode != 0,
                "all_tables_unchanged": unchanged,
                "tables": len(tables),
                "changed_tables": changed_tables,
                "protected_tables_unchanged": protected_tables_unchanged,
                "protected_table_count": len(protected_tables),
                "held_children_index_om_baseline_unchanged": held_unchanged,
                "lifecycle_unchanged": lifecycle_unchanged,
                "v2_partition": (
                    {
                        "members": len(members),
                        "unique_native_ids": len(
                            {member["native_id"] for member in members}
                        ),
                        "unique_external_ids": len(
                            {member["external_id"] for member in members}
                        ),
                        "emitted": sum(member["emitted"] for member in members),
                        "held": sum(not member["emitted"] for member in members),
                    }
                    if case.startswith("v2_")
                    else None
                ),
                "v2_held_fixture_is_lease_with_null_title": (
                    held_fixture_verified if case.startswith("v2_") else None
                ),
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
            assert protected_tables_unchanged, (
                case + " changed protected history or baseline"
            )
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
                    deltas[table] = {
                        "emitted_rows_before": 100,
                        "emitted_rows_after": 100,
                        "changed_identities": changed,
                    }
                checks[-1]["fresh_emitted_child_deltas"] = deltas

            if case not in {"new", "missing_index", "v2_held_missing_index"}:
                assert lifecycle_unchanged, case
            if case == "v2_held_missing_index":
                missing_held_index = sql(
                    "SELECT count(*) FROM credeals.cre_source_index "
                    "WHERE external_id='property/Synthetic-100';",
                    db,
                ).stdout.strip()
                assert missing_held_index == "0", missing_held_index
                checks[-1]["held_missing_index_remains_absent"] = True
        sql("DROP DATABASE " + db + ";")
        sql("CHECKPOINT;")

    if pyramid_mode:
        # Exercise the complete production SQL generator with synthetic Pyramid data.
        def pyramid_row(index, *, fresh=False, transition=False):
            key = f"listings/fixture-{index}"
            url = f"https://www.pyramidbrokerage.com/{key}/"
            tx = "lease" if index % 2 else "sale"
            marker = "fresh" if fresh else "before"
            return ingest.to_row({
                "sourceKey": "pyramid-brokerage", "id": key, "url": url, "canonicalUrl": url,
                "name": "Stable title" if transition else marker, "street": "1 Main Street",
                "city": "Albany", "state": "NY", "description": "Stable description", "assetType": "Industrial",
                "transactionMode": tx, "transactionType": tx.title(),
                "wordpressRecord": {"id": index, "link": url, "status": "publish", "type": "pbc-listings"},
                "photos": [f"https://example.test/{marker}-photo.jpg"] + ([f"https://example.test/{marker}-photo-2.jpg"] if index == 3 else []), "brokerIds": [0],
                "documents": [{"url": f"https://example.test/{marker}-doc.pdf", "docType": "brochure"}],
                "media": [{"url": f"https://example.test/{marker}-tour", "mediaType": "video"}],
                "links": [{"url": f"https://example.test/{marker}-site", "linkType": "other"}],
            }, {0: {"name": marker + " broker", "email": marker + "@example.invalid"}},
                "2026-10-16T00:00:00Z")

        pyramid_members = [{"native_id": index, "external_id": f"listings/fixture-{index}",
            "source_url": f"https://www.pyramidbrokerage.com/listings/fixture-{index}/",
            "transaction": "lease" if index % 2 else "sale", "emitted": index <= 99}
            for index in range(1, 103)]
        member_digest = hashlib.sha256(json.dumps(pyramid_members, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        pyramid_claims = {"schema": "pyramid-verified-subset-v1", "attempt_id": "e" * 32,
            "binding": {"scope": "pyramid_verified_subset_v1", "artifact_sha256": "a" * 64,
                "inventory_count": 102, "emitted_count": 99, "held_count": 3,
                "members_sha256": member_digest}, "members": pyramid_members}
        from cre_pyramid_rollback_result import read_result
        result_binding = {"artifact_sha256": "a" * 64, "artifact_bytes": 123,
            "artifact_run_key": ingest.artifact_run_identity_from_digests(["a" * 64])[0],
            "generation_id": "2026-10-16T000000Z", "generation_started_at": "2026-10-16T00:00:00Z",
            "database_target_sha256": "c" * 64, "attempt_id": "d" * 32,
            "collector_git_sha": "e" * 40}
        def pyramid_sql(rows, subset=False, claims=None, result=False):
            return ingest.build_sql(rows, [{"slug": "pyramid-brokerage", "discovered": len(rows),
                "saved": len(rows), "errors": 0, "notes": "synthetic fixture"}],
                "2026-10-16T00:00:00Z", set(),
                pyramid_verified_subset_claims=(claims or pyramid_claims) if subset else None,
                artifact_run_key=result_binding["artifact_run_key"] if result else None,
                pyramid_guard_binding=result_binding if result else None)
        held_ids = "('listings/fixture-100','listings/fixture-101','listings/fixture-102')"
        for number, case in enumerate(("success", "deleted_emitted", "held_collision", "transition", "transition_mismatch", "transition_ambiguous", "transition_inactive", "transition_held", "transition_no_index", "transition_cross_path", "transition_old_collision", "transition_malformed", "transition_missing_fact")):
            db = "pyramid_fixture_" + str(number)
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db)
            seed = pyramid_sql([pyramid_row(i, transition=i <= 3) for i in range(1, 103)])
            assert "\nCOMMIT;\n" in seed
            sql(seed, db)
            if case == "deleted_emitted":
                sql("UPDATE credeals.cre_listings SET deleted_at=now(),status='inactive' WHERE external_id='listings/fixture-1';", db)
            if case == "held_collision":
                sql("UPDATE credeals.cre_source_index SET url='https://www.pyramidbrokerage.com/listings/wrong/' WHERE external_id='listings/fixture-101';", db)
            if case.startswith("transition"):
                # Historical IDs differ, but exact path and all six facts agree.
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', to_jsonb(900 + split_part(external_id, '-', 2)::int)) WHERE external_id IN ('listings/fixture-1','listings/fixture-2','listings/fixture-3');", db)
            if case == "transition_missing_fact":
                sql("UPDATE credeals.cre_listings SET address=NULL WHERE external_id='listings/fixture-1';", db)
            if case == "transition_mismatch":
                sql("UPDATE credeals.cre_listings SET description='Different' WHERE external_id='listings/fixture-1';", db)
            if case == "transition_ambiguous":
                sql("UPDATE credeals.cre_listings SET raw_data=raw_data || jsonb_build_object('history',jsonb_build_object('wordpressRecord',jsonb_build_object('id',902))) WHERE external_id='listings/fixture-1';", db)
            if case == "transition_inactive":
                sql("UPDATE credeals.cre_listings SET status='inactive' WHERE external_id='listings/fixture-1';", db)
            if case == "transition_no_index":
                sql("DELETE FROM credeals.cre_source_index WHERE external_id='listings/fixture-1';", db)
            if case == "transition_cross_path":
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', '1'::jsonb) WHERE external_id='listings/fixture-4';", db)
            if case == "transition_old_collision":
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', '901'::jsonb) WHERE external_id='listings/fixture-4';", db)
            if case == "transition_malformed":
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', to_jsonb('bad'::text)) WHERE external_id='listings/fixture-1';", db)
            if case == "transition_held":
                sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', '999'::jsonb) WHERE external_id='listings/fixture-101';", db)
            tables = sql("SELECT tablename FROM pg_tables WHERE schemaname='credeals' ORDER BY tablename;", db).stdout.splitlines()
            def all_tables():
                parts = ["SELECT '" + table + "' name,md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) hash FROM credeals." + table + " t" for table in tables]
                return json.loads(sql("SELECT jsonb_object_agg(name,hash)::text FROM (" + " UNION ALL ".join(parts) + ")s;", db).stdout)
            def held_snapshot():
                parts = ["SELECT 'parents' name,jsonb_agg(to_jsonb(t) ORDER BY t.id)::text value FROM credeals.cre_listings t WHERE external_id IN " + held_ids,
                         "SELECT 'index' name,jsonb_agg(to_jsonb(t) ORDER BY t.id)::text value FROM credeals.cre_source_index t WHERE external_id IN " + held_ids]
                for table in ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
                    parts.append("SELECT '" + table + "' name,jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text value FROM credeals." + table +
                        " t JOIN credeals.cre_listings l ON l.id=t.listing_id WHERE l.external_id IN " + held_ids)
                return sql("SELECT jsonb_object_agg(name,value)::text FROM (" + " UNION ALL ".join(parts) + ")s;", db).stdout.strip()
            before = all_tables()
            held_before = held_snapshot()
            held_contents = json.loads(held_before)
            for key in ("parents", "index", "cre_listing_images", "cre_listing_contacts",
                        "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
                assert len(json.loads(held_contents[key])) == 3, (case, key, held_contents[key])
            lifecycle_query = "SELECT jsonb_agg(jsonb_build_array(l.external_id,l.status,l.deleted_at,si.soft_deleted,si.observation_present,si.presence_generation,si.presence_changed_at) ORDER BY l.external_id)::text FROM credeals.cre_listings l JOIN credeals.cre_source_index si USING(brokerage_id,external_id) WHERE l.external_id NOT IN " + held_ids + ";"
            lifecycle_before = sql(lifecycle_query, db).stdout
            staged_rows = [pyramid_row(i, fresh=True, transition=case.startswith("transition") and i <= 3) for i in range(1, 100)]
            from cre_pyramid_identity_probe import probe_sql
            projection = [{**{k: row[k] for k in ("slug", "external_id", "source_url", "title", "address", "city", "state", "transaction_type", "description")}, "native_id": row["raw_data"]["wordpressRecord"]["id"]} for row in staged_rows]
            probe = sql(probe_sql(projection) + ";", db).stdout.strip().split("|")
            assert probe[:2] == ["99", "99"], (case, probe)
            held_by_probe = json.loads(probe[2])
            if case == "transition":
                assert held_by_probe == [], held_by_probe
                assert json.loads(probe[3]) == ["listings/fixture-1", "listings/fixture-2", "listings/fixture-3"]
            if case in {"transition_mismatch", "transition_ambiguous", "transition_inactive", "transition_no_index", "transition_cross_path", "transition_old_collision", "transition_malformed", "transition_missing_fact"}:
                assert "listings/fixture-1" in held_by_probe, (case, held_by_probe)
            text = pyramid_sql(staged_rows, subset=True)
            assert "\nCOMMIT;\n" in text
            (out / ("pyramid-" + case + ".sql")).write_text(text)
            result = sql(text, db, check=False)
            after = all_tables()
            if case in {"success", "transition"}:
                assert result.returncode == 0, result.stderr
                assert held_snapshot() == held_before
                assert sql(lifecycle_query, db).stdout == lifecycle_before
                assert sql("SELECT count(*) FROM credeals.cre_listings WHERE title='fresh';", db).stdout.strip() == ("96" if case == "transition" else "99")
                if case == "transition":
                    for table in ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
                        count = sql("SELECT count(*) FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id IN ('listings/fixture-1','listings/fixture-2','listings/fixture-3');", db).stdout.strip()
                        assert count == ("8" if table == "cre_listing_images" else "6"), (table, count)
                assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id IN " + held_ids + " AND title='before';", db).stdout.strip() == "3"
                if case == "transition":
                    prior = sql("SELECT raw_data->>'pyramidTransitionPrior' FROM credeals.cre_listings WHERE external_id='listings/fixture-1';", db).stdout.strip()
                    assert json.loads(prior)["wordpressRecord"]["id"] == 901
                    # A later ordinary complete refresh must not erase old children.
                    repeat = pyramid_sql([pyramid_row(i, fresh=True, transition=i <= 3)
                                          for i in range(1, 103)])
                    sql(repeat, db)
                    for table in ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
                        count = sql("SELECT count(*) FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id IN ('listings/fixture-1','listings/fixture-2','listings/fixture-3');", db).stdout.strip()
                        assert count == ("8" if table == "cre_listing_images" else "6"), (table, count)
                    assert sql("SELECT raw_data->>'pyramidTransitionPrior' FROM credeals.cre_listings WHERE external_id='listings/fixture-1';", db).stdout.strip() == prior
                    # A further native ID transition retains the original evidence.
                    sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', '1001'::jsonb) WHERE external_id='listings/fixture-1';", db)
                    sql(pyramid_sql([pyramid_row(i, fresh=True, transition=i <= 3) for i in range(1, 100)], subset=True), db)
                    assert sql("SELECT raw_data->>'pyramidTransitionPrior' FROM credeals.cre_listings WHERE external_id='listings/fixture-1';", db).stdout.strip() == prior
                    assert sql("SELECT count(*) FROM credeals.cre_listing_images c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id IN ('listings/fixture-1','listings/fixture-2','listings/fixture-3');", db).stdout.strip() == "8"
                checks.append({"case": "pyramid_" + case, "committed": True,
                    "emitted_updated": 99, "held_parent_five_children_index_unchanged": True,
                    "emitted_lifecycle_unchanged": True})
            else:
                assert result.returncode != 0 and "Pyramid" in result.stderr, (case, result.stderr)
                assert before == after, case + " changed tables despite refusal"
                checks.append({"case": "pyramid_" + case, "refused": True, "all_tables_unchanged": True})
            sql("DROP DATABASE " + db + ";")
            sql("CHECKPOINT;")

        # Execute the complete v2 SQL with two missing-city records: one retains
        # its existing city, while an existing unknown city remains NULL.
        db = "pyramid_incomplete_fixture"
        sql("CREATE DATABASE " + db + ";")
        sql(schema, db)
        sql(pyramid_sql([pyramid_row(i) for i in range(1, 103)]), db)
        sql("UPDATE credeals.cre_listings SET city=NULL,state=NULL,property_type=NULL WHERE external_id='listings/fixture-102';", db)
        sql("UPDATE credeals.cre_listings SET lat=43.0,lng=-78.0 WHERE external_id='listings/fixture-101';", db)
        sql("UPDATE credeals.cre_listings SET lat=44.0,lng=NULL WHERE external_id='listings/fixture-99';", db)
        members = [dict(m, emitted=True, incomplete=m["native_id"] in (101, 102), transition=False,
                        missing_fields=["state", "city", "assetType"] if m["native_id"] in (101, 102) else []) for m in pyramid_members]
        incomplete_claims = {"schema": "pyramid-incomplete-v2", "attempt_id": "f" * 32,
            "binding": {"scope": "pyramid_incomplete_v2", "artifact_sha256": "b" * 64,
                "inventory_count": 102, "emitted_count": 102, "held_count": 0, "incomplete_count": 2, "transition_count": 0,
                "missing_field_counts": {"state": 2, "city": 2, "assetType": 2},
                "members_sha256": hashlib.sha256(json.dumps(members, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()}, "members": members}
        rows = [pyramid_row(i, fresh=True) for i in range(1, 103)]
        for row in rows[-2:]:
            row["city"] = row["state"] = row["property_type"] = None
            row["raw_data"].update(missingFieldEvidence={"fields": ["state", "city", "assetType"], "method": "source_detail_and_taxonomy"},
                preserveChildCollections=True, detailObservedWithChildPreservation=True)
        text = pyramid_sql(rows, subset=True, claims=incomplete_claims)
        result = sql(text, db, check=False)
        assert result.returncode == 0, result.stderr
        assert sql("SELECT coalesce(city,'<NULL>') || '|' || coalesce(state::text,'<NULL>') || '|' || coalesce(property_type,'<NULL>') FROM credeals.cre_listings WHERE external_id IN ('listings/fixture-101','listings/fixture-102') ORDER BY external_id;", db).stdout.splitlines() == ["Albany|NY|industrial", "<NULL>|<NULL>|<NULL>"]
        assert sql("SELECT coalesce(lat::text,'<NULL>') || '|' || coalesce(lng::text,'<NULL>') FROM credeals.cre_listings WHERE external_id IN ('listings/fixture-101','listings/fixture-102','listings/fixture-99') ORDER BY external_id;", db).stdout.splitlines() == ["43|-78", "<NULL>|<NULL>", "44|<NULL>"]
        for table in ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
            count = sql("SELECT count(*) FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id IN ('listings/fixture-101','listings/fixture-102');", db).stdout.strip()
            assert count == "4", (table, count)
        prior = sql("SELECT raw_data->>'pyramidTransitionPrior' FROM credeals.cre_listings WHERE external_id='listings/fixture-101';", db).stdout.strip()
        assert json.loads(prior)["name"] == "before"
        sql(pyramid_sql([pyramid_row(i, fresh=True) for i in range(1, 103)]), db)
        for table in ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
            count = sql("SELECT count(*) FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id IN ('listings/fixture-101','listings/fixture-102');", db).stdout.strip()
            assert count == "4", (table, count)
        checks.append({"case": "pyramid_incomplete", "committed": True, "existing_city_preserved": True,
                       "unknown_city_null": True, "old_and_new_children_preserved": True})
        sql("DROP DATABASE " + db + ";")
        sql("CHECKPOINT;")

        # An old literal-path contradiction with no historical native ID
        # reaches the typed guard after the complete-path classification guard.
        for case in ("historical_conflict", "early_error", "late_error"):
            db = "pyramid_result_" + case
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db)
            sql(pyramid_sql([pyramid_row(i) for i in range(1, 103)]), db)
            if case == "historical_conflict":
                sql("UPDATE credeals.cre_listings SET source_url='https://www.pyramidbrokerage.com/listings/old-path/', raw_data=raw_data - 'wordpressRecord' WHERE external_id='listings/fixture-1';", db)
            before = all_tables()
            text = pyramid_sql([pyramid_row(i, fresh=True) for i in range(1, 103)], result=True)
            if case == "early_error":
                text = text.replace("SELECT EXISTS (\nWITH incoming_pyramid AS MATERIALIZED", "SELECT 1/0;\nSELECT EXISTS (\nWITH incoming_pyramid AS MATERIALIZED", 1)
            if case == "late_error":
                text = text.replace("-- Pyramid preserves historical literal path identities.", "SELECT 1/0;\n-- Pyramid preserves historical literal path identities.", 1)
            result = sql(text, db, check=False)
            assert all_tables() == before, case
            path = out / ("result-" + case + ".log")
            path.write_text(result.stdout)
            if case == "historical_conflict":
                assert result.returncode == 0, result.stderr
                assert read_result(path, result_binding)["outcome"]["outcome"] == "historical_identity_refused"
            else:
                assert result.returncode != 0, (case, result.stderr)
                try:
                    read_result(path, result_binding)
                except ValueError:
                    pass
                else:
                    raise AssertionError(case + " emitted a typed result")
            checks.append({"case": "pyramid_" + case, "all_tables_unchanged": True})
            sql("DROP DATABASE " + db + ";")
            sql("CHECKPOINT;")

        # One changed-ID record with insufficient historical facts is held;
        # two independently valid transitions and incomplete peers still apply.
        db = "pyramid_identity_hold_fixture"
        sql("CREATE DATABASE " + db + ";")
        sql(schema, db)
        sql(pyramid_sql([pyramid_row(i, transition=i <= 3) for i in range(1, 103)]), db)
        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', to_jsonb(900 + split_part(external_id, '-', 2)::int)) WHERE external_id IN ('listings/fixture-1','listings/fixture-2','listings/fixture-3');", db)
        # This held row also has a historical literal-path contradiction.
        # The typed guard must inspect only emitted rows after the v2 partition.
        sql("UPDATE credeals.cre_listings SET address=NULL,source_url='https://www.pyramidbrokerage.com/listings/old-held-path/' WHERE external_id='listings/fixture-1';", db)
        snapshot_sql = "SELECT row_to_json(t)::text FROM credeals.cre_listings t WHERE external_id='listings/fixture-1';"
        held_before = sql(snapshot_sql, db).stdout
        def identity_held_children():
            parts = ["SELECT 'index' name,jsonb_agg(to_jsonb(i)-'scraped_at' ORDER BY i.id)::text value FROM credeals.cre_source_index i WHERE external_id='listings/fixture-1'"]
            for table in ("cre_listing_images", "cre_listing_contacts", "cre_listing_documents", "cre_listing_media", "cre_listing_links"):
                parts.append("SELECT '" + table + "' name,jsonb_agg(to_jsonb(c) ORDER BY to_jsonb(c)::text)::text value FROM credeals." + table + " c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='listings/fixture-1'")
            return sql("SELECT jsonb_object_agg(name,value)::text FROM (" + " UNION ALL ".join(parts) + ")s;", db).stdout
        held_children_before = identity_held_children()
        members = [dict(m, emitted=m["native_id"] != 1, incomplete=m["native_id"] in (101, 102), transition=m["native_id"] in (2, 3),
                        missing_fields=["state", "city", "assetType"] if m["native_id"] in (101, 102) else []) for m in pyramid_members]
        claims = {"schema": "pyramid-incomplete-v2", "attempt_id": "f" * 32,
            "binding": {"scope": "pyramid_incomplete_v2", "artifact_sha256": "b" * 64,
                "inventory_count": 102, "emitted_count": 101, "held_count": 1, "incomplete_count": 2, "transition_count": 2,
                "missing_field_counts": {"state": 2, "city": 2, "assetType": 2},
                "members_sha256": hashlib.sha256(json.dumps(members, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()}, "members": members}
        rows = [pyramid_row(i, fresh=True, transition=i <= 3) for i in range(1, 103)]
        for row in rows[-2:]:
            row["city"] = row["state"] = row["property_type"] = None
            row["raw_data"].update(missingFieldEvidence={"fields": ["state", "city", "assetType"], "method": "source_detail_and_taxonomy"},
                preserveChildCollections=True, detailObservedWithChildPreservation=True)
        projection = [{**{k: row[k] for k in ("slug", "external_id", "source_url", "title", "address", "city", "state", "transaction_type", "description")}, "native_id": row["raw_data"]["wordpressRecord"]["id"]} for row in rows]
        probe = sql(probe_sql(projection) + ";", db).stdout.strip().split("|")
        assert json.loads(probe[2]) == ["listings/fixture-1"], probe
        # An ordinary complete path cannot silently replace children after a
        # native ID changes between a clear probe and the transaction.
        before_complete = all_tables()
        refused = sql(pyramid_sql(rows), db, check=False)
        assert refused.returncode != 0 and "Pyramid unclassified" in refused.stderr, refused.stderr
        assert all_tables() == before_complete
        # A stale clear classification must abort atomically under the locks.
        stale = json.loads(json.dumps(claims))
        stale["members"][0]["emitted"] = True
        stale["binding"].update(emitted_count=102, held_count=0,
            members_sha256=hashlib.sha256(json.dumps(stale["members"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest())
        before_stale = all_tables()
        refused = sql(pyramid_sql(rows, subset=True, claims=stale), db, check=False)
        assert refused.returncode != 0 and "Pyramid" in refused.stderr, refused.stderr
        assert all_tables() == before_stale
        stale_transition = json.loads(json.dumps(claims))
        stale_transition["members"][1]["transition"] = False
        stale_transition["binding"].update(transition_count=1,
            members_sha256=hashlib.sha256(json.dumps(stale_transition["members"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest())
        before_unexpected = all_tables()
        refused = sql(pyramid_sql(rows, subset=True, claims=stale_transition), db, check=False)
        assert refused.returncode != 0 and "Pyramid" in refused.stderr, refused.stderr
        assert all_tables() == before_unexpected
        # A changed parent after a transition probe must not be trusted, even
        # if another writer already replaced its native ID with the new one.
        sql("UPDATE credeals.cre_listings SET address='Different',raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', '2'::jsonb) WHERE external_id='listings/fixture-2';", db)
        before_drift = all_tables()
        refused = sql(pyramid_sql(rows, subset=True, claims=claims), db, check=False)
        assert refused.returncode != 0 and "Pyramid" in refused.stderr, refused.stderr
        assert all_tables() == before_drift
        sql("UPDATE credeals.cre_listings SET address='1 Main Street',raw_data=jsonb_set(raw_data, '{wordpressRecord,id}', '902'::jsonb) WHERE external_id='listings/fixture-2';", db)
        result = sql(pyramid_sql(rows, subset=True, claims=claims, result=True), db, check=False)
        assert result.returncode == 0, result.stderr
        result_path = out / "result-identity-hold.log"
        result_path.write_text(result.stdout)
        assert read_result(result_path, result_binding)["outcome"]["outcome"] == "committed"
        assert sql(snapshot_sql, db).stdout == held_before
        assert identity_held_children() == held_children_before
        assert sql("SELECT status || ':' || listings_saved FROM credeals.cre_scrape_jobs WHERE status='partial' ORDER BY completed_at DESC LIMIT 1;", db).stdout.strip() == "partial:101"
        assert sql("SELECT count(*) FROM credeals.cre_listing_images c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id IN ('listings/fixture-2','listings/fixture-3');", db).stdout.strip() == "6"
        checks.append({"case": "pyramid_identity_hold", "held_parent_unchanged": True,
                       "verified_transitions_and_incomplete_peers_applied": True})
        sql("DROP DATABASE " + db + ";")
        sql("CHECKPOINT;")

        # A previously clear complete-path probe cannot revive a parent or
        # source-index row that changes before the transaction obtains locks.
        for case, mutation in (
            ("complete_parent_stale", "UPDATE credeals.cre_listings SET status='inactive',deleted_at=now() WHERE external_id='listings/fixture-1';"),
            ("complete_index_absent", "UPDATE credeals.cre_source_index SET observation_present=false WHERE external_id='listings/fixture-1';"),
            ("complete_index_deleted", "UPDATE credeals.cre_source_index SET soft_deleted=true WHERE external_id='listings/fixture-1';"),
            ("complete_index_url_changed", "UPDATE credeals.cre_source_index SET url='https://www.pyramidbrokerage.com/listings/wrong/' WHERE external_id='listings/fixture-1';"),
            ("complete_index_source_changed", "UPDATE credeals.cre_source_index SET source_key='other' WHERE external_id='listings/fixture-1';"),
            ("complete_index_collision", "UPDATE credeals.cre_source_index SET url=(SELECT url FROM credeals.cre_source_index WHERE external_id='listings/fixture-1') WHERE external_id='listings/fixture-2';"),
        ):
            db = "pyramid_" + case
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db)
            sql(pyramid_sql([pyramid_row(i) for i in range(1, 103)]), db)
            sql(mutation, db)
            before = all_tables()
            result = sql(pyramid_sql([pyramid_row(i, fresh=True) for i in range(1, 103)]), db, check=False)
            assert result.returncode != 0 and "Pyramid" in result.stderr, (case, result.stderr)
            assert all_tables() == before, case + " mutated tables despite refusal"
            checks.append({"case": case, "refused": True, "all_tables_unchanged": True})
            sql("DROP DATABASE " + db + ";")
            sql("CHECKPOINT;")

        if benchmark_mode:
            db = "pyramid_scale_fixture"
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db)
            sql(pyramid_sql([pyramid_row(i) for i in range(1, 1028)]), db)
            rows = [pyramid_row(i) for i in range(1, 2057)]
            for row in rows:
                row["description"] = "Synthetic detail " + "x" * 500
            projection = [{**{k: row[k] for k in ("slug", "external_id", "source_url", "title", "address", "city", "state", "transaction_type", "description")},
                           "native_id": row["raw_data"]["wordpressRecord"]["id"]} for row in rows]
            query = probe_sql(projection) + ";"
            started_at = time.perf_counter()
            result = sql(query, db)
            elapsed = time.perf_counter() - started_at
            parts = result.stdout.strip().split("|")
            assert parts[:2] == ["2056", "2056"] and json.loads(parts[2]) == [] and json.loads(parts[3]) == [], parts
            checks.append({"case": "pyramid_identity_probe_scale", "staged": 2056, "historical": 1027,
                           "sql_bytes": len(query.encode()), "elapsed_seconds": round(elapsed, 3)})
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
