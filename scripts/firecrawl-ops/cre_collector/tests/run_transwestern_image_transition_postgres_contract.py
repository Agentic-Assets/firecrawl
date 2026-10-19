"""AGENTIC-4576: actual canonical writer against one owned cached PG17 fixture.

Opt-in only. No external database URL, credentials, network, ports, image pull,
bind mounts, provider calls or production repair path are accepted.
"""

import argparse
import collections
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

COLLECTOR = Path(__file__).resolve().parent.parent
REPO = COLLECTOR.parents[2]
STAMP = "2026-10-19T20:00:00Z"
LATER = "2026-10-19T20:01:00Z"
PREFIX = "CRE_TRANSWESTERN_IMAGE_TRANSITION "
PHOTO = "https://images.transwestern.com/property/primary.jpg"
OTHER = "https://images.transwestern.com/property/other.jpg"
MARKER = "https://transwestern.com/assets/images/transwestern-mapmarker.png"
TILES = [f"https://mapsresources-pa.googleapis.com/v1/tiles?fixture={i}" for i in range(12)]
MIGRATIONS = (
    "001_cre_brokerages.sql", "002_cre_listings.sql", "003_cre_scrape_tracking.sql",
    "004_cre_indexes.sql", "007_cre_change_tracking.sql", "008_cre_fk_indexes.sql",
    "009_cre_history_retention.sql", "011_cre_listing_media.sql",
    "012_cre_listing_institutional_cols.sql", "013_cre_listing_om_facts.sql",
    "006_cre_contact_urls.sql",
)


def listing(identity, photos, *, witness=True, excluded=()):
    url = "https://transwestern.com/property/" + identity
    value = {
        "sourceKey": "transwestern", "id": identity, "url": url, "canonicalUrl": url,
        "name": "Synthetic contract property", "transactionType": "Sale", "transactionMode": "sale",
        "photos": photos, "rawTranswesternFeed": {"PropertyImage": photos[0] if photos else None},
        "transwesternPhotoRoleVersion": "v1", "inventoryObservedAt": LATER, "detailObservedAt": LATER,
        "freshnessProvenance": {"method": "transwestern_detail_page", "cacheDisposition": "live"},
        "documents": [{"url": "https://transwestern.com/fixture.pdf", "title": "Fixture"}],
    }
    if witness and photos:
        value["transwesternPrimaryPhotoEvidence"] = {"url": photos[0], "basis": "provider_feed"}
    if excluded:
        value["transwesternExcludedPhotoEvidence"] = {
            "schema": "transwestern-photo-role-v1", "entries": list(excluded),
        }
    return value


def cases():
    old = [listing("a", [*TILES, PHOTO, OTHER])]
    fresh = [listing("a", [PHOTO, OTHER])]
    return [
        dict(name="ordinary_raw_guard_red", before=old, after=fresh, bound=False, refuses=True),
        dict(name="bound_exact_noise_removal", before=old, after=fresh),
        dict(name="same_count_nonnoise_substitution", before=old,
             after=[listing("a", [PHOTO, "https://images.transwestern.com/property/replacement.jpg"])], refuses=True),
        dict(name="physical_nonnoise_duplicate_loss", before=old, after=fresh, duplicate=True, refuses=True),
        dict(name="cross_parent_compensation", before=old + [listing("b", [PHOTO])],
             after=[listing("a", [PHOTO]), listing("b", [PHOTO, OTHER])], refuses=True),
        dict(name="new_parent_cannot_compensate", before=old,
             after=[listing("a", [PHOTO]), listing("new", [OTHER])], refuses=True),
        dict(name="offstage_full_tuples_untouched", before=old + [listing("offstage", [OTHER])], after=fresh),
        dict(name="unknown_prior_primary_retained_after_raw_reorder",
             before=[listing("a", [PHOTO, OTHER, *TILES], witness=False)],
             after=[listing("a", [OTHER, PHOTO], witness=False)], historical_primary=PHOTO),
        dict(name="unknown_missing_protected_primary_refuses",
             before=[listing("a", [PHOTO, OTHER, *TILES], witness=False)],
             after=[listing("a", [OTHER], witness=False)], refuses=True),
        dict(name="excluded_primary_no_witness_becomes_zero", before=[listing("a", [MARKER, *TILES])],
             after=[listing("a", [], witness=False, excluded=[{
                 "url": MARKER, "role": "template_resource", "basis": "existing_source_template_policy",
             }])], zero_primary=True),
        dict(name="new_unknown_only_has_zero_primary", before=old,
             after=fresh + [listing("new", [OTHER], witness=False)], new_zero=True),
        dict(name="ambiguous_prior_primary_refuses", before=old, after=fresh, ambiguous=True, refuses=True),
        dict(name="image_trigger_rolls_back_all_tables", before=old, after=fresh, fail_trigger=True, refuses=True),
        dict(name="job_trigger_rolls_back_all_tables", before=old, after=fresh, fail_job=True, refuses=True),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.execute_disposable_fixture:
        parser.error("OFF by default; explicit disposable fixture execution required")
    if os.name != "posix" or os.geteuid() == 0 or not (REPO / ".git").is_dir():
        raise RuntimeError("requires nonroot genuine standalone Linux checkout")
    if (REPO / ".env").exists() or any(
        key.startswith(("PG", "POSTGRES", "SUPABASE"))
        or key in {"DATABASE_URL", "CRE_ENV_FILE"} for key in os.environ
    ):
        raise RuntimeError("refusing database environment")
    if (REPO / ".git/objects/info/alternates").exists():
        raise RuntimeError("alternate Git objects are not a standalone clone")
    for path in (REPO / ".git/objects").glob("[0-9a-f][0-9a-f]/*"):
        if path.is_file() and path.stat().st_nlink != 1:
            raise RuntimeError("hardlinked Git object")
    sys.path.insert(0, str(COLLECTOR))
    import cre_ingest as ingest
    from cre_transwestern_image_sql import image_state_query
    from cre_transwestern_image_outcome import matches_readback, matches_reports

    if "transwestern_image_transition_binding" not in inspect.signature(ingest.build_sql).parameters:
        raise RuntimeError("writer hook absent; no Docker operation performed")
    os.umask(0o077)
    args.proof_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    home = args.proof_dir / "home"
    home.mkdir(mode=0o700)
    env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    owner = uuid.uuid4().hex
    label = "cre4576.disposable-owner"
    container = None
    transcript, checks = [], []

    def command(argv, *, check=False, timeout=60, **kwargs):
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=timeout, check=check, **kwargs)

    def sql(text, *, check=True):
        result = command([
            "docker", "exec", "--user", "postgres", "-i", container, "psql", "-X", "-qAt",
            "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-",
        ], input="SET statement_timeout='20s'; SET lock_timeout='2s';\n" + text)
        transcript.append({"sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def snapshot():
        names = sql("SELECT tablename FROM pg_tables WHERE schemaname='credeals' ORDER BY tablename;").stdout.splitlines()
        parts = [f"SELECT {ingest.sql_lit(name)} name,coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) rows FROM credeals.{name} t" for name in names]
        return json.loads(sql("SELECT jsonb_object_agg(name,rows)::text FROM (" + " UNION ALL ".join(parts) + ") s;").stdout)

    def images():
        return json.loads(sql("""SELECT coalesce(jsonb_agg(jsonb_build_object(
          'id',i.id,'listing_id',l.id,'external_id',l.external_id,'url',i.url,
          'is_primary',i.is_primary,'display_order',i.display_order,'alt_text',i.alt_text)
          ORDER BY l.external_id,i.url,i.is_primary,i.display_order,i.id),'[]')::text
          FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
          JOIN credeals.cre_listing_images i ON i.listing_id=l.id
          WHERE b.slug='transwestern' AND l.deleted_at IS NULL;""").stdout)

    def generated(listings, name, *, bound=True, mutation=None):
        rows = [ingest.to_row(value, {}, LATER) for value in listings]
        assert all(rows)
        artifact = json.dumps({"listings": listings}, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(artifact).hexdigest()
        run_key, _ = ingest.artifact_run_identity_from_digests([digest])
        sha = command(["git", "--no-optional-locks", "-C", str(REPO), "rev-parse", "HEAD"], check=True).stdout.strip()
        binding = {"artifact_sha256": digest, "artifact_bytes": len(artifact), "artifact_run_key": run_key,
                   "generation_id": "fixture-4576-" + name, "generation_started_at": STAMP,
                   "database_target_sha256": "b" * 64, "attempt_id": hashlib.sha256(name.encode()).hexdigest()[:32],
                   "collector_git_sha": sha}
        if mutation:
            binding.update(mutation)
        jobs = [{"slug": "transwestern", "discovered": len(rows), "saved": len(rows), "errors": 0, "notes": name}]
        options = dict(artifact_run_key=run_key, finished_at=LATER, skip_post_commit_summary=True)
        if bound:
            options["transwestern_image_transition_binding"] = binding
        return rows, binding, ingest.build_sql(rows, jobs, STAMP, set(), **options)

    try:
        cached = json.loads(command(["docker", "image", "inspect", "postgres:17"], check=True).stdout)[0]
        container = command([
            "docker", "create", "--pull", "never", "--name", "cre4576-" + owner,
            "--label", label + "=" + owner, "--network", "none", "--cpus", "1", "--cpuset-cpus", "1",
            "--memory", "512m", "--memory-swap", "512m", "--pids-limit", "128",
            "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
            "--tmpfs", "/var/run/postgresql:rw,size=16m", "--env", "POSTGRES_HOST_AUTH_METHOD=trust", cached["Id"],
        ], check=True).stdout.strip()
        assert len(container) == 64 and all(char in "0123456789abcdef" for char in container)
        command(["docker", "start", container], check=True)
        actual = json.loads(command(["docker", "inspect", container], check=True).stdout)[0]
        assert actual["Image"] == cached["Id"] and actual["Config"]["Labels"][label] == owner
        assert actual["HostConfig"]["NetworkMode"] == "none" and not actual["HostConfig"]["PortBindings"]
        assert all(not mappings for mappings in (actual["NetworkSettings"]["Ports"] or {}).values())
        assert all(mount["Type"] == "tmpfs" for mount in actual["Mounts"])
        assert actual["HostConfig"]["Memory"] == 512 * 1024**2
        assert actual["HostConfig"]["MemorySwap"] == 512 * 1024**2
        assert actual["HostConfig"]["PidsLimit"] == 128
        assert actual["HostConfig"]["NanoCpus"] == 1_000_000_000
        assert actual["HostConfig"]["CpusetCpus"] == "1"
        (args.proof_dir / "container-inspect.json").write_text(json.dumps({
            "container_id": container, "image_id": actual["Image"], "label": owner,
            "network": actual["HostConfig"]["NetworkMode"],
            "port_bindings": actual["HostConfig"]["PortBindings"],
            "ports": actual["NetworkSettings"]["Ports"], "mounts": actual["Mounts"],
            "tmpfs": actual["HostConfig"]["Tmpfs"],
            "memory": actual["HostConfig"]["Memory"], "memory_swap": actual["HostConfig"]["MemorySwap"],
            "pids_limit": actual["HostConfig"]["PidsLimit"],
            "nano_cpus": actual["HostConfig"]["NanoCpus"], "cpuset": actual["HostConfig"]["CpusetCpus"],
        }, indent=2))
        for _ in range(60):
            # The temporary initdb server has only a Unix socket. TCP loopback
            # readiness selects the final server after initdb has settled.
            if command(["docker", "exec", "--user", "postgres", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"]).returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("owned fixture startup did not settle")
        directory = COLLECTOR.parent / "sql"
        schema = "CREATE SCHEMA credeals;\n" + "\n".join((directory / name).read_text() for name in MIGRATIONS)
        schema += "\n\\set CRE_APPROVE_LISTING_LIFECYCLE 1\n\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman\n\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4576\n\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'\n"
        schema += (directory / "016_cre_listing_lifecycle.sql").read_text()
        (args.proof_dir / "fixture-schema.sql").write_text(schema)
        sql(schema)
        for case in cases():
            sql("TRUNCATE credeals.cre_listings,credeals.cre_scrape_jobs,credeals.cre_source_baseline,credeals.cre_source_index CASCADE;")
            _, _, seed = generated(case["before"], case["name"] + "-seed", bound=False)
            sql(seed)
            sql("UPDATE credeals.cre_listing_images SET alt_text='retained role text';")
            if case["name"] == "offstage_full_tuples_untouched":
                sql("UPDATE credeals.cre_listing_images i SET alt_text='retained offstage text',display_order=17 FROM credeals.cre_listings l WHERE l.id=i.listing_id AND l.external_id='offstage';")
            if case.get("duplicate"):
                sql("INSERT INTO credeals.cre_listing_images(listing_id,url,is_primary,display_order,alt_text) SELECT listing_id,url,false,display_order,alt_text FROM credeals.cre_listing_images WHERE url=" + ingest.sql_lit(OTHER) + ";")
            if case.get("ambiguous"):
                sql("UPDATE credeals.cre_listing_images SET is_primary=true WHERE url=" + ingest.sql_lit(OTHER) + ";")
            if case.get("fail_trigger") or case.get("fail_job"):
                table = "cre_scrape_jobs" if case.get("fail_job") else "cre_listing_images"
                sql(f"CREATE FUNCTION credeals.fixture_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture terminal write failure'; END $$; CREATE TRIGGER fixture_fail BEFORE INSERT ON credeals.{table} FOR EACH ROW EXECUTE FUNCTION credeals.fixture_fail();")
            before, old_images = snapshot(), images()
            rows, binding, query = generated(case["after"], case["name"], bound=case.get("bound", True))
            if not case.get("bound", True):
                jobs = [{"slug": "transwestern", "discovered": len(rows), "saved": len(rows), "errors": 0, "notes": case["name"]}]
                assert query == ingest.build_sql(rows, jobs, STAMP, set(), artifact_run_key=binding["artifact_run_key"], finished_at=LATER, skip_post_commit_summary=True, transwestern_image_transition_binding=None)
            (args.proof_dir / (case["name"] + ".sql")).write_text(query)
            result = sql(query, check=False)
            after = snapshot()
            outcomes = [json.loads(line[len(PREFIX):]) for line in result.stdout.splitlines() if line.startswith(PREFIX)]
            if case.get("refuses"):
                assert result.returncode != 0 and before == after and not outcomes, case["name"]
                if case["name"] == "ordinary_raw_guard_red":
                    assert "fell more than 30%" in result.stderr
            else:
                assert result.returncode == 0 and len(outcomes) == 1, (case["name"], result.stderr)
                receipt = outcomes[0]
                assert receipt["source_key"] == "transwestern" and receipt["binding"] == binding
                observed = images()
                for row in rows:
                    expected = collections.Counter((image["url"], image["order"]) for image in row["images"])
                    children = [image for image in observed if image["external_id"] == row["external_id"]]
                    assert collections.Counter((image["url"], image["display_order"]) for image in children) == expected
                    expected_primary = (case.get("historical_primary") or row["images"][0]["url"]) if row["images"] else None
                    if case.get("zero_primary") or (case.get("new_zero") and row["external_id"] == "new"):
                        expected_primary = None
                    assert [image["url"] for image in children if image["is_primary"]] == ([expected_primary] if expected_primary else [])
                    for image in children:
                        old = [prior for prior in old_images if prior["external_id"] == row["external_id"] and prior["url"] == image["url"]]
                        if old:
                            assert image["alt_text"] == old[0]["alt_text"]
                assert [image for image in observed if image["external_id"] == "offstage"] == [image for image in old_images if image["external_id"] == "offstage"]
                old_offstage = {item["id"] for item in before["cre_listings"] if item["external_id"] == "offstage"}
                for table in before:
                    old_rows = [item for item in before[table] if item.get("id") in old_offstage or item.get("listing_id") in old_offstage or item.get("source_listing_id") in old_offstage or item.get("external_id") == "offstage"]
                    new_rows = [item for item in after[table] if item.get("id") in old_offstage or item.get("listing_id") in old_offstage or item.get("source_listing_id") in old_offstage or item.get("external_id") == "offstage"]
                    assert old_rows == new_rows
                # Same production image projection used by final-role readback.
                state = json.loads(sql("SELECT row_to_json(state)::text FROM (" + image_state_query() + ") state;").stdout)
                assert matches_readback(receipt, [state])
                assert matches_reports(receipt, len(old_images), len(observed))
                (args.proof_dir / (case["name"] + "-state.json")).write_text(json.dumps(state))
                assert query.index("COMMIT;") < query.index(PREFIX)
                if case["name"] == "bound_exact_noise_removal":
                    sql("UPDATE credeals.cre_listing_images SET alt_text='postcommit tamper';")
                    tampered = json.loads(sql("SELECT row_to_json(state)::text FROM (" + image_state_query() + ") state;").stdout)
                    assert not matches_readback(receipt, [tampered])
            checks.append({"case": case["name"], "rc": result.returncode,
                           "expected_refusal": case.get("refuses", False), "all_tables_unchanged": before == after,
                           "postcommit_outcomes": len(outcomes)})
            if case.get("fail_trigger") or case.get("fail_job"):
                table = "cre_scrape_jobs" if case.get("fail_job") else "cre_listing_images"
                sql(f"DROP TRIGGER fixture_fail ON credeals.{table}; DROP FUNCTION credeals.fixture_fail();")
        # A syntactically invalid binding and mixed-source rows must refuse before SQL execution.
        for mutation in ({"collector_git_sha": "wrong"}, {"artifact_bytes": True}, {"artifact_run_key": "wrong"}):
            try:
                generated([listing("a", [PHOTO])], "bad-binding", mutation=mutation)
            except ValueError:
                checks.append({"case": "invalid_binding_" + next(iter(mutation)), "refused_before_sql": True})
            else:
                raise AssertionError("invalid binding accepted")
        foreign = listing("foreign", [PHOTO])
        foreign["sourceKey"] = "svn"
        foreign["url"] = foreign["canonicalUrl"] = "https://svn.com/property/foreign"
        try:
            generated([listing("a", [PHOTO]), foreign], "mixed-source")
        except ValueError:
            checks.append({"case": "mixed_source_refused", "refused_before_sql": True})
        else:
            raise AssertionError("mixed-source transition accepted")
    finally:
        (args.proof_dir / "transcript.json").write_text(json.dumps(transcript, indent=2))
        (args.proof_dir / "checks.json").write_text(json.dumps(checks, indent=2))
        if container:
            actual = json.loads(command(["docker", "inspect", container], check=True).stdout)[0]
            assert actual["Config"]["Labels"][label] == owner
            removed = command(["docker", "rm", "-f", container])
            absent = command(["docker", "inspect", container])
            cleanup = {"container": container, "remove_rc": removed.returncode, "absent": absent.returncode != 0}
            (args.proof_dir / "cleanup.json").write_text(json.dumps(cleanup, indent=2))
            assert removed.returncode == 0 and absent.returncode != 0


if __name__ == "__main__":
    main()
