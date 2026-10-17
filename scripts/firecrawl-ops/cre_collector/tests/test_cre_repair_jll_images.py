"""Contracts for the exact-evidence JLL image repair builder."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

import cre_repair_jll_images as repair
import run_jll_image_repair_postgres_contract as runner


def test_runner_inspect_classifies_missing_container(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "error: no such object: qa-id"),
    )
    assert runner.inspect("qa-id") is None


def test_runner_inspect_does_not_classify_daemon_failure_as_absence(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "Cannot connect to the Docker daemon"
        ),
    )
    with pytest.raises(RuntimeError, match="docker inspect failed"):
        runner.inspect("qa-id")


def test_runner_extracts_full_scale_phase_timing_as_json(tmp_path):
    log = tmp_path / "runner.log"
    log.write_text(
        'JLL_REPAIR_SYNTHETIC_FULL_SCALE {"parents":9985,"images":419538,"candidates":345440,"apply_seconds":12.34,"restore_seconds":15.67}\n',
        encoding="utf-8",
    )
    assert runner.full_scale_timing(log) == {
        "parents": 9985,
        "images": 419538,
        "candidates": 345440,
        "apply_seconds": 12.34,
        "restore_seconds": 15.67,
    }


def candidate(**overrides):
    row = {
        "default_off": True,
        "apply_authorized": False,
        "source_key": "jll",
        "parent_id": "00000000-0000-0000-0000-000000000001",
        "external_id": "fixture-1",
        "image_id": "00000000-0000-0000-0000-000000000101",
        "url": "https://property.jll.com/listings/image-a.jpg",
        "is_primary": False,
        "display_order": 1,
        "expected_before_row_digest": "a" * 32,
        "expected_before_parent_digest": "b" * 32,
        "expected_before_image_collection_digest": "c" * 32,
        "historical_generation": "2026-10-14T055338Z-8da3d563a9b4",
        "historical_cache_relative_path": "cache/jll-detail/fixture.json",
        "historical_cache_sha256": "d" * 64,
        "native_refId": "fixture-1",
        "native_literal_http_reference_count": 1,
        "production_helper_scope": "unknown",
        "reason": (
            "exact_historical_dom_comma_srcset_fragment_removed_by_unchanged_"
            "production_helper_and_not_native_or_retained_dom_reference"
        ),
        "fragment_proof": [
            {
                "attribute": "srcset",
                "srcset_sha256": "e" * 64,
                "complete_candidate_url_count": 2,
            }
        ],
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_key": "jll-investor"},
        {"is_primary": True},
        {"is_primary": None},
        {"display_order": 0},
        {"display_order": None},
        {"default_off": False},
        {"apply_authorized": True},
        {"historical_cache_sha256": "0" * 64},
        {"expected_before_row_digest": "0" * 32},
        {"fragment_proof": []},
    ],
)
def test_candidate_validation_rejects_scope_or_proof_drift(overrides):
    with pytest.raises((ValueError, TypeError)):
        repair.validate_candidate(candidate(**overrides))


def test_candidate_validation_accepts_only_conservative_exact_evidence():
    repair.validate_candidate(candidate())


def test_evidence_hash_rejects_symlink_hardlink_fifo_and_public_input(tmp_path):
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    private_dir.chmod(0o700)
    safe = private_dir / "safe.json"
    safe.write_text("{}", encoding="utf-8")
    safe.chmod(0o600)

    linked = private_dir / "linked.json"
    linked.symlink_to(safe)
    with pytest.raises((ValueError, OSError)):
        repair.file_hash(linked)

    hardlinked = private_dir / "hardlinked.json"
    os.link(safe, hardlinked)
    with pytest.raises((ValueError, OSError)):
        repair.file_hash(hardlinked)

    public = private_dir / "public.json"
    public.write_text("{}", encoding="utf-8")
    public.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        repair.file_hash(public)

    if hasattr(os, "mkfifo"):
        fifo = private_dir / "input.fifo"
        os.mkfifo(fifo, stat.S_IRUSR | stat.S_IWUSR)
        with pytest.raises((ValueError, OSError)):
            repair.file_hash(fifo)


def test_copy_path_rejects_shell_sensitive_or_control_paths():
    with pytest.raises(ValueError):
        repair.copy_path(Path("/tmp/unsafe'path.csv"))
    with pytest.raises(ValueError):
        repair.copy_path(Path("/tmp/unsafe\npath.csv"))


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fixture_evidence(work_dir):
    parent_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    parents = [
        {
            "id": parent_id,
            "brokerage_id": "00000000-0000-0000-0000-000000000010",
            "external_id": f"fixture-{index}",
            "source_url": f"https://property.jll.com/listings/{index}",
            "parent_digest": f"{index:032x}",
            "raw_digest": f"{index + 10:032x}",
        }
        for index, parent_id in enumerate(parent_ids, start=1)
    ]
    images = []
    approved = []
    for index in range(6):
        parent_id = parent_ids[min(index // 3, 1)]
        image_id = f"00000000-0000-0000-0000-{index + 101:012d}"
        url = (
            "https://property.jll.com/listings/native.jpg"
            if index == 3
            else f"https://property.jll.com/listings/bad-{index}.jpg"
        )
        row = {
            "id": image_id,
            "listing_id": parent_id,
            "url": url,
            "alt_text": f"image {index}",
            "is_primary": True if index == 3 else (None if index == 4 else False),
            "display_order": 0 if index == 3 else (None if index == 5 else index + 1),
        }
        images.append(
            {
                "id": image_id,
                "listing_id": parent_id,
                "row_digest": f"{index + 20:032x}",
                "row_json": json.dumps(row, sort_keys=True, separators=(",", ":")),
            }
        )
        if index < 3:
            approved.append(
                {
                    "image_id": image_id,
                    "parent_id": parent_id,
                    "expected_digest": f"{index + 20:032x}",
                }
            )

    write_csv(
        work_dir / "parents.csv",
        ["id", "brokerage_id", "external_id", "source_url", "parent_digest", "raw_digest"],
        parents,
    )
    write_csv(
        work_dir / "images.csv",
        ["id", "listing_id", "row_digest", "row_json"],
        images,
    )
    write_csv(
        work_dir / "candidates.csv",
        ["image_id", "parent_id", "expected_digest"],
        approved,
    )
    pins = repair.EvidencePins(
        proposal_sha256="1" * 64,
        candidates_sha256="2" * 64,
        preimage_sha256="3" * 64,
        candidate_count=3,
        parent_count=2,
        candidate_ids_sha256="4" * 64,
        parent_ids_sha256="5" * 64,
        preimage_parent_count=2,
        preimage_image_count=6,
    )
    return repair.Evidence(
        work_dir=work_dir,
        parent_count=2,
        candidate_count=3,
        pins=asdict(pins),
        image_columns=[
            {"name": "id", "type": "uuid", "not_null": True},
            {"name": "listing_id", "type": "uuid", "not_null": True},
            {"name": "url", "type": "text", "not_null": True},
            {"name": "alt_text", "type": "text", "not_null": False},
            {"name": "is_primary", "type": "boolean", "not_null": False},
            {"name": "display_order", "type": "integer", "not_null": False},
        ],
    )


def test_generated_delete_and_restore_sql_are_child_only_and_exactly_scoped(tmp_path):
    evidence = fixture_evidence(tmp_path)

    delete_sql = repair.build_sql(evidence, operation="delete", commit=False)
    restore_sql = repair.build_sql(evidence, operation="restore", commit=False)

    assert "DELETE FROM credeals.cre_listing_images" in delete_sql
    assert "DELETE FROM credeals.cre_listings" not in delete_sql
    assert "UPDATE credeals.cre_listings" not in delete_sql
    assert "INSERT INTO credeals.cre_listing_images" not in delete_sql
    assert "INSERT INTO credeals.cre_listing_images" in restore_sql
    assert "DELETE FROM credeals.cre_listing_images" not in restore_sql
    for sql in (delete_sql, restore_sql):
        assert "cre_listing_price_history" not in sql
        assert "cre_listing_events" not in sql
        assert "cre_scrape_jobs" not in sql
        assert "cre_source_index" in sql
        assert "pg_advisory_xact_lock" in sql
        assert "ROLLBACK;" in sql
        assert "COMMIT;" not in sql
    assert "bad-0.jpg" not in delete_sql
    assert "native.jpg" not in delete_sql


def _docker(container, *args, input_text=None, check=True, timeout=30):
    result = subprocess.run(
        ["docker", "exec", "-i", container, *args],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"docker exec failed ({result.returncode}): {result.stderr}")
    return result


def _psql(container, sql, *, app_name=None, check=True, timeout=30):
    args = ["psql", "-U", "postgres", "-d", "postgres", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    if app_name:
        args = ["-e", f"PGAPPNAME={app_name}", *args]
    return _docker(container, *args, input_text=sql, check=check, timeout=timeout)


def _assert_test_container(container):
    assert re.fullmatch(r"codex-jll-repair-qa-[a-f0-9]{32}", container), "integration tests require the fresh runner-owned QA container"
    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{json .}}", container],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    record = json.loads(inspect.stdout)
    labels = record["Config"].get("Labels") or {}
    owner = os.environ.get("CRE_REPAIR_PG_TEST_OWNER")
    assert owner and labels.get("codex_jll_repair_owner") == owner
    assert labels.get("codex_jll_repair_qa") == "isolated-test-only"
    assert record["Config"]["Image"] == "postgres:17@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
    config = record["HostConfig"]
    assert config["NetworkMode"] == "none"
    assert config["NanoCpus"] == 500_000_000 and config["Memory"] == 2 * 1024**3
    assert config["PidsLimit"] == 64 and not config.get("PortBindings") and not config.get("Binds")
    assert all(not bindings for bindings in (record["NetworkSettings"].get("Ports") or {}).values())
    tmpfs = config.get("Tmpfs") or {}
    assert list(tmpfs) == ["/var/lib/postgresql/data"]
    assert "size=1536m" in tmpfs["/var/lib/postgresql/data"]


def _reset_schema(container):
    _psql(container, "DROP SCHEMA IF EXISTS credeals CASCADE; CREATE SCHEMA credeals;")
    sql_dir = Path(__file__).resolve().parents[4] / "scripts" / "firecrawl-ops" / "sql"
    _docker(container, "mkdir", "-p", "/tmp/jll-repair-schema")
    copied = subprocess.run(
        ["docker", "cp", str(sql_dir) + "/.", f"{container}:/tmp/jll-repair-schema/"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert copied.returncode == 0, copied.stderr
    migrated = subprocess.run(
        ["docker", "exec", "-w", "/tmp/jll-repair-schema", container,
         "psql", "-U", "postgres", "-d", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "000_run_all.sql"],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert migrated.returncode == 0, migrated.stderr


def _seed_fixture(container):
    _psql(
        container,
        """
INSERT INTO credeals.cre_listings(id, brokerage_id, external_id, source_url, raw_data)
SELECT '00000000-0000-0000-0000-000000000001', id, 'qa-1', 'https://property.jll.com/listings/qa-1', '{"generation":"qa"}'::jsonb
FROM credeals.cre_brokerages WHERE slug='jll';
INSERT INTO credeals.cre_listings(id, brokerage_id, external_id, source_url, raw_data)
SELECT '00000000-0000-0000-0000-000000000002', id, 'qa-2', 'https://property.jll.com/listings/qa-2', '{"generation":"qa"}'::jsonb
FROM credeals.cre_brokerages WHERE slug='jll';
INSERT INTO credeals.cre_listing_images(id,listing_id,url,alt_text,is_primary,display_order) VALUES
('00000000-0000-0000-0000-000000000101','00000000-0000-0000-0000-000000000001','https://property.jll.com/listings/bad-101.jpg','candidate 101',false,1),
('00000000-0000-0000-0000-000000000102','00000000-0000-0000-0000-000000000001','https://property.jll.com/listings/bad-102.jpg',NULL,false,2),
('00000000-0000-0000-0000-000000000103','00000000-0000-0000-0000-000000000001','https://property.jll.com/listings/native-primary.jpg','protected',true,0),
('00000000-0000-0000-0000-000000000201','00000000-0000-0000-0000-000000000002','https://property.jll.com/listings/bad-201.jpg','candidate 201',false,1),
('00000000-0000-0000-0000-000000000202','00000000-0000-0000-0000-000000000002','https://property.jll.com/listings/native-null-primary.jpg','protected',NULL,3),
('00000000-0000-0000-0000-000000000203','00000000-0000-0000-0000-000000000002','https://property.jll.com/listings/native-null-order.jpg','protected',false,NULL);
INSERT INTO credeals.cre_listing_contacts(listing_id,name,email) VALUES ('00000000-0000-0000-0000-000000000001','QA broker','qa@example.invalid');
INSERT INTO credeals.cre_listing_documents(listing_id,doc_type,title,url) VALUES ('00000000-0000-0000-0000-000000000001','om','QA OM','https://property.jll.com/fixture/qa-om.pdf');
WITH job AS (
  INSERT INTO credeals.cre_scrape_jobs(brokerage_id,status,listings_discovered,listings_scraped,listings_saved,notes)
  SELECT id,'completed',2,2,2,'isolated repair contract fixture' FROM credeals.cre_brokerages WHERE slug='jll'
  RETURNING id,brokerage_id
)
INSERT INTO credeals.cre_scrape_log(job_id,listing_id,url,status,http_status)
SELECT job.id,'00000000-0000-0000-0000-000000000001','https://property.jll.com/listings/qa-1','success',200 FROM job;
INSERT INTO credeals.cre_listing_events(listing_id,brokerage_id,event_type,source_url)
SELECT l.id,l.brokerage_id,'new',l.source_url FROM credeals.cre_listings l WHERE l.id='00000000-0000-0000-0000-000000000001';
INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url,observed_status)
SELECT l.brokerage_id,l.external_id,'jll',l.source_url,'active' FROM credeals.cre_listings l WHERE l.id='00000000-0000-0000-0000-000000000001';
INSERT INTO credeals.cre_enrichment_queue(brokerage_id,source_key,external_id,url,reason)
SELECT l.brokerage_id,'jll',l.external_id,l.source_url,'changed' FROM credeals.cre_listings l WHERE l.id='00000000-0000-0000-0000-000000000001';
INSERT INTO credeals.cre_source_baseline(source_key,brokerage_slug,median_active_rows,last_active_rows)
VALUES ('qa-jll','jll',2,2);
INSERT INTO credeals.cre_listing_price_history(listing_id,sale_price_usd,status)
VALUES ('00000000-0000-0000-0000-000000000001',1000000,'active');
INSERT INTO credeals.cre_listing_contacts_archive(source_listing_id,name,email)
VALUES ('00000000-0000-0000-0000-000000000001','Archived QA broker','archive@example.invalid');
INSERT INTO credeals.cre_listing_documents_archive(source_listing_id,doc_type,title,url)
VALUES ('00000000-0000-0000-0000-000000000001','om','Archived QA OM','https://property.jll.com/fixture/archived-qa-om.pdf');
INSERT INTO credeals.cre_listing_media(listing_id,media_type,url)
VALUES ('00000000-0000-0000-0000-000000000001','virtual_tour','https://property.jll.com/fixture/tour');
INSERT INTO credeals.cre_listing_links(listing_id,link_type,url)
VALUES ('00000000-0000-0000-0000-000000000001','map','https://maps.example.invalid/qa');
INSERT INTO credeals.cre_listing_media_archive(source_listing_id,media_type,url)
VALUES ('00000000-0000-0000-0000-000000000001','virtual_tour','https://property.jll.com/fixture/archived-tour');
INSERT INTO credeals.cre_listing_links_archive(source_listing_id,link_type,url)
VALUES ('00000000-0000-0000-0000-000000000001','map','https://maps.example.invalid/archived-qa');
INSERT INTO credeals.cre_listing_om_facts(listing_id,fact_key,fact_value_num,source_doc_url,parser_version)
VALUES ('00000000-0000-0000-0000-000000000001','noi',50000,'https://property.jll.com/fixture/qa-om.pdf','qa-om/1');
INSERT INTO credeals.cre_listing_om_facts_archive(source_listing_id,fact_group,fact_key,fact_value_num,source_doc_url,parser_version)
VALUES ('00000000-0000-0000-0000-000000000001','scalar','noi',50000,'https://property.jll.com/fixture/qa-om.pdf','qa-om/1');
INSERT INTO credeals.cre_zip_cbsa_crosswalk(zip5,county_fips,county_name,state,cbsa_code,cbsa_name)
VALUES ('99999','99999','QA County','TX','99999','QA Metro');
""",
    )


def _query_json(container, sql):
    result = _psql(container, sql)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _snapshot_tables(container):
    table_names = _query_json(
        container,
        "SELECT json_agg(table_name ORDER BY table_name) FROM information_schema.tables WHERE table_schema='credeals' AND table_type='BASE TABLE' AND table_name LIKE 'cre_%';",
    )
    assert len(table_names) == 21, f"expected all 21 canonical CRE tables, found {len(table_names)}"
    branches = [
        f"SELECT '{name}'::text AS name, COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]'::jsonb) AS rows FROM credeals.{name} t"
        for name in table_names
    ]
    return _query_json(container, "SELECT jsonb_object_agg(name, rows) FROM (" + " UNION ALL ".join(branches) + ") s;")


def _stage_live_evidence(container, local_dir):
    parents = _query_json(
        container,
        """SELECT json_agg(json_build_object(
'id',l.id,'brokerage_id',l.brokerage_id,'external_id',l.external_id,'source_url',l.source_url,
'parent_digest',md5(to_jsonb(l)::text),'raw_digest',md5(l.raw_data::text)
) ORDER BY l.id) FROM credeals.cre_listings l WHERE l.id IN ('00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002');""".replace("\n+", "\n"),
    )
    images = _query_json(
        container,
        """SELECT json_agg(json_build_object('id',i.id,'listing_id',i.listing_id,'row_digest',md5(to_jsonb(i)::text),'row_json',to_jsonb(i)) ORDER BY i.id)
FROM credeals.cre_listing_images i WHERE i.listing_id IN ('00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002');""",
    )
    columns = _query_json(
        container,
        """SELECT json_agg(json_build_object('name',attname,'type',format_type(atttypid,atttypmod),'not_null',attnotnull) ORDER BY attnum)
FROM pg_attribute WHERE attrelid='credeals.cre_listing_images'::regclass AND attnum>0 AND NOT attisdropped;""",
    )
    approved_ids = ["00000000-0000-0000-0000-000000000101", "00000000-0000-0000-0000-000000000102", "00000000-0000-0000-0000-000000000201"]
    def id_digest(values):
        return hashlib.sha256("".join(f"{value}\n" for value in sorted(values)).encode()).hexdigest()
    pins = repair.EvidencePins(
        proposal_sha256="1" * 64,
        candidates_sha256="2" * 64,
        preimage_sha256="3" * 64,
        candidate_count=3,
        parent_count=2,
        candidate_ids_sha256=id_digest(approved_ids),
        parent_ids_sha256=id_digest([row["id"] for row in parents]),
        preimage_parent_count=2,
        preimage_image_count=6,
    )
    evidence = repair.Evidence(
        work_dir=Path("/tmp") / f"jll-repair-fixture-{uuid.uuid4().hex}",
        parent_count=2,
        candidate_count=3,
        pins=asdict(pins),
        image_columns=columns,
    )
    evidence.work_dir.mkdir(mode=0o700)
    write_csv(evidence.parents_csv, ["id", "brokerage_id", "external_id", "source_url", "parent_digest", "raw_digest"], parents)
    write_csv(
        evidence.images_csv,
        ["id", "listing_id", "row_digest", "row_json"],
        [{**row, "row_json": json.dumps(row["row_json"], sort_keys=True, separators=(",", ":"))} for row in images],
    )
    write_csv(
        evidence.candidates_csv,
        ["image_id", "parent_id", "expected_digest"],
        [{"image_id": row["id"], "parent_id": row["listing_id"], "expected_digest": row["row_digest"]} for row in images if row["id"] in approved_ids],
    )
    _docker(container, "mkdir", "-p", str(evidence.work_dir))
    for path in (evidence.parents_csv, evidence.images_csv, evidence.candidates_csv):
        copied = subprocess.run(["docker", "cp", str(path), f"{container}:{evidence.work_dir}/{path.name}"], capture_output=True, text=True, check=False, timeout=15)
        assert copied.returncode == 0, copied.stderr
    return evidence


@pytest.fixture
def isolated_pg(tmp_path):
    container = os.environ.get("CRE_REPAIR_PG_TEST_CONTAINER")
    if not container:
        pytest.skip("set CRE_REPAIR_PG_TEST_CONTAINER to the dedicated bounded QA container")
    _assert_test_container(container)
    _reset_schema(container)
    _seed_fixture(container)
    evidence = _stage_live_evidence(container, tmp_path)
    yield container, evidence


def _run_builder_sql(container, evidence, *, operation, commit, check=True, sql=None, timeout=30):
    program = sql if sql is not None else repair.build_sql(evidence, operation=operation, commit=commit)
    return _psql(container, program, check=check, timeout=timeout)


def test_postgres_migration_contract_rollback_apply_restore_exact_21_table_parity(isolated_pg):
    container, evidence = isolated_pg
    baseline = _snapshot_tables(container)
    rollback = _run_builder_sql(container, evidence, operation="delete", commit=False)
    assert "JLL_REPAIR_RESULT:" in rollback.stdout
    assert _snapshot_tables(container) == baseline

    _run_builder_sql(container, evidence, operation="delete", commit=True)
    deleted = _snapshot_tables(container)
    assert deleted["cre_listing_images"] != baseline["cre_listing_images"]
    assert [row["id"] for row in deleted["cre_listing_images"]] == [
        "00000000-0000-0000-0000-000000000103",
        "00000000-0000-0000-0000-000000000202",
        "00000000-0000-0000-0000-000000000203",
    ]
    assert {key: value for key, value in deleted.items() if key != "cre_listing_images"} == {
        key: value for key, value in baseline.items() if key != "cre_listing_images"
    }

    _run_builder_sql(container, evidence, operation="restore", commit=True)
    assert _snapshot_tables(container) == baseline


def test_postgres_readonly_check_runs_and_refuses_changed_image(isolated_pg):
    container, evidence = isolated_pg
    baseline = _snapshot_tables(container)
    checked = _run_builder_sql(container, evidence, operation="check", commit=False)
    assert '"operation": "check"' in checked.stdout
    assert '"committed": false' in checked.stdout
    assert _snapshot_tables(container) == baseline

    _psql(container, "UPDATE credeals.cre_listing_images SET url=url||'-changed' WHERE id='00000000-0000-0000-0000-000000000101';")
    changed = _snapshot_tables(container)
    refused = _run_builder_sql(container, evidence, operation="check", commit=False, check=False)
    assert refused.returncode != 0
    assert _snapshot_tables(container) == changed


@pytest.mark.parametrize(
    "change_sql",
    [
        "UPDATE credeals.cre_listing_images SET url=url||'-changed' WHERE id='00000000-0000-0000-0000-000000000101';",
        "DELETE FROM credeals.cre_listing_images WHERE id='00000000-0000-0000-0000-000000000101';",
        "INSERT INTO credeals.cre_listing_images(id,listing_id,url) VALUES ('00000000-0000-0000-0000-000000000301','00000000-0000-0000-0000-000000000001','https://property.jll.com/listings/extra.jpg');",
        "UPDATE credeals.cre_listings SET raw_data=raw_data||'{\"later\":true}'::jsonb WHERE id='00000000-0000-0000-0000-000000000001';",
        "UPDATE credeals.cre_listing_images SET listing_id='00000000-0000-0000-0000-000000000002' WHERE id='00000000-0000-0000-0000-000000000101';",
    ],
    ids=["changed-candidate", "missing-candidate", "extra-image", "stale-parent", "wrong-target"],
)
def test_postgres_refuses_changed_missing_extra_stale_or_wrong_target(isolated_pg, change_sql):
    container, evidence = isolated_pg
    _psql(container, change_sql)
    before_attempt = _snapshot_tables(container)
    refused = _run_builder_sql(container, evidence, operation="delete", commit=True, check=False)
    assert refused.returncode != 0
    assert _snapshot_tables(container) == before_attempt


def test_postgres_trigger_guard_refuses_without_side_effect(isolated_pg):
    container, evidence = isolated_pg
    _psql(
        container,
        """CREATE TABLE credeals.qa_image_trigger_effect(effect text);
CREATE FUNCTION credeals.qa_image_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO credeals.qa_image_trigger_effect(effect) VALUES ('fired'); RETURN OLD; END $$;
CREATE TRIGGER qa_image_trigger BEFORE DELETE ON credeals.cre_listing_images FOR EACH ROW EXECUTE FUNCTION credeals.qa_image_trigger();""",
    )
    before = _snapshot_tables(container)
    refused = _run_builder_sql(container, evidence, operation="delete", commit=True, check=False)
    assert refused.returncode != 0
    assert _snapshot_tables(container) == before
    effects = _psql(container, "SELECT count(*) FROM credeals.qa_image_trigger_effect;")
    assert effects.stdout.strip() == "0"


def test_postgres_rejects_staged_candidate_identity_tampering(isolated_pg):
    container, evidence = isolated_pg
    with evidence.candidates_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    rows[0]["image_id"] = "00000000-0000-0000-0000-000000000103"  # protected native primary row
    write_csv(evidence.candidates_csv, fields, rows)
    copied = subprocess.run(
        ["docker", "cp", str(evidence.candidates_csv), f"{container}:{evidence.work_dir}/candidates.csv"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert copied.returncode == 0, copied.stderr
    baseline = _snapshot_tables(container)
    refused = _run_builder_sql(container, evidence, operation="delete", commit=True, check=False)
    assert refused.returncode != 0
    assert _snapshot_tables(container) == baseline


def test_postgres_lost_positive_marker_after_commit_is_observably_uncertain(isolated_pg):
    container, evidence = isolated_pg
    baseline = _snapshot_tables(container)
    sql = repair.build_sql(evidence, operation="delete", commit=True)
    sql = sql.rsplit("SELECT 'JLL_REPAIR_RESULT:'", 1)[0] + "SELECT 1/0;\n"
    result = _run_builder_sql(container, evidence, operation="delete", commit=True, check=False, sql=sql)
    assert result.returncode != 0
    after_uncertain = _snapshot_tables(container)
    assert after_uncertain["cre_listing_images"] != baseline["cre_listing_images"]
    # Recovery is explicit and uses the same exact full-preimage restore SQL.
    _run_builder_sql(container, evidence, operation="restore", commit=True)
    assert _snapshot_tables(container) == baseline


def _race_insert_during_pause(container, sql, image_id):
    app_name = f"codex-jll-race-{uuid.uuid4().hex[:12]}"
    command = [
        "docker", "exec", "-i", "-e", f"PGAPPNAME={app_name}", container,
        "psql", "-U", "postgres", "-d", "postgres", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-f", "-",
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write(sql)
        process.stdin.flush()
        process.stdin.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            seen = _psql(
                container,
                f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{app_name}' AND query LIKE '%pg_sleep(3)%';",
            )
            if seen.stdout.strip() == "1":
                break
            if process.poll() is not None:
                raise AssertionError(f"race client exited before gate: {process.stderr.read()}")
            time.sleep(0.05)
        else:
            raise AssertionError("did not observe the isolated repair transaction at the pre-lock pause")
        _psql(
            container,
            f"INSERT INTO credeals.cre_listing_images(id,listing_id,url) VALUES ('{image_id}','00000000-0000-0000-0000-000000000001','https://property.jll.com/listings/raced-extra.jpg');",
        )
        process.wait(timeout=15)
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        return process.returncode, stdout, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _start_pg_session(container, app_name, sql):
    command = [
        "docker", "exec", "-i", "-e", f"PGAPPNAME={app_name}", container,
        "psql", "-U", "postgres", "-d", "postgres", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-f", "-",
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    process.stdin.write(sql)
    process.stdin.flush()
    process.stdin.close()
    return process


def _wait_for_query(container, app_name, pattern, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        seen = _psql(
            container,
            f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{app_name}' AND query LIKE '%{pattern}%';",
        )
        if seen.stdout.strip() == "1":
            return
        if process.poll() is not None:
            raise AssertionError(f"isolated PostgreSQL client exited before gate: {process.stderr.read()}")
        time.sleep(0.05)
    raise AssertionError(f"did not observe isolated PostgreSQL query {pattern!r}")


def _gate_before_locks(sql):
    marker = "SELECT pg_advisory_xact_lock(hashtextextended("
    assert marker in sql
    return sql.replace(marker, "SELECT pg_sleep(3);\n" + marker, 1)


def test_two_session_child_insert_race_is_refused_and_rr_mutation_control_exposes_regression(isolated_pg, tmp_path):
    container, evidence = isolated_pg
    baseline = _snapshot_tables(container)
    rc_sql = _gate_before_locks(repair.build_sql(evidence, operation="delete", commit=True))
    rc_code, _stdout, rc_stderr = _race_insert_during_pause(container, rc_sql, "00000000-0000-0000-0000-000000000301")
    assert rc_code != 0, f"READ COMMITTED transaction should refuse the concurrent extra image: {rc_stderr}"
    after_refusal = _snapshot_tables(container)
    images_by_id = {row["id"]: row for row in after_refusal["cre_listing_images"]}
    assert len(images_by_id) == 7
    assert images_by_id["00000000-0000-0000-0000-000000000301"] == {
        "alt_text": None,
        "display_order": 0,
        "id": "00000000-0000-0000-0000-000000000301",
        "is_primary": False,
        "listing_id": "00000000-0000-0000-0000-000000000001",
        "url": "https://property.jll.com/listings/raced-extra.jpg",
    }
    assert {key: value for key, value in after_refusal.items() if key != "cre_listing_images"} == {
        key: value for key, value in baseline.items() if key != "cre_listing_images"
    }

    # QA-only mutation control: restore the former RR isolation in generated SQL.
    # A row inserted after the RR transaction snapshot is then invisible to its
    # complete-cohort CAS and the candidate deletion commits alongside the extra.
    _reset_schema(container)
    _seed_fixture(container)
    evidence = _stage_live_evidence(container, tmp_path)
    baseline = _snapshot_tables(container)
    rr_sql = repair.build_sql(evidence, operation="delete", commit=True).replace(
        "BEGIN ISOLATION LEVEL READ COMMITTED;", "BEGIN ISOLATION LEVEL REPEATABLE READ;", 1
    )
    rr_sql = _gate_before_locks(rr_sql)
    rr_code, rr_stdout, rr_stderr = _race_insert_during_pause(container, rr_sql, "00000000-0000-0000-0000-000000000301")
    assert rr_code == 0, f"RR regression-control did not reproduce: {rr_stderr}"
    assert "JLL_REPAIR_RESULT:" in rr_stdout
    after_rr = _snapshot_tables(container)
    assert len(after_rr["cre_listing_images"]) == 4
    assert {key: value for key, value in after_rr.items() if key != "cre_listing_images"} == {
        key: value for key, value in baseline.items() if key != "cre_listing_images"
    }
    _psql(container, "DELETE FROM credeals.cre_listing_images WHERE id='00000000-0000-0000-0000-000000000301';")
    _run_builder_sql(container, evidence, operation="restore", commit=True)
    assert _snapshot_tables(container) == baseline


def test_postgres_image_table_lock_timeout_refuses_while_concurrent_writer_holds_lock(isolated_pg):
    container, evidence = isolated_pg
    baseline = _snapshot_tables(container)
    app_name = f"codex-jll-lock-{uuid.uuid4().hex[:12]}"
    holder = _start_pg_session(
        container,
        app_name,
        "BEGIN; LOCK TABLE credeals.cre_listing_images IN ROW EXCLUSIVE MODE; SELECT pg_sleep(8); COMMIT;",
    )
    try:
        _wait_for_query(container, app_name, "pg_sleep(8)", holder)
        refused = _run_builder_sql(container, evidence, operation="delete", commit=True, check=False, timeout=12)
        assert refused.returncode != 0
        assert _snapshot_tables(container) == baseline
        holder.wait(timeout=10)
        assert holder.returncode == 0, holder.stderr.read()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_postgres_cancel_before_commit_rolls_back_atomically(isolated_pg):
    container, evidence = isolated_pg
    baseline = _snapshot_tables(container)
    sql = repair.build_sql(evidence, operation="delete", commit=True)
    marker = "COMMIT;\nSELECT 'JLL_REPAIR_RESULT:'"
    assert marker in sql
    sql = sql.replace(marker, "SELECT pg_sleep(60);\n" + marker, 1)
    app_name = f"codex-jll-cancel-{uuid.uuid4().hex[:12]}"
    client = _start_pg_session(container, app_name, sql)
    try:
        _wait_for_query(container, app_name, "pg_sleep(60)", client)
        cancelled = _psql(
            container,
            f"SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='{app_name}' AND state='active';",
        )
        assert cancelled.stdout.strip() == "t"
        client.wait(timeout=10)
        assert client.returncode != 0
        assert _snapshot_tables(container) == baseline
    finally:
        if client.poll() is None:
            client.kill()
            client.wait(timeout=5)


def test_full_cardinality_synthetic_apply_restore_timing_probe(tmp_path):
    if os.environ.get("CRE_REPAIR_PG_TEST_FULL_SCALE") != "1":
        pytest.skip("set CRE_REPAIR_PG_TEST_FULL_SCALE=1 for the single synthetic cardinality probe")
    container = os.environ.get("CRE_REPAIR_PG_TEST_CONTAINER")
    if not container:
        pytest.skip("set CRE_REPAIR_PG_TEST_CONTAINER to the dedicated bounded QA container")
    _assert_test_container(container)
    _reset_schema(container)
    brokerage_id = _psql(container, "SELECT id FROM credeals.cre_brokerages WHERE slug='jll';").stdout.strip()
    work_dir = Path("/tmp") / f"jll-repair-full-scale-{uuid.uuid4().hex}"
    parent_count, image_count, candidate_count = 9985, 419538, 345440
    parent_ids = [f"00000000-0000-0000-0002-{index:012x}" for index in range(parent_count)]
    candidate_hash, parent_hash = hashlib.sha256(), hashlib.sha256()
    for parent_id in parent_ids:
        parent_hash.update((parent_id + "\n").encode())
    parent_seed = tmp_path / "full-scale-parents.csv"
    image_seed = tmp_path / "full-scale-images.csv"
    with parent_seed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["id", "brokerage_id", "external_id", "source_url", "raw_data"])
        for index, parent_id in enumerate(parent_ids):
            writer.writerow([parent_id, brokerage_id, f"qa-full-{index}", f"https://property.jll.com/fixture/{index}", json.dumps({"fixture": index}, separators=(",", ":"))])
    with image_seed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["id", "listing_id", "url", "alt_text", "is_primary", "display_order"])
        for index in range(image_count):
            image_id = f"00000000-0000-0000-0003-{index:012x}"
            if index < candidate_count:
                candidate_hash.update((image_id + "\n").encode())
            writer.writerow([
                image_id,
                parent_ids[index % parent_count],
                f"https://fixture.invalid/{index}",
                f"synthetic {index}",
                "false",
                1 if index < candidate_count else 0,
            ])
    _docker(container, "mkdir", "-p", str(work_dir))
    _docker(container, "chown", "postgres:postgres", str(work_dir))
    for path in (parent_seed, image_seed):
        copied = subprocess.run(["docker", "cp", str(path), f"{container}:{work_dir}/{path.name}"], capture_output=True, text=True, check=False, timeout=30)
        assert copied.returncode == 0, copied.stderr
    _psql(
        container,
        f"""CREATE TEMP TABLE qa_parents(id uuid,brokerage_id uuid,external_id text,source_url text,raw_data jsonb);
\\copy qa_parents FROM '{work_dir}/full-scale-parents.csv' WITH (FORMAT csv,HEADER true)
INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,raw_data) SELECT id,brokerage_id,external_id,source_url,raw_data FROM qa_parents;
CREATE TEMP TABLE qa_images(id uuid,listing_id uuid,url text,alt_text text,is_primary boolean,display_order integer);
\\copy qa_images FROM '{work_dir}/full-scale-images.csv' WITH (FORMAT csv,HEADER true)
INSERT INTO credeals.cre_listing_images(id,listing_id,url,alt_text,is_primary,display_order) SELECT id,listing_id,url,alt_text,is_primary,display_order FROM qa_images;
CHECKPOINT;
""",
        timeout=120,
    )
    _psql(
        container,
        f"""COPY (SELECT l.id,l.brokerage_id,l.external_id,l.source_url,md5(to_jsonb(l)::text),md5(l.raw_data::text) FROM credeals.cre_listings l WHERE l.id IN (SELECT id FROM credeals.cre_listings WHERE external_id LIKE 'qa-full-%')) TO '{work_dir}/parents.csv' WITH (FORMAT csv,HEADER true);
COPY (SELECT i.id,i.listing_id,md5(to_jsonb(i)::text),to_jsonb(i)::text FROM credeals.cre_listing_images i JOIN credeals.cre_listings l ON l.id=i.listing_id WHERE l.external_id LIKE 'qa-full-%') TO '{work_dir}/images.csv' WITH (FORMAT csv,HEADER true);
COPY (SELECT i.id,i.listing_id,md5(to_jsonb(i)::text) FROM credeals.cre_listing_images i JOIN credeals.cre_listings l ON l.id=i.listing_id WHERE l.external_id LIKE 'qa-full-%' AND i.display_order>0 AND i.is_primary IS FALSE) TO '{work_dir}/candidates.csv' WITH (FORMAT csv,HEADER true);""",
            timeout=120,
        )
    pins = repair.EvidencePins(
        proposal_sha256="1" * 64,
        candidates_sha256="2" * 64,
        preimage_sha256="3" * 64,
        candidate_count=candidate_count,
        parent_count=parent_count,
        candidate_ids_sha256=candidate_hash.hexdigest(),
        parent_ids_sha256=parent_hash.hexdigest(),
        preimage_parent_count=parent_count,
        preimage_image_count=image_count,
    )
    columns = _query_json(container, "SELECT json_agg(json_build_object('name',attname,'type',format_type(atttypid,atttypmod),'not_null',attnotnull) ORDER BY attnum) FROM pg_attribute WHERE attrelid='credeals.cre_listing_images'::regclass AND attnum>0 AND NOT attisdropped;")
    evidence = repair.Evidence(work_dir, parent_count, candidate_count, asdict(pins), columns)
    started = time.monotonic()
    _run_builder_sql(container, evidence, operation="delete", commit=True, timeout=150)
    apply_seconds = time.monotonic() - started
    remaining = int(_psql(container, "SELECT count(*) FROM credeals.cre_listing_images;").stdout.strip())
    assert remaining == image_count - candidate_count
    started = time.monotonic()
    _run_builder_sql(container, evidence, operation="restore", commit=True, timeout=150)
    restore_seconds = time.monotonic() - started
    restored = int(_psql(container, "SELECT count(*) FROM credeals.cre_listing_images;").stdout.strip())
    assert restored == image_count
    print(
        "JLL_REPAIR_SYNTHETIC_FULL_SCALE "
        + json.dumps(
            {
                "parents": parent_count,
                "images": image_count,
                "candidates": candidate_count,
                "apply_seconds": round(apply_seconds, 2),
                "restore_seconds": round(restore_seconds, 2),
            },
            sort_keys=True,
        )
    )
