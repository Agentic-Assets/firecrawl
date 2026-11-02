"""AGENTIC-5143: the documents child-loss guard with site boilerplate excluded, on one owned PG17 fixture.

Opt-in only. No external database URL, credentials, network, ports, image pull, bind mounts,
provider calls or production repair path are accepted. Pattern: run_transwestern_image_transition_postgres_contract.py.

Proves, on real PostgreSQL 17 with the canonical writer (``cre_ingest.build_sql``):
  * the SQL predicate agrees with the shared fixture and with the Python emulation for every URL;
  * a Transwestern-shaped cleanup (the collector stops attaching 4 boilerplate documents per listing)
    trips the old guard (``AND NOT false``) and passes the new one, and the junk is gone afterwards;
  * a collapse of real documents still trips the new guard, even when boilerplate pads the count;
  * the exclusion is host scoped: the same paths on another host still count;
  * the post-commit ``cre_validate`` ``child_counts`` comparison agrees with the precommit guard.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

COLLECTOR = Path(__file__).resolve().parent.parent
REPO = COLLECTOR.parents[2]
STAMP = "2026-11-02T20:00:00Z"
LATER = "2026-11-02T20:01:00Z"
PHOTO = "https://images.transwestern.com/property/primary.jpg"
PARENTS = 12
MIGRATIONS = (
    "001_cre_brokerages.sql", "002_cre_listings.sql", "003_cre_scrape_tracking.sql",
    "004_cre_indexes.sql", "007_cre_change_tracking.sql", "008_cre_fk_indexes.sql",
    "009_cre_history_retention.sql", "011_cre_listing_media.sql",
    "012_cre_listing_institutional_cols.sql", "013_cre_listing_om_facts.sql",
    "006_cre_contact_urls.sql",
)
JUNK_PATHS = (
    "/Upload/TREC/TREC-H-A-SA.pdf",
    "/Upload/TREC/CN%201-2.pdf",
    "/Upload/TREC/TREC-FW-D.pdf",
    "/service/structured-finance",
)


def real_url(identity):
    return f"https://i.transwestern.com/{identity}/om.pdf"


JUNK = [f"https://transwestern.com{path}" for path in JUNK_PATHS]
# Same paths as the boilerplate, on a host the exclusion must not touch.
FOREIGN = [f"https://example-broker.test{path}" for path in JUNK_PATHS]


def listing(identity, documents):
    url = "https://transwestern.com/property/" + identity
    return {
        "sourceKey": "transwestern", "id": identity, "url": url, "canonicalUrl": url,
        "name": "Synthetic contract property " + identity, "transactionType": "Sale",
        "transactionMode": "sale", "photos": [PHOTO],
        "inventoryObservedAt": LATER, "detailObservedAt": LATER,
        "freshnessProvenance": {"method": "transwestern_detail_page", "cacheDisposition": "live"},
        "documents": [{"url": value, "title": "Fixture " + value.rsplit("/", 1)[-1]} for value in documents],
    }


def parents(per_parent):
    identities = [f"p{index:02d}" for index in range(PARENTS)]
    return [listing(identity, per_parent(identity)) for identity in identities]


def cases():
    """before: the artifact that seeds the fixture; after: the artifact the guard judges."""
    with_junk = parents(lambda i: [real_url(i), *JUNK])
    real_only = parents(lambda i: [real_url(i)])
    return [
        dict(name="junk_cleanup_old_guard_trips", before=with_junk, after=real_only,
             old_predicate=True, refuses=True, message="fell more than 30%"),
        dict(name="junk_cleanup_new_guard_passes", before=with_junk, after=real_only,
             symmetric_postcommit=True),
        dict(name="real_collapse_hidden_by_junk_old_guard_passes", before=with_junk,
             after=parents(lambda i: JUNK), old_predicate=True),
        dict(name="real_collapse_hidden_by_junk_new_guard_trips", before=with_junk,
             after=parents(lambda i: JUNK), refuses=True, message="fell more than 30%"),
        dict(name="real_collapse_without_junk_new_guard_trips", before=real_only,
             after=parents(lambda i: []), refuses=True, message="fell more than 30%"),
        dict(name="real_collapse_without_junk_old_guard_trips", before=real_only,
             after=parents(lambda i: []), old_predicate=True, refuses=True, message="fell more than 30%"),
        dict(name="other_host_same_paths_still_counted_new_guard_trips",
             before=parents(lambda i: FOREIGN), after=parents(lambda i: []),
             refuses=True, message="fell more than 30%"),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    args = parser.parse_args()
    if not args.execute_disposable_fixture:
        parser.error("OFF by default; explicit disposable fixture execution required")
    if os.name != "posix" or os.geteuid() == 0 or not (REPO / ".git").is_dir():
        raise RuntimeError("requires nonroot genuine standalone checkout")
    if (REPO / ".env").exists() or any(
        key.startswith(("PG", "POSTGRES", "SUPABASE"))
        or key in {"DATABASE_URL", "CRE_ENV_FILE"} for key in os.environ
    ):
        raise RuntimeError("refusing database environment")
    if (REPO / ".git/objects/info/alternates").exists():
        raise RuntimeError("alternate Git objects are not a standalone clone")
    sys.path.insert(0, str(COLLECTOR))
    import cre_checkpoint_refresh as refresh
    import cre_document_boilerplate as boilerplate
    import cre_ingest as ingest
    import cre_validate

    real_predicate = ingest.document_boilerplate_sql
    fixture = json.loads((COLLECTOR / "tests" / "fixtures" / "document_boilerplate_rules.json").read_text())
    os.umask(0o077)
    args.proof_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    home = args.proof_dir / "home"
    home.mkdir(mode=0o700)
    env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    owner = uuid.uuid4().hex
    label = "cre5143.disposable-owner"
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

    def stored_documents():
        return json.loads(sql("""SELECT coalesce(jsonb_agg(jsonb_build_object(
          'external_id',l.external_id,'url',d.url) ORDER BY l.external_id,d.url),'[]')::text
          FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
          JOIN credeals.cre_listing_documents d ON d.listing_id=l.id
          WHERE b.slug='transwestern' AND l.deleted_at IS NULL;""").stdout)

    def child_counts(query):
        rows = [line.split("|") for line in sql(query).stdout.splitlines() if line]
        return {"queries": {"child_counts": [
            {"source_key": key, "child_type": kind, "count": count} for key, kind, count in rows
        ]}}

    def document_count(validation):
        return next(int(row["count"]) for row in validation["queries"]["child_counts"]
                    if row["source_key"] == "transwestern" and row["child_type"] == "documents")

    def generated(listings, name, *, old_predicate=False):
        rows = [ingest.to_row(value, {}, LATER) for value in listings]
        assert all(rows)
        artifact = json.dumps({"listings": listings}, sort_keys=True, separators=(",", ":")).encode()
        run_key, _ = ingest.artifact_run_identity_from_digests([hashlib.sha256(artifact).hexdigest()])
        jobs = [{"slug": "transwestern", "discovered": len(rows), "saved": len(rows), "errors": 0, "notes": name}]
        # The old guard is the same writer with the exclusion switched off.
        ingest.document_boilerplate_sql = (lambda url_expr: "false") if old_predicate else real_predicate
        try:
            return ingest.build_sql(rows, jobs, STAMP, set(), artifact_run_key=run_key,
                                    finished_at=LATER, skip_post_commit_summary=True)
        finally:
            ingest.document_boilerplate_sql = real_predicate

    try:
        cached = json.loads(command(["docker", "image", "inspect", "postgres:17"], check=True).stdout)[0]
        container = command([
            "docker", "create", "--pull", "never", "--name", "cre5143-" + owner,
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
        (args.proof_dir / "container-inspect.json").write_text(json.dumps({
            "container_id": container, "image_id": actual["Image"], "label": owner,
            "network": actual["HostConfig"]["NetworkMode"],
            "port_bindings": actual["HostConfig"]["PortBindings"],
            "ports": actual["NetworkSettings"]["Ports"], "mounts": actual["Mounts"],
            "tmpfs": actual["HostConfig"]["Tmpfs"],
            "memory": actual["HostConfig"]["Memory"], "pids_limit": actual["HostConfig"]["PidsLimit"],
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
        schema += "\n\\set CRE_APPROVE_LISTING_LIFECYCLE 1\n\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman\n\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5143\n\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'\n"
        schema += (directory / "016_cre_listing_lifecycle.sql").read_text()
        (args.proof_dir / "fixture-schema.sql").write_text(schema)
        sql(schema)

        # 1. The SQL predicate itself, in PostgreSQL, against the shared fixture and the Python emulation.
        urls = [*fixture["denied"], *fixture["kept"], *fixture["tsOnlyDenied"]]
        values = ",".join(f"({ingest.sql_lit(url)})" for url in urls)
        evaluated = dict(line.rsplit("|", 1) for line in sql(
            f"SELECT u.url || '|' || ({boilerplate.document_boilerplate_sql('u.url')})::text FROM (VALUES {values}) AS u(url);"
        ).stdout.splitlines())
        assert len(evaluated) == len(urls)
        for url in fixture["denied"]:
            assert evaluated[url] == "true", ("denied URL not matched by PostgreSQL", url)
        for url in fixture["kept"]:
            assert evaluated[url] == "false", ("kept URL matched by PostgreSQL", url)
        for url in urls:
            assert (evaluated[url] == "true") == boilerplate.matches_boilerplate_url(url), ("PostgreSQL and Python disagree", url)
        null_row = sql(f"SELECT ({boilerplate.document_boilerplate_sql('NULL::text')})::text;").stdout.strip()
        assert null_row == "false"
        checks.append({"case": "predicate_in_postgresql_matches_fixture_and_python",
                       "denied": len(fixture["denied"]), "kept": len(fixture["kept"]),
                       "ts_only_spellings_compared_with_python": len(fixture["tsOnlyDenied"]),
                       "null_url_is_false": True})

        # 2. The writer, with the exclusion on and off.
        for case in cases():
            sql("TRUNCATE credeals.cre_listings,credeals.cre_scrape_jobs,credeals.cre_source_baseline,credeals.cre_source_index CASCADE;")
            sql(generated(case["before"], case["name"] + "-seed"))
            seeded_documents = stored_documents()
            assert len(seeded_documents) == sum(len(item["documents"]) for item in case["before"]), case["name"]
            before_validation = child_counts(cre_validate.QUERIES["child_counts"])
            unexcluded_query = cre_validate.QUERIES["child_counts"].replace(
                f"AND NOT {real_predicate('d.url')}", "AND NOT false")
            assert unexcluded_query != cre_validate.QUERIES["child_counts"]
            before_unexcluded = child_counts(unexcluded_query)
            before_tables = snapshot()
            after_rows = [ingest.to_row(value, {}, LATER) for value in case["after"]]
            query = generated(case["after"], case["name"], old_predicate=case.get("old_predicate", False))
            (args.proof_dir / (case["name"] + ".sql")).write_text(query)
            result = sql(query, check=False)
            after_tables = snapshot()
            if case.get("refuses"):
                assert result.returncode != 0 and before_tables == after_tables, case["name"]
                assert case["message"] in result.stderr, (case["name"], result.stderr)
            else:
                assert result.returncode == 0, (case["name"], result.stderr)
                wanted = sorted(
                    ({"external_id": row["external_id"], "url": doc["url"]} for row in after_rows for doc in row["documents"]),
                    key=lambda row: (row["external_id"], row["url"]),
                )
                assert sorted(stored_documents(), key=lambda row: (row["external_id"], row["url"])) == wanted, case["name"]
            record = {"case": case["name"], "rc": result.returncode,
                      "expected_refusal": case.get("refuses", False),
                      "old_predicate": case.get("old_predicate", False),
                      "all_tables_unchanged": before_tables == after_tables,
                      "documents_before": len(seeded_documents),
                      "documents_after": len(stored_documents())}
            if case.get("symmetric_postcommit"):
                after_validation = child_counts(cre_validate.QUERIES["child_counts"])
                after_unexcluded = child_counts(unexcluded_query)
                # Post-commit comparison: the same exclusion on both sides passes; counting boilerplate would not.
                assert refresh.compare_validation_quality(before_validation, after_validation)["ok"] is True
                assert refresh.compare_validation_quality(before_unexcluded, after_unexcluded)["ok"] is False
                assert document_count(before_validation) == document_count(after_validation) == PARENTS
                assert document_count(before_unexcluded) == PARENTS * 5 and document_count(after_unexcluded) == PARENTS
                record["postcommit_documents_excluding_boilerplate"] = [
                    document_count(before_validation), document_count(after_validation)]
                record["postcommit_documents_counting_boilerplate"] = [
                    document_count(before_unexcluded), document_count(after_unexcluded)]
                record["postcommit_comparison_ok"] = True
            checks.append(record)
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
    print("CRE_DOCUMENT_BOILERPLATE_GUARD_OK " + json.dumps(checks))


if __name__ == "__main__":
    main()
