import base64
import hashlib
import json
import os
import re
import stat
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

import cre_repair_cushman_identity as repair


def minimal_state():
    return {
        "listings": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "external_id": "provider-a",
                "source_url": (
                    "https://www.cushmanwakefield.com/en/"
                    "united-states/properties/example"
                ),
                "deleted": False,
                "generation": repair.EXPECTED_GENERATION,
                "updated_at": "2026-07-30T08:24:21+00:00",
                "target_id": "url:v1:00000000000000000000000000000000",
                "identity_url": (
                    "https://www.cushmanwakefield.com/en/"
                    "united-states/properties/example"
                ),
            }
        ],
        "sourceIndex": [],
        "queue": [],
        "geometrySha256": repair.EXPECTED_GEOMETRY_SHA256,
    }


def minimal_artifact():
    return [
        repair.ArtifactRow(
            provider_id="provider-a",
            source_url=(
                "https://www.cushmanwakefield.com/en/"
                "united-states/properties/example"
            ),
            target_id="url:v1:00000000000000000000000000000000",
            transaction_mode="sale",
        )
    ]


def test_reviewed_artifact_loads_and_has_exact_geometry():
    rows = repair.load_artifact(repair.DEFAULT_ARTIFACT)
    assert len(rows) == repair.EXPECTED_ARTIFACT_ROWS
    assert len({row.target_id for row in rows}) == repair.EXPECTED_ARTIFACT_TARGETS
    assert all(row.target_id.startswith("url:v1:") for row in rows)


def test_artifact_hash_and_generation_are_fail_closed(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps(
            {
                "runMeta": {
                    "freshness": {"generationId": repair.EXPECTED_GENERATION}
                },
                "listings": [],
            }
        )
    )
    with pytest.raises(ValueError, match="SHA-256"):
        repair.load_artifact(path)


def test_apply_sql_contains_all_reviewed_surfaces_and_postconditions():
    sql = repair.build_apply_sql(minimal_artifact(), minimal_state())
    required = (
        "pg_advisory_xact_lock",
        "_cw_om_owner",
        "_cw_survivors",
        "cushman-superseded:v1:",
        "cre_listing_contacts",
        "cre_listing_documents",
        "cre_listing_images",
        "cre_listing_om_facts",
        "cre_listing_events",
        "cre_source_index",
        "cre_enrichment_queue",
        "latestInventoryObservation",
        "activeOmFacts",
        "totalParentsPreserved",
    )
    for value in required:
        assert value in sql
    assert "DELETE FROM credeals.cre_listings" not in sql
    assert "DELETE FROM credeals.cre_listing_om_facts" not in sql
    assert "UPDATE credeals.cre_listing_om_facts f SET listing_id" in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_survivor_order_preserves_old_uuid_and_latest_om_owner():
    sql = repair.invariant_sql()
    old_priority = (
        "CASE WHEN r.generation IS DISTINCT FROM "
        f"'{repair.EXPECTED_GENERATION}'"
    )
    om_priority = "CASE WHEN r.id=o.owner_id THEN 0 ELSE 1 END"
    assert old_priority in sql
    assert om_priority in sql
    assert sql.index(old_priority) < sql.index(om_priority)
    assert "count(DISTINCT listing_id)=1" in sql


def test_survivor_projection_has_no_ambiguous_target_id_reference():
    sql = repair.invariant_sql()
    survivor_scope = sql.split(
        "CREATE TEMP TABLE _cw_survivors ON COMMIT DROP AS", 1
    )[1].split(
        "CREATE UNIQUE INDEX ON _cw_survivors(target_id);", 1
    )[0]
    assert "LEFT JOIN _cw_om_owner o" in survivor_scope
    unqualified_target = re.compile(r"(?<![.\w])target_id\b")
    assert unqualified_target.findall(survivor_scope) == []
    prior_failure = survivor_scope.replace(
        "SELECT r.target_id", "SELECT target_id", 1
    )
    assert unqualified_target.findall(prior_failure) == ["target_id"]


def test_source_index_merge_and_queue_rekey_preserve_retry_state():
    sql = repair.apply_body(minimal_artifact(), minimal_state())
    assert "min(first_seen)" not in sql
    assert "max(last_seen)" not in sql
    assert "max(last_enumerated_at)" not in sql
    for field in (
        "source_lastmod",
        "soft_deleted",
        "first_seen",
        "last_seen",
        "last_enumerated_at",
        "prior_sale_price",
        "prior_lease_rate",
        "prior_status",
    ):
        assert f"{field}=r.{field}" in sql
    assert "DELETE FROM credeals.cre_source_index loser" in sql
    queue_section = sql[sql.index("UPDATE credeals.cre_enrichment_queue q") :]
    assert "SET external_id=p.target_id,url=survivor.source_url" in queue_section
    assert "attempts=" not in queue_section
    assert "claimed_at=" not in queue_section


def test_named_stage_payload_reconstructs_exact_geometry_and_digest():
    payload = [
        {
            "id": 1,
            "probe": "'\N{GREEK SMALL LETTER LAMDA};"
            * (repair.PREIMAGE_SQL_CHUNK_BYTES // 4),
        }
    ]
    chunks = repair.named_json_payload_chunks(payload)
    sql = repair.named_json_payload_transport_sql("probe", payload)
    expected = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    )
    expected_bytes = expected.encode("ascii")
    expected_md5 = hashlib.md5(
        expected_bytes, usedforsecurity=False
    ).hexdigest()
    assert "".join(chunks) == expected
    assert len(chunks) > 1
    assert all(
        len(chunk.encode("ascii")) <= repair.PREIMAGE_SQL_CHUNK_BYTES
        for chunk in chunks
    )
    assert f"HAVING count(*)={len(chunks)}" in sql
    assert f"max(seq)={len(chunks) - 1}" in sql
    assert f"octet_length(payload)<>{len(expected_bytes)}" in sql
    assert f"md5(payload)<>'{expected_md5}'" in sql
    assert "string_agg(payload,'' ORDER BY seq)" in sql
    assert "Cushman stage probe chunk geometry mismatch" in sql
    assert "Cushman stage probe payload integrity mismatch" in sql
    assert "Cushman stage probe array/count mismatch" in sql


def test_named_stage_payload_preserves_empty_array_as_one_chunk():
    chunks = repair.named_json_payload_chunks([])
    sql = repair.named_json_payload_transport_sql("probe", [])
    assert chunks == ("[]",)
    assert "VALUES (0,'[]');" in sql
    assert "HAVING count(*)=1" in sql
    assert "max(seq)=0" in sql
    assert "jsonb_array_length(payload))<>0" in sql


def test_named_stage_payload_emits_only_bounded_terminated_inserts():
    payload = [
        {"probe": "'" * (repair.PREIMAGE_SQL_CHUNK_BYTES * 2 + 1)}
    ]
    sql = repair.named_json_payload_transport_sql("probe", payload)
    inserts = [
        line
        for line in sql.splitlines()
        if line.startswith("INSERT INTO _cw_stage_probe_chunks")
    ]
    assert len(inserts) >= 3
    assert all(line.endswith(";") for line in inserts)
    assert all(
        len(line.encode("utf-8"))
        < repair.PREIMAGE_SQL_STATEMENT_CEILING_BYTES
        for line in inserts
    )
    static_sql = "\n".join(
        line for line in sql.splitlines() if line not in inserts
    )
    assert (
        len(static_sql.encode("utf-8"))
        < repair.PREIMAGE_SQL_STATEMENT_CEILING_BYTES
    )
    for terminator in (
        ") ON COMMIT DROP;",
        "$cw_stage_probe$;",
        "$cw_stage_probe_array$;",
        "DROP TABLE _cw_stage_probe_chunks,_cw_stage_probe_assembled;",
    ):
        assert terminator in sql


def test_stage_sql_chunks_all_four_arrays_and_removes_recordset_literals():
    sql = repair.stage_sql(minimal_artifact(), minimal_state())
    destinations = {
        "artifact": "_cw_artifact",
        "listings": "_cw_rows",
        "source_index": "_cw_si_plan",
        "queue": "_cw_queue_plan",
    }
    for name, destination in destinations.items():
        assert f"CREATE TEMP TABLE _cw_stage_{name}_chunks" in sql
        assert f"INSERT INTO _cw_stage_{name}_chunks" in sql
        assert f"(SELECT payload FROM _cw_stage_{name}_payload)" in sql
        assert f"CREATE TEMP TABLE {destination} ON COMMIT DROP AS" in sql
        assert f"Cushman stage {name} array/count mismatch" in sql
    assert not re.search(r"jsonb_to_recordset\s*\(\s*'", sql)
    assert "Cushman staged artifact row count mismatch" in sql
    assert "Cushman staged listings row count mismatch" in sql
    assert "Cushman staged source-index row count mismatch" in sql
    assert "Cushman staged queue row count mismatch" in sql


def test_named_stage_payload_rejects_unsafe_name_and_non_array():
    with pytest.raises(ValueError, match="name is unsafe"):
        repair.named_json_payload_transport_sql("bad-name", [])
    with pytest.raises(TypeError, match="must be an array"):
        repair.named_json_payload_chunks({"not": "an array"})


def test_source_index_donor_is_one_coherent_ranked_tuple():
    older_with_larger_values = {
        "id": "00000000-0000-0000-0000-000000000002",
        "last_enumerated_at": "2026-07-29T12:00:00+00:00",
        "last_seen": "2026-07-29T12:00:00+00:00",
        "soft_deleted": False,
        "source_lastmod": "2026-07-31T00:00:00+00:00",
        "prior_sale_price": 99_000_000,
    }
    freshest = {
        "id": "00000000-0000-0000-0000-000000000001",
        "last_enumerated_at": "2026-07-30T12:00:00+00:00",
        "last_seen": "2026-07-30T12:00:00+00:00",
        "soft_deleted": False,
        "source_lastmod": "2026-07-30T00:00:00+00:00",
        "prior_sale_price": 1_000_000,
    }
    donor = repair.select_source_index_donor(
        [older_with_larger_values, freshest]
    )
    assert donor is freshest
    assert donor["source_lastmod"] == "2026-07-30T00:00:00+00:00"
    assert donor["prior_sale_price"] == 1_000_000


def test_preimage_is_complete_and_rollback_restores_every_fk_surface():
    sql = repair.preimage_sql(minimal_artifact(), minimal_state())
    assert "LEFT JOIN _cw_current current USING(target_id)" in sql
    assert "'schemaVersion',6" in sql
    assert (
        f"'innerEncoding','{repair.PREIMAGE_INNER_ENCODING}'" in sql
    )
    assert "pgp_sym_encrypt(" in sql
    assert "Cushman inner preimage compression/integrity mismatch" in sql
    assert "'innerPayloadPgpBase64'" in sql
    assert "'repairTopology'" in sql
    assert "'stateListings'" in sql
    assert "'url',ranked.url" in sql
    assert "'fact_group',ranked.fact_group" in sql
    assert "'parsed_at',ranked.parsed_at" in sql
    for key in (
        "'listings'",
        "'repairPlan'",
        "'contacts'",
        "'documents'",
        "'images'",
        "'media'",
        "'links'",
        "'omFacts'",
        "'events'",
        "'priceHistory'",
        "'scrapeLogs'",
        "'sourceIndex'",
        "'queue'",
    ):
        assert key in sql
    assert "CREATE TEMP TABLE _cw_preimage_output ON COMMIT DROP" in sql
    assert "Cushman preimage output row count mismatch" in sql
    assert f"payload_bytes>{repair.MAX_PREIMAGE_BYTES}" in sql
    assert (
        f"plaintext_bytes>{repair.MAX_INNER_PREIMAGE_BYTES}" in sql
    )
    assert repair.MAX_PREIMAGE_BYTES == 64 * 1024 * 1024
    assert repair.MAX_INNER_PREIMAGE_BYTES == 128 * 1024 * 1024
    assert "Cushman preimage output integrity mismatch" in sql
    assert "encode(convert_to(payload,'UTF8'),'base64')" in sql
    assert f"'protocol','{repair.PREIMAGE_OUTPUT_PROTOCOL}'" in sql
    assert f"'encoding','{repair.PREIMAGE_OUTPUT_ENCODING}'" in sql
    assert f"FOR {repair.PREIMAGE_OUTPUT_CHUNK_CHARS}" in sql
    assert "generate_series" not in sql
    assert "Cushman preimage output completion guard failed" in sql
    assert "'chunk',''" in sql
    assert repair.PREIMAGE_OUTPUT_MAX_CHUNKS < 400
    payload = reviewed_empty_preimage()
    with (
        patch.object(repair, "EXPECTED_ARTIFACT_ROWS", 0),
        patch.object(repair, "EXPECTED_TOTAL_ROWS", 0),
        patch.object(repair, "EXPECTED_CONTACT_ROWS", 0),
        patch.object(repair, "EXPECTED_DOCUMENT_ROWS", 0),
        patch.object(repair, "EXPECTED_IMAGE_ROWS", 0),
        patch.object(repair, "EXPECTED_OM_FACTS", 0),
        patch.object(repair, "EXPECTED_EVENT_ROWS", 0),
        patch.object(repair, "EXPECTED_SOURCE_INDEX_ROWS", 0),
        patch.object(repair, "EXPECTED_QUEUE_ROWS", 0),
    ):
        rollback = repair.build_rollback_sql(
            payload, minimal_artifact(), minimal_state()
        )
    for table in repair.EXPECTED_FK_TABLES:
        assert table in rollback
    assert "jsonb_populate_recordset" in rollback
    assert "FULL JOIN" in rollback
    assert "post_listing_id" in rollback
    assert "parent post-repair disposition drift" in rollback
    assert "pgp_sym_decrypt(" in rollback
    assert "Cushman rollback inner payload integrity mismatch" in rollback
    assert "SELECT payload::text FROM _cw_rollback_inner" in rollback
    assert "FULL JOIN _cw_rollback_sections actual USING(key)" in rollback
    assert "DROP TABLE _cw_rollback_inner_text,_cw_rollback_inner" in rollback
    assert "raw_data=p.raw_data" in rollback
    assert "l.raw_data IS DISTINCT FROM p.raw_data" in rollback
    assert rollback.rstrip().endswith("COMMIT;")


def test_inner_section_key_contract_is_exact_and_relationally_guarded():
    assert repair.PREIMAGE_INNER_SECTION_KEYS == (
        "schemaVersion",
        "repairPlan",
        "listings",
        "contacts",
        "documents",
        "images",
        "media",
        "links",
        "omFacts",
        "events",
        "priceHistory",
        "scrapeLogs",
        "sourceIndex",
        "queue",
    )
    sql = repair.preimage_sql(minimal_artifact(), minimal_state())
    with patch.multiple(
        repair,
        EXPECTED_ARTIFACT_ROWS=0,
        EXPECTED_TOTAL_ROWS=0,
        EXPECTED_CONTACT_ROWS=0,
        EXPECTED_DOCUMENT_ROWS=0,
        EXPECTED_IMAGE_ROWS=0,
        EXPECTED_OM_FACTS=0,
        EXPECTED_EVENT_ROWS=0,
        EXPECTED_SOURCE_INDEX_ROWS=0,
        EXPECTED_QUEUE_ROWS=0,
    ):
        rollback = repair.build_rollback_sql(
            reviewed_empty_preimage(), minimal_artifact(), minimal_state()
        )
    for statement, table in (
        (sql, "_cw_preimage_inner_sections"),
        (rollback, "_cw_rollback_sections"),
    ):
        assert f"FULL JOIN {table} actual USING(key)" in statement
        assert (
            f"(SELECT count(*) FROM {table})\n"
            f"      <>{len(repair.PREIMAGE_INNER_SECTION_KEYS)}"
        ) in statement
        for key in repair.PREIMAGE_INNER_SECTION_KEYS:
            assert f"('{key}')" in statement
    assert "DROP TABLE _cw_rollback_sections;" in rollback
    assert rollback.index("CREATE UNIQUE INDEX ON _pre_queue(id);") < (
        rollback.index("DROP TABLE _cw_rollback_sections;")
    )
    inner_rollback = rollback.split(
        "CREATE TEMP TABLE _cw_rollback_inner_text", 1
    )[1].split("CREATE TEMP TABLE _cw_rollback_inner", 1)[0]
    assert inner_rollback.count("pgp_sym_decrypt(") == 1
    for key in repair.PREIMAGE_INNER_COUNTS:
        assert (
            f"payload->'{key}' FROM _cw_rollback_inner"
            not in rollback
        )


def test_preimage_payload_never_duplicates_uncompressed_raw_data():
    sql = repair.preimage_sql(minimal_artifact(), minimal_state())
    source_payload = sql.split(
        "WITH source_payload AS MATERIALIZED (", 1
    )[1].split(
        "),\nencoded_payload AS MATERIALIZED (", 1
    )[0]
    assert "'raw_data',l.raw_data" not in source_payload
    for forbidden in (
        "  'listings',(",
        "  'repairPlan',(",
        "  'sourceIndex',(",
        "  'queue',(",
    ):
        assert forbidden not in source_payload
    assert "'innerPayloadPgpBase64'" in source_payload
    assert "'stateListings'" in source_payload
    assert "'repairTopology'" in source_payload
    inner = sql.split(
        "CREATE TEMP TABLE _cw_preimage_inner ON COMMIT DROP AS", 1
    )[1].split(
        "CREATE TEMP TABLE _cw_preimage_inner_readback", 1
    )[0]
    assert "WITH inner_source AS MATERIALIZED (" in inner
    assert "'raw_data',l.raw_data" in inner
    assert "raw_data_pgp_base64" not in sql
    assert inner.count("pgp_sym_encrypt(") == 1


def test_pgcrypto_preflight_is_locked_and_proves_exact_surface():
    preflight = repair.pgcrypto_preflight_sql()
    for signature in (
        "pgp_sym_encrypt(text,text,text)",
        "pgp_sym_decrypt(bytea,text)",
        "digest(bytea,text)",
    ):
        assert f"to_regprocedure('{signature}')" in preflight
    assert repair.PREIMAGE_COMPRESSION_PASSPHRASE in preflight
    assert repair.PREIMAGE_COMPRESSION_PGP_OPTIONS in preflight
    assert "unpacked IS DISTINCT FROM probe" in preflight
    assert "Cushman pgcrypto preflight roundtrip failed" in preflight

    sql = repair.preimage_sql(minimal_artifact(), minimal_state())
    assert sql.index("pg_advisory_xact_lock") < sql.index(
        "Cushman repair requires pgcrypto"
    )
    assert sql.index("Cushman repair requires pgcrypto") < sql.index(
        "CREATE TEMP TABLE _cw_preimage_inner"
    )


def test_preimage_output_uses_one_bounded_frontend_select_per_sequence():
    statements = repair.preimage_output_select_statements()
    assert len(statements) == repair.PREIMAGE_OUTPUT_MAX_CHUNKS
    covered = []
    for expected_seq, statement in enumerate(statements):
        assert statement.endswith(";")
        assert statement.count("SELECT jsonb_build_object(") == 1
        assert f"'seq',{expected_seq}," in statement
        assert f"FROM {expected_seq}*" in statement
        assert f"WHERE {expected_seq}<o.chunk_count;" in statement
        assert "generate_series" not in statement
        covered.append(expected_seq)
    assert covered == list(range(repair.PREIMAGE_OUTPUT_MAX_CHUNKS))

    maximal_envelope = {
        "protocol": repair.PREIMAGE_OUTPUT_PROTOCOL,
        "seq": repair.PREIMAGE_OUTPUT_MAX_CHUNKS - 1,
        "count": repair.PREIMAGE_OUTPUT_MAX_CHUNKS,
        "payloadBytes": repair.MAX_PREIMAGE_BYTES,
        "payloadMd5": "f" * 32,
        "encoding": repair.PREIMAGE_OUTPUT_ENCODING,
        "chunk": "A" * repair.PREIMAGE_OUTPUT_CHUNK_CHARS,
    }
    assert (
        len(
            json.dumps(
                maximal_envelope, separators=(",", ":")
            ).encode("ascii")
        )
        < repair.PREIMAGE_OUTPUT_ROW_CEILING_BYTES
    )


def reviewed_empty_preimage():
    synthetic_inner = b'{"schemaVersion":1}'
    return {
        "schemaVersion": 6,
        "capturedAt": "2026-07-30T18:00:00+00:00",
        "applyTimestampBinding": (
            "updated_at=raw_data.cushmanIdentityRepair.appliedAt="
            "transaction_timestamp"
        ),
        "innerSchemaVersion": repair.PREIMAGE_INNER_SCHEMA_VERSION,
        "innerEncoding": repair.PREIMAGE_INNER_ENCODING,
        "artifactSha256": repair.EXPECTED_ARTIFACT_SHA256,
        "databaseTargetSha256": repair.EXPECTED_DB_TARGET_SHA256,
        "generation": repair.EXPECTED_GENERATION,
        "geometrySha256": repair.EXPECTED_GEOMETRY_SHA256,
        "innerPayloadBytes": len(synthetic_inner),
        "innerPayloadSha256": hashlib.sha256(synthetic_inner).hexdigest(),
        "innerPayloadPgpBase64": base64.b64encode(
            b"synthetic-pgp:" + synthetic_inner
        ).decode(),
        "innerCounts": repair.expected_inner_counts(),
        "artifactPlan": [],
        "repairTopology": [],
        "stateListings": [],
        "stateSourceIndex": [],
        "stateQueue": [],
    }


def post_raw_data(target_id, survivor_id, disposition, raw_data=None):
    marker = {
        "generationId": repair.EXPECTED_GENERATION,
        "canonicalExternalId": target_id,
        "canonicalListingId": survivor_id,
        "disposition": disposition,
        "repairToken": repair.REPAIR_TOKEN,
    }
    result = dict(
        raw_data
        or {
            "freshnessProvenance": {
                "generationId": repair.EXPECTED_GENERATION
            }
        }
    )
    result["cushmanIdentityRepair"] = marker
    return result


def post_raw_data_geometry(raw_data):
    canonical = json.dumps(
        raw_data,
        ensure_ascii=False,
        separators=(", ", ": "),
        sort_keys=True,
    ).encode()
    return {
        "post_raw_data_bytes": len(canonical),
        "post_raw_data_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def raw_output_envelopes(raw):
    encoded = base64.b64encode(raw).decode("ascii")
    chunks = [
        encoded[start : start + repair.PREIMAGE_OUTPUT_CHUNK_CHARS]
        for start in range(
            0, len(encoded), repair.PREIMAGE_OUTPUT_CHUNK_CHARS
        )
    ]
    digest = hashlib.md5(raw, usedforsecurity=False).hexdigest()
    envelopes = [
        {
            "protocol": repair.PREIMAGE_OUTPUT_PROTOCOL,
            "seq": seq,
            "count": len(chunks),
            "payloadBytes": len(raw),
            "payloadMd5": digest,
            "encoding": repair.PREIMAGE_OUTPUT_ENCODING,
            "chunk": chunk,
        }
        for seq, chunk in enumerate(chunks)
    ]
    envelopes.append(
        {
            "protocol": repair.PREIMAGE_OUTPUT_PROTOCOL,
            "seq": len(chunks),
            "count": len(chunks),
            "payloadBytes": len(raw),
            "payloadMd5": digest,
            "encoding": repair.PREIMAGE_OUTPUT_ENCODING,
            "chunk": "",
        }
    )
    return envelopes


def preimage_output_envelopes(payload):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return raw_output_envelopes(raw)


def preimage_output_stdout(envelopes):
    return "\n".join(
        json.dumps(envelope, separators=(",", ":"))
        for envelope in envelopes
    )


def expected_post_state(
    external_id,
    source_url,
    target_id,
    survivor_id,
    disposition,
    raw_data=None,
):
    return {
        "external_id": external_id,
        "source_url": source_url,
        "canonical_url": source_url,
        "status": "active",
        "transaction_type": "sale",
        "property_type": None,
        "title": None,
        "address": None,
        "city": None,
        "state": None,
        "zip": None,
        "country": None,
        "lat": None,
        "lng": None,
        "scraped_at": None,
        "source_lastmod": None,
        "canonical_key": None,
        "deleted_at_static": None,
        "deleted_at_uses_apply_timestamp": disposition
        == "superseded_duplicate",
    }


def current_listing(listing_id, source_url):
    return {
        "id": listing_id,
        "external_id": f"provider-{listing_id[-1]}",
        "source_url": source_url,
        "deleted": False,
        "generation": repair.EXPECTED_GENERATION,
        "updated_at": "2026-07-30T08:24:21+00:00",
    }


def plan_row(listing, survivor_id, source_url):
    listing_id = listing["id"]
    target_id = repair.cushman_canonical_external_id(source_url)
    assert target_id is not None
    is_survivor = listing_id == survivor_id
    external_id = (
        target_id
        if is_survivor
        else "cushman-superseded:v1:"
        + hashlib.md5(
            listing_id.encode(), usedforsecurity=False
        ).hexdigest()
    )
    return {
        "id": listing_id,
        "target_id": target_id,
        "survivor_id": survivor_id,
        "has_current": True,
        "post_external_id": external_id,
        "post_source_url": source_url,
        "post_deleted": not is_survivor,
        "post_generation": repair.EXPECTED_GENERATION,
    }


def patched_review_counts(total, contacts=0):
    return {
        "EXPECTED_ARTIFACT_ROWS": 0,
        "EXPECTED_TOTAL_ROWS": total,
        "EXPECTED_CONTACT_ROWS": contacts,
        "EXPECTED_DOCUMENT_ROWS": 0,
        "EXPECTED_IMAGE_ROWS": 0,
        "EXPECTED_OM_FACTS": 0,
        "EXPECTED_EVENT_ROWS": 0,
        "EXPECTED_SOURCE_INDEX_ROWS": 0,
        "EXPECTED_QUEUE_ROWS": 0,
    }


def empty_child_policy_payload():
    return {
        "contacts": [],
        "documents": [],
        "images": [],
        "media": [],
        "links": [],
        "omFacts": [],
        "events": [],
        "priceHistory": [],
        "scrapeLogs": [],
    }


def test_private_preimage_is_owner_only_and_never_overwritten(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "preimage.json"
    expected_bytes = b'{\n  "ok": true\n}\n'
    digest = repair.atomic_private_json(path, {"ok": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == expected_bytes
    assert digest == hashlib.sha256(expected_bytes).hexdigest()
    with pytest.raises(FileExistsError, match="overwrite"):
        repair.atomic_private_json(path, {"ok": False})
    assert json.loads(path.read_text()) == {"ok": True}


def test_private_preimage_rejects_unsafe_paths_and_oversize_payload(tmp_path):
    relative = Path("relative-preimage.json")
    with pytest.raises(ValueError, match="absolute"):
        repair.atomic_private_json(relative, {"ok": True})

    permissive = tmp_path / "permissive"
    permissive.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="owner-only"):
        repair.atomic_private_json(permissive / "preimage.json", {"ok": True})

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    symlink = tmp_path / "private-link"
    symlink.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        repair.atomic_private_json(symlink / "preimage.json", {"ok": True})

    with patch.object(repair, "MAX_PREIMAGE_BYTES", 1):
        with pytest.raises(ValueError, match="size limit"):
            repair.atomic_private_json(private / "large.json", {"ok": True})


def test_private_preimage_load_requires_exact_hash_and_private_file(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "preimage.json"
    payload = reviewed_empty_preimage()
    zero_counts = {
        "EXPECTED_ARTIFACT_ROWS": 0,
        "EXPECTED_TOTAL_ROWS": 0,
        "EXPECTED_CONTACT_ROWS": 0,
        "EXPECTED_DOCUMENT_ROWS": 0,
        "EXPECTED_IMAGE_ROWS": 0,
        "EXPECTED_OM_FACTS": 0,
        "EXPECTED_EVENT_ROWS": 0,
        "EXPECTED_SOURCE_INDEX_ROWS": 0,
        "EXPECTED_QUEUE_ROWS": 0,
    }
    with patch.multiple(repair, **zero_counts):
        digest = repair.atomic_private_json(path, payload)
        loaded, actual = repair.load_private_preimage(path, digest)
        assert loaded == payload
        assert actual == digest
        with pytest.raises(ValueError, match="does not match"):
            repair.load_private_preimage(path, "0" * 64)
        with pytest.raises(ValueError, match="lowercase hex"):
            repair.load_private_preimage(path, "not-a-digest")
        os.chmod(path, 0o640)
        with pytest.raises(ValueError, match="owner-only"):
            repair.load_private_preimage(path, digest)


def test_rollback_cli_requires_expected_preimage_sha(tmp_path):
    with pytest.raises(SystemExit) as exc:
        repair.main(
            [
                "--env-file",
                "unused.env",
                "--rollback-preimage",
                str(tmp_path / "preimage.json"),
            ]
        )
    assert exc.value.code == 2


def test_preimage_output_chunks_reconstruct_exact_validated_object(monkeypatch):
    monkeypatch.setattr(repair, "PREIMAGE_OUTPUT_CHUNK_CHARS", 64)
    payload = reviewed_empty_preimage()
    payload["innerPayloadPgpBase64"] = base64.b64encode(
        b"synthetic-compressed-inner" * 80
    ).decode()
    stdout = preimage_output_stdout(preimage_output_envelopes(payload))
    with patch.multiple(repair, **patched_review_counts(0)):
        assert repair.parse_preimage_chunk_output(stdout) == payload


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "geometry"),
        ("missing_terminal", "geometry"),
        ("duplicate", "inconsistent"),
        ("reordered", "inconsistent"),
        ("metadata", "inconsistent"),
        ("short_nonfinal", "geometry"),
        ("extra_key", "envelope"),
        ("bool_seq", "envelope"),
        ("invalid_base64", "base64"),
        ("terminal_seq", "completion"),
        ("terminal_chunk", "completion"),
        ("byte_count", "byte-count"),
        ("digest", "digest"),
    ],
)
def test_preimage_output_chunks_reject_partial_or_corrupt_geometry(
    monkeypatch, case, message
):
    monkeypatch.setattr(repair, "PREIMAGE_OUTPUT_CHUNK_CHARS", 32)
    payload = reviewed_empty_preimage()
    payload["transportProbe"] = "bounded-output-" * 20
    envelopes = preimage_output_envelopes(payload)
    assert len(envelopes) > 2
    if case == "missing":
        envelopes.pop(1)
    elif case == "missing_terminal":
        envelopes.pop()
    elif case == "duplicate":
        envelopes[1] = dict(envelopes[0])
    elif case == "reordered":
        envelopes[0], envelopes[1] = envelopes[1], envelopes[0]
    elif case == "metadata":
        envelopes[1]["payloadBytes"] += 1
    elif case == "short_nonfinal":
        envelopes[0]["chunk"] = envelopes[0]["chunk"][:-1]
    elif case == "extra_key":
        envelopes[0]["unexpected"] = True
    elif case == "bool_seq":
        envelopes[0]["seq"] = False
    elif case == "invalid_base64":
        envelopes[-2]["chunk"] = "!" + envelopes[-2]["chunk"][1:]
    elif case == "terminal_seq":
        envelopes[-1]["seq"] -= 1
    elif case == "terminal_chunk":
        envelopes[-1]["chunk"] = "QQ=="
    elif case == "byte_count":
        for envelope in envelopes:
            envelope["payloadBytes"] += 1
    elif case == "digest":
        for envelope in envelopes:
            envelope["payloadMd5"] = "0" * 32
    with (
        patch.multiple(repair, **patched_review_counts(0)),
        pytest.raises(RuntimeError, match=message),
    ):
        repair.parse_preimage_chunk_output(
            preimage_output_stdout(envelopes)
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff", "UTF-8"),
        (b"not-json", "JSON"),
        (b'{"schemaVersion":3}', "schema"),
    ],
)
def test_preimage_output_chunks_reject_invalid_payload(
    monkeypatch, raw, message
):
    monkeypatch.setattr(repair, "PREIMAGE_OUTPUT_CHUNK_CHARS", 64)
    with (
        patch.multiple(repair, **patched_review_counts(0)),
        pytest.raises(RuntimeError, match=message),
    ):
        repair.parse_preimage_chunk_output(
            preimage_output_stdout(raw_output_envelopes(raw))
        )


def test_run_psql_keeps_default_json_parser_and_explicit_chunk_mode(
    monkeypatch
):
    monkeypatch.setattr(repair, "find_psql", lambda: "psql")
    monkeypatch.setattr(repair, "psql_connection_args", lambda _url: [])
    monkeypatch.setattr(repair, "psql_connection_env", lambda _url: {})
    outputs = iter(
        [
            '{"ok":true}\n',
            preimage_output_stdout(
                preimage_output_envelopes(reviewed_empty_preimage())
            ),
        ]
    )

    def succeed(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["psql"],
            returncode=0,
            stdout=next(outputs),
            stderr="",
        )

    monkeypatch.setattr(repair.subprocess, "run", succeed)
    assert repair.run_psql("postgresql://unused", "SELECT 1") == {"ok": True}
    with patch.multiple(repair, **patched_review_counts(0)):
        assert repair.run_psql(
            "postgresql://unused",
            "SELECT chunks",
            result_mode="preimage_chunks",
        ) == reviewed_empty_preimage()
    with pytest.raises(ValueError, match="unsupported"):
        repair.run_psql(
            "postgresql://unused", "SELECT 1", result_mode="auto"
        )


def test_run_psql_reports_rollback_verification_timeout(monkeypatch):
    monkeypatch.setattr(repair, "find_psql", lambda: "psql")
    monkeypatch.setattr(repair, "psql_connection_args", lambda _url: [])
    monkeypatch.setattr(repair, "psql_connection_env", lambda _url: {})

    def time_out(*_args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="psql", timeout=kwargs["timeout"]
        )

    monkeypatch.setattr(repair.subprocess, "run", time_out)
    with pytest.raises(
        RuntimeError,
        match=(
            r"rollback-only verification exceeded its 1800s "
            r"client wall-clock timeout"
        ),
    ):
        repair.run_psql(
            "postgresql://unused",
            "ROLLBACK;",
            timeout_seconds=repair.ROLLBACK_VERIFICATION_TIMEOUT_SECONDS,
        )


def test_roundtrip_cli_times_only_noncommitting_preimage_and_roundtrip(
    monkeypatch, capsys
):
    calls = []
    preimage = reviewed_empty_preimage()

    def fake_run_psql(
        _db_url, sql, timeout_seconds=None, result_mode="json"
    ):
        calls.append((sql, timeout_seconds, result_mode))
        if sql == "PREIMAGE":
            return preimage
        return {"ok": True}

    monkeypatch.setattr(
        repair, "load_db_url", lambda _path: ("postgresql://unused", {})
    )
    monkeypatch.setattr(repair, "assert_db_target", lambda _url: None)
    monkeypatch.setattr(
        repair, "shared_cre_lock", lambda _path: nullcontext()
    )
    monkeypatch.setattr(repair, "load_artifact", lambda _path: minimal_artifact())
    monkeypatch.setattr(repair, "load_live_state", lambda _url: minimal_state())
    monkeypatch.setattr(repair, "preflight_sql", lambda *_args: "PREFLIGHT")
    monkeypatch.setattr(repair, "preimage_sql", lambda *_args: "PREIMAGE")
    monkeypatch.setattr(
        repair, "build_roundtrip_sql", lambda *_args: "ROUNDTRIP"
    )
    monkeypatch.setattr(repair, "run_psql", fake_run_psql)

    assert (
        repair.main(
            ["--env-file", "unused.env", "--verify-rollback-roundtrip"]
        )
        == 0
    )
    assert calls == [
        ("PREFLIGHT", None, "json"),
        (
            "PREIMAGE",
            repair.ROLLBACK_VERIFICATION_TIMEOUT_SECONDS,
            "preimage_chunks",
        ),
        (
            "ROUNDTRIP",
            repair.ROLLBACK_VERIFICATION_TIMEOUT_SECONDS,
            "json",
        ),
    ]
    assert '"mode": "verify_rollback_roundtrip"' in capsys.readouterr().out


def test_apply_rollback_verification_uses_client_timeout(
    monkeypatch, capsys
):
    calls = []

    def fake_run_psql(
        _db_url, sql, timeout_seconds=None, result_mode="json"
    ):
        calls.append((sql, timeout_seconds, result_mode))
        return {"ok": True}

    monkeypatch.setattr(
        repair, "load_db_url", lambda _path: ("postgresql://unused", {})
    )
    monkeypatch.setattr(repair, "assert_db_target", lambda _url: None)
    monkeypatch.setattr(
        repair, "shared_cre_lock", lambda _path: nullcontext()
    )
    monkeypatch.setattr(repair, "load_artifact", lambda _path: minimal_artifact())
    monkeypatch.setattr(repair, "load_live_state", lambda _url: minimal_state())
    monkeypatch.setattr(repair, "preflight_sql", lambda *_args: "PREFLIGHT")
    monkeypatch.setattr(
        repair,
        "build_apply_sql",
        lambda *_args: "BEGIN ISOLATION LEVEL SERIALIZABLE;\nSELECT 1;\nCOMMIT;\n",
    )
    monkeypatch.setattr(repair, "run_psql", fake_run_psql)

    assert (
        repair.main(
            ["--env-file", "unused.env", "--verify-apply-rollback"]
        )
        == 0
    )
    assert calls == [
        ("PREFLIGHT", None, "json"),
        (
            "BEGIN ISOLATION LEVEL SERIALIZABLE;\nSELECT 1;\nROLLBACK;\n",
            repair.ROLLBACK_VERIFICATION_TIMEOUT_SECONDS,
            "json",
        ),
    ]
    assert '"mode": "verify_apply_rollback"' in capsys.readouterr().out


def test_persistent_apply_and_explicit_rollback_have_no_client_timeout(
    monkeypatch, tmp_path
):
    calls = []
    preimage = reviewed_empty_preimage()

    def fake_run_psql(
        _db_url, sql, timeout_seconds=None, result_mode="json"
    ):
        calls.append((sql, timeout_seconds, result_mode))
        if sql == "PREIMAGE":
            return preimage
        return {"ok": True}

    monkeypatch.setattr(
        repair, "load_db_url", lambda _path: ("postgresql://unused", {})
    )
    monkeypatch.setattr(repair, "assert_db_target", lambda _url: None)
    monkeypatch.setattr(
        repair, "shared_cre_lock", lambda _path: nullcontext()
    )
    monkeypatch.setattr(repair, "load_artifact", lambda _path: minimal_artifact())
    monkeypatch.setattr(repair, "load_live_state", lambda _url: minimal_state())
    monkeypatch.setattr(repair, "preflight_sql", lambda *_args: "PREFLIGHT")
    monkeypatch.setattr(repair, "preimage_sql", lambda *_args: "PREIMAGE")
    monkeypatch.setattr(repair, "validate_preimage", lambda payload: payload)
    monkeypatch.setattr(repair, "build_apply_sql", lambda *_args: "APPLY")
    monkeypatch.setattr(repair, "run_psql", fake_run_psql)
    monkeypatch.setattr(
        repair, "atomic_private_json", lambda *_args: "0" * 64
    )

    assert (
        repair.main(
            [
                "--env-file",
                "unused.env",
                "--apply",
                "--preimage",
                str(tmp_path / "preimage.json"),
            ]
        )
        == 0
    )
    assert calls == [
        ("PREFLIGHT", None, "json"),
        ("PREIMAGE", None, "preimage_chunks"),
        ("APPLY", None, "json"),
    ]

    calls.clear()
    monkeypatch.setattr(
        repair,
        "load_private_preimage",
        lambda *_args: (preimage, "0" * 64),
    )
    monkeypatch.setattr(
        repair, "artifact_from_preimage", lambda _payload: minimal_artifact()
    )
    monkeypatch.setattr(
        repair, "state_from_preimage", lambda _payload: minimal_state()
    )
    monkeypatch.setattr(repair, "build_rollback_sql", lambda *_args: "ROLLBACK")
    assert (
        repair.main(
            [
                "--env-file",
                "unused.env",
                "--rollback-preimage",
                str(tmp_path / "preimage.json"),
                "--expected-preimage-sha256",
                "0" * 64,
            ]
        )
        == 0
    )
    assert calls == [("ROLLBACK", None, "json")]


@pytest.mark.parametrize(
    "mode_args",
    [
        ["--apply", "--preimage", "/tmp/cushman-preimage.json"],
        [
            "--rollback-preimage",
            "/tmp/cushman-preimage.json",
            "--expected-preimage-sha256",
            "0" * 64,
        ],
        ["--verify-apply-rollback"],
        ["--verify-rollback-roundtrip"],
    ],
)
def test_alternate_lock_path_is_rejected_before_database_access(mode_args):
    with (
        patch.object(repair, "load_db_url") as load_db_url,
        pytest.raises(SystemExit) as exc,
    ):
        repair.main(
            [
                "--env-file",
                "unused.env",
                "--lock-dir",
                "/tmp/not-the-canonical-cre-lock",
                *mode_args,
            ]
        )
    assert exc.value.code == 2
    load_db_url.assert_not_called()


def test_preimage_validation_rejects_metadata_and_count_drift():
    payload = reviewed_empty_preimage()
    with patch.object(repair, "EXPECTED_ARTIFACT_ROWS", 0):
        with pytest.raises(ValueError, match="repairTopology count"):
            repair.validate_preimage(payload)
    payload["artifactSha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifactSha256"):
        repair.validate_preimage(payload)
    payload = reviewed_empty_preimage()
    payload["schemaVersion"] = 5
    with pytest.raises(ValueError, match="schemaVersion"):
        repair.validate_preimage(payload)


@pytest.mark.parametrize(
    "legacy_key",
    ["repairPlan", "listings", "omFacts", "sourceIndex", "queue"],
)
def test_preimage_validation_rejects_legacy_clear_state_arrays(legacy_key):
    payload = reviewed_empty_preimage()
    payload[legacy_key] = [{"oversizedClearState": "x"}]
    with (
        patch.multiple(repair, **patched_review_counts(0)),
        pytest.raises(ValueError, match="outer schema"),
    ):
        repair.validate_preimage(payload)


def test_preimage_validation_rejects_invalid_captured_listing_shape():
    payload = reviewed_empty_preimage()
    payload["stateListings"] = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "external_id": "provider-a",
            "raw_data": {},
            "deleted_at": None,
            "updated_at": "2026-07-30T08:24:21+00:00",
        }
    ]
    payload["repairTopology"] = [{}]
    with (
        patch.object(repair, "EXPECTED_ARTIFACT_ROWS", 0),
        patch.object(repair, "EXPECTED_TOTAL_ROWS", 1),
        patch.object(repair, "EXPECTED_CONTACT_ROWS", 0),
        patch.object(repair, "EXPECTED_DOCUMENT_ROWS", 0),
        patch.object(repair, "EXPECTED_IMAGE_ROWS", 0),
        patch.object(repair, "EXPECTED_OM_FACTS", 0),
        patch.object(repair, "EXPECTED_EVENT_ROWS", 0),
        patch.object(repair, "EXPECTED_SOURCE_INDEX_ROWS", 0),
        patch.object(repair, "EXPECTED_QUEUE_ROWS", 0),
        patch.object(repair, "EXPECTED_ARTIFACT_TARGETS", 0),
    ):
        with pytest.raises(ValueError, match="stateListings row"):
            repair.validate_preimage(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__(
            "innerPayloadPgpBase64", "***"
        ),
        lambda payload: payload.__setitem__(
            "innerPayloadBytes", 0
        ),
        lambda payload: payload.__setitem__(
            "innerPayloadSha256", "A" * 64
        ),
        lambda payload: payload.__setitem__(
            "innerSchemaVersion", 0
        ),
        lambda payload: payload["innerCounts"].__setitem__(
            "omFacts", -1
        ),
    ],
)
def test_preimage_validation_rejects_inner_envelope_drift(mutation):
    payload = reviewed_empty_preimage()
    with patch.multiple(repair, **patched_review_counts(0)):
        assert repair.validate_preimage(payload) is payload
        mutation(payload)
        with pytest.raises(ValueError, match="inner"):
            repair.validate_preimage(payload)


def test_state_from_preimage_uses_explicit_generation_without_plaintext_raw_data():
    listing_id = "00000000-0000-0000-0000-000000000001"
    source_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/generation-envelope"
    )
    listing = current_listing(listing_id, source_url)
    payload = reviewed_empty_preimage()
    payload["stateListings"] = [listing]
    payload["repairTopology"] = [plan_row(listing, listing_id, source_url)]
    with patch.multiple(repair, **patched_review_counts(1)):
        state = repair.state_from_preimage(payload)
    assert state["listings"][0]["generation"] == repair.EXPECTED_GENERATION
    assert "raw_data" not in payload["stateListings"][0]


def test_preimage_validation_rejects_extra_clear_listing_state():
    listing_id = "00000000-0000-0000-0000-000000000001"
    source_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/no-clear-raw-data"
    )
    listing = current_listing(listing_id, source_url)
    listing["raw_data"] = {"large": "clear-state-is-forbidden"}
    payload = reviewed_empty_preimage()
    payload["stateListings"] = [listing]
    payload["repairTopology"] = [plan_row(listing, listing_id, source_url)]
    with (
        patch.multiple(repair, **patched_review_counts(1)),
        pytest.raises(ValueError, match="stateListings row"),
    ):
        repair.validate_preimage(payload)


def test_exact_post_state_detects_title_and_raw_data_only_changes():
    target_id = "url:v1:" + "1" * 32
    survivor_id = "00000000-0000-0000-0000-000000000001"
    state = expected_post_state(
        target_id,
        "https://www.cushmanwakefield.com/en/united-states/properties/example",
        target_id,
        survivor_id,
        "canonical_survivor",
    )
    title_change = json.loads(json.dumps(state))
    title_change["title"] = "Later enriched title"
    assert title_change != state
    raw_data = post_raw_data(
        target_id, survivor_id, "canonical_survivor"
    )
    raw_change = json.loads(json.dumps(raw_data))
    raw_change["laterDetail"] = {"fresh": True}
    assert post_raw_data_geometry(raw_change) != post_raw_data_geometry(
        raw_data
    )
    payload = reviewed_empty_preimage()
    with patch.multiple(repair, **patched_review_counts(0)):
        sql = repair.build_rollback_sql(
            payload, minimal_artifact(), minimal_state()
        )
    assert "'title',l.title" in sql
    assert "'raw_data_base',l.raw_data #-" not in sql
    assert "IS DISTINCT FROM p.post_raw_data_bytes" in sql
    assert "IS DISTINCT FROM p.post_raw_data_sha256" in sql
    assert ") IS DISTINCT FROM p.post_state" in sql
    assert "l.updated_at IS DISTINCT FROM (" in sql
    assert "CREATE TEMP TABLE _cw_rollback_inner_text" in sql
    assert "DO $cw_outer_inner_correlation$" in sql
    assert "Cushman outer/inner listing state mismatch" in sql
    assert "CREATE UNIQUE INDEX ON _pre_listings(id);" in sql


@pytest.mark.parametrize(
    "survivor_mode",
    ["zero", "multiple"],
)
def test_preimage_rejects_zero_or_multiple_survivors_per_target(survivor_mode):
    first_id = "00000000-0000-0000-0000-000000000011"
    second_id = "00000000-0000-0000-0000-000000000012"
    source_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/topology-example"
    )
    first = current_listing(first_id, source_url)
    second = current_listing(second_id, source_url)
    payload = reviewed_empty_preimage()
    payload["stateListings"] = [first, second]
    assignments = (
        [second_id, first_id]
        if survivor_mode == "zero"
        else [first_id, second_id]
    )
    payload["repairTopology"] = [
        plan_row(first, assignments[0], source_url),
        plan_row(second, assignments[1], source_url),
    ]
    with (
        patch.multiple(repair, **patched_review_counts(2)),
        pytest.raises(ValueError, match="survivor topology"),
    ):
        repair.validate_preimage(payload)


def test_preimage_rejects_cross_target_plan():
    first_id = "00000000-0000-0000-0000-000000000021"
    second_id = "00000000-0000-0000-0000-000000000022"
    first_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/first-target"
    )
    second_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/second-target"
    )
    first = current_listing(first_id, first_url)
    second = current_listing(second_id, second_url)

    swapped = reviewed_empty_preimage()
    swapped["stateListings"] = [first, second]
    swapped["repairTopology"] = [
        plan_row(first, second_id, second_url),
        plan_row(second, first_id, first_url),
    ]
    with (
        patch.multiple(repair, **patched_review_counts(2)),
        pytest.raises(ValueError, match="repairPlan identity"),
    ):
        repair.validate_preimage(swapped)

@pytest.mark.parametrize("key", ["contacts", "documents", "events"])
def test_child_policy_rejects_direct_child_left_on_alias(key):
    survivor_id = "00000000-0000-0000-0000-000000000101"
    alias_id = "00000000-0000-0000-0000-000000000102"
    target_id = "url:v1:" + "a" * 32
    payload = empty_child_policy_payload()
    payload[key] = [
        {
            "id": "00000000-0000-0000-0000-000000000199",
            "listing_id": alias_id,
            "post_listing_id": alias_id,
        }
    ]
    with pytest.raises(ValueError, match=f"{key} disposition"):
        repair.validate_child_dispositions(
            payload,
            {survivor_id: target_id, alias_id: target_id},
            {target_id: survivor_id},
        )


@pytest.mark.parametrize(
    "key", ["media", "links", "priceHistory", "scrapeLogs"]
)
def test_child_policy_rejects_unchanged_child_moved_to_survivor(key):
    survivor_id = "00000000-0000-0000-0000-000000000201"
    alias_id = "00000000-0000-0000-0000-000000000202"
    target_id = "url:v1:" + "b" * 32
    payload = empty_child_policy_payload()
    payload[key] = [
        {
            "id": "00000000-0000-0000-0000-000000000299",
            "listing_id": alias_id,
            "post_listing_id": survivor_id,
        }
    ]
    with pytest.raises(ValueError, match=f"{key} disposition"):
        repair.validate_child_dispositions(
            payload,
            {survivor_id: target_id, alias_id: target_id},
            {target_id: survivor_id},
        )


def test_image_policy_recomputes_winner_and_preserves_conflicting_alias_rows():
    survivor_id = "00000000-0000-0000-0000-000000000301"
    first_alias = "00000000-0000-0000-0000-000000000302"
    second_alias = "00000000-0000-0000-0000-000000000303"
    target_id = "url:v1:" + "c" * 32
    listing_targets = {
        survivor_id: target_id,
        first_alias: target_id,
        second_alias: target_id,
    }
    payload = empty_child_policy_payload()
    payload["images"] = [
        {
            "id": "00000000-0000-0000-0000-000000000310",
            "listing_id": first_alias,
            "post_listing_id": survivor_id,
            "url": "https://images.example/property.jpg",
        },
        {
            "id": "00000000-0000-0000-0000-000000000311",
            "listing_id": second_alias,
            "post_listing_id": second_alias,
            "url": "https://images.example/property.jpg",
        },
    ]
    repair.validate_child_dispositions(
        payload, listing_targets, {target_id: survivor_id}
    )
    payload["images"][1]["post_listing_id"] = survivor_id
    with pytest.raises(ValueError, match="images disposition"):
        repair.validate_child_dispositions(
            payload, listing_targets, {target_id: survivor_id}
        )

    payload["images"] = [
        {
            "id": "00000000-0000-0000-0000-000000000300",
            "listing_id": first_alias,
            "post_listing_id": first_alias,
            "url": "https://images.example/survivor-owned.jpg",
        },
        {
            "id": "00000000-0000-0000-0000-000000000399",
            "listing_id": survivor_id,
            "post_listing_id": survivor_id,
            "url": "https://images.example/survivor-owned.jpg",
        },
    ]
    repair.validate_child_dispositions(
        payload, listing_targets, {target_id: survivor_id}
    )


def test_om_policy_recomputes_latest_winner_and_keeps_older_conflicts():
    survivor_id = "00000000-0000-0000-0000-000000000401"
    first_alias = "00000000-0000-0000-0000-000000000402"
    second_alias = "00000000-0000-0000-0000-000000000403"
    target_id = "url:v1:" + "d" * 32
    listing_targets = {
        survivor_id: target_id,
        first_alias: target_id,
        second_alias: target_id,
    }
    shared = {
        "fact_group": "financial",
        "fact_key": "noi",
        "source_doc_url": "https://docs.example/om.pdf",
        "parser_version": "v1",
    }
    payload = empty_child_policy_payload()
    payload["omFacts"] = [
        {
            **shared,
            "id": "00000000-0000-0000-0000-000000000410",
            "listing_id": first_alias,
            "post_listing_id": survivor_id,
            "parsed_at": "2026-07-30T12:00:00+00:00",
        },
        {
            **shared,
            "id": "00000000-0000-0000-0000-000000000411",
            "listing_id": second_alias,
            "post_listing_id": second_alias,
            "parsed_at": "2026-07-29T12:00:00+00:00",
        },
    ]
    repair.validate_child_dispositions(
        payload, listing_targets, {target_id: survivor_id}
    )
    payload["omFacts"][1]["post_listing_id"] = survivor_id
    with pytest.raises(ValueError, match="omFacts disposition"):
        repair.validate_child_dispositions(
            payload, listing_targets, {target_id: survivor_id}
        )

    payload["omFacts"] = [
        {
            **shared,
            "id": "00000000-0000-0000-0000-000000000420",
            "listing_id": survivor_id,
            "post_listing_id": survivor_id,
            "parsed_at": "2026-07-31T12:00:00+00:00",
        },
        {
            **shared,
            "id": "00000000-0000-0000-0000-000000000421",
            "listing_id": first_alias,
            "post_listing_id": first_alias,
            "parsed_at": "2026-07-30T12:00:00+00:00",
        },
    ]
    repair.validate_child_dispositions(
        payload, listing_targets, {target_id: survivor_id}
    )


def test_non_null_old_generation_alias_is_valid_roundtrip_disposition():
    survivor_id = "00000000-0000-0000-0000-000000000001"
    alias_id = "00000000-0000-0000-0000-000000000002"
    source_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/old-generation-example"
    )
    target_id = repair.cushman_canonical_external_id(source_url)
    assert target_id is not None
    alias_external_id = (
        "cushman-superseded:v1:"
        + hashlib.md5(alias_id.encode(), usedforsecurity=False).hexdigest()
    )
    payload = reviewed_empty_preimage()
    payload["stateListings"] = [
        {
            "id": survivor_id,
            "external_id": "current-provider",
            "source_url": source_url,
            "deleted": False,
            "generation": repair.EXPECTED_GENERATION,
            "updated_at": "2026-07-30T08:24:21+00:00",
        },
        {
            "id": alias_id,
            "external_id": "old-provider",
            "source_url": source_url,
            "deleted": False,
            "generation": "2026-07-01T000000Z",
            "updated_at": "2026-07-01T00:00:00+00:00",
        },
    ]
    payload["repairTopology"] = [
        {
            "id": survivor_id,
            "target_id": target_id,
            "survivor_id": survivor_id,
            "has_current": True,
            "post_external_id": target_id,
            "post_source_url": source_url,
            "post_deleted": False,
            "post_generation": repair.EXPECTED_GENERATION,
        },
        {
            "id": alias_id,
            "target_id": target_id,
            "survivor_id": survivor_id,
            "has_current": True,
            "post_external_id": alias_external_id,
            "post_source_url": source_url,
            "post_deleted": True,
            "post_generation": "2026-07-01T000000Z",
        },
    ]
    zero_child_counts = {
        "EXPECTED_ARTIFACT_ROWS": 0,
        "EXPECTED_TOTAL_ROWS": 2,
        "EXPECTED_CONTACT_ROWS": 0,
        "EXPECTED_DOCUMENT_ROWS": 0,
        "EXPECTED_IMAGE_ROWS": 0,
        "EXPECTED_OM_FACTS": 0,
        "EXPECTED_EVENT_ROWS": 0,
        "EXPECTED_SOURCE_INDEX_ROWS": 0,
        "EXPECTED_QUEUE_ROWS": 0,
    }
    with patch.multiple(repair, **zero_child_counts):
        assert repair.validate_preimage(payload) is payload
        sql = repair.build_roundtrip_sql(
            payload, minimal_artifact(), minimal_state()
        )
    assert ") IS DISTINCT FROM p.post_state" in sql
    assert "parent post-repair disposition drift" in sql
    assert "rollback refused after newer Cushman data" not in sql


def test_old_only_survivor_plan_is_complete_and_preserves_source_generation():
    listing_id = "00000000-0000-0000-0000-000000000003"
    source_url = (
        "https://www.cushmanwakefield.com/en/"
        "united-states/properties/old-only-example"
    )
    old_generation = "2026-06-30T120000Z"
    target_id = repair.cushman_canonical_external_id(source_url)
    assert target_id is not None
    payload = reviewed_empty_preimage()
    payload["stateListings"] = [
        {
            "id": listing_id,
            "external_id": "old-only-provider",
            "source_url": source_url,
            "deleted": False,
            "generation": old_generation,
            "updated_at": "2026-06-30T12:00:00+00:00",
        }
    ]
    payload["repairTopology"] = [
        {
            "id": listing_id,
            "target_id": target_id,
            "survivor_id": listing_id,
            "has_current": False,
            "post_external_id": target_id,
            "post_source_url": source_url,
            "post_deleted": False,
            "post_generation": old_generation,
        }
    ]
    with (
        patch.object(repair, "EXPECTED_ARTIFACT_ROWS", 0),
        patch.object(repair, "EXPECTED_TOTAL_ROWS", 1),
        patch.object(repair, "EXPECTED_CONTACT_ROWS", 0),
        patch.object(repair, "EXPECTED_DOCUMENT_ROWS", 0),
        patch.object(repair, "EXPECTED_IMAGE_ROWS", 0),
        patch.object(repair, "EXPECTED_OM_FACTS", 0),
        patch.object(repair, "EXPECTED_EVENT_ROWS", 0),
        patch.object(repair, "EXPECTED_SOURCE_INDEX_ROWS", 0),
        patch.object(repair, "EXPECTED_QUEUE_ROWS", 0),
    ):
        assert repair.validate_preimage(payload) is payload
    assert (
        len(payload["repairTopology"])
        == len(payload["stateListings"])
        == 1
    )


def test_rollback_timestamp_readback_matches_before_update_trigger_clock():
    payload = reviewed_empty_preimage()
    with (
        patch.object(repair, "EXPECTED_ARTIFACT_ROWS", 0),
        patch.object(repair, "EXPECTED_TOTAL_ROWS", 0),
        patch.object(repair, "EXPECTED_CONTACT_ROWS", 0),
        patch.object(repair, "EXPECTED_DOCUMENT_ROWS", 0),
        patch.object(repair, "EXPECTED_IMAGE_ROWS", 0),
        patch.object(repair, "EXPECTED_OM_FACTS", 0),
        patch.object(repair, "EXPECTED_EVENT_ROWS", 0),
        patch.object(repair, "EXPECTED_SOURCE_INDEX_ROWS", 0),
        patch.object(repair, "EXPECTED_QUEUE_ROWS", 0),
    ):
        sql = repair.build_rollback_sql(
            payload, minimal_artifact(), minimal_state()
        )
    assert "transaction_timestamp() AS trigger_updated_at" in sql
    assert "l.updated_at IS DISTINCT FROM (" in sql
    assert "SELECT trigger_updated_at FROM _cw_rollback_clock" in sql
    assert "clock_timestamp() AS started_at" not in sql


def test_apply_timestamp_is_bound_to_private_preimage_and_parent_marker():
    apply_sql = repair.build_apply_sql(minimal_artifact(), minimal_state())
    preimage_sql = repair.preimage_sql(minimal_artifact(), minimal_state())
    assert "'applyTimestampBinding'" in preimage_sql
    assert "'appliedAt',transaction_timestamp()" in apply_sql
    assert f"'repairToken','{repair.REPAIR_TOKEN}'" in apply_sql
    assert "l.updated_at IS DISTINCT FROM (" in apply_sql
    assert "cushmanIdentityRepair,appliedAt" in apply_sql
    assert ") IS DISTINCT FROM p.post_state" in apply_sql


def test_roundtrip_runs_forward_and_reverse_in_one_rolled_back_transaction():
    payload = reviewed_empty_preimage()
    with (
        patch.object(repair, "EXPECTED_ARTIFACT_ROWS", 0),
        patch.object(repair, "EXPECTED_TOTAL_ROWS", 0),
        patch.object(repair, "EXPECTED_CONTACT_ROWS", 0),
        patch.object(repair, "EXPECTED_DOCUMENT_ROWS", 0),
        patch.object(repair, "EXPECTED_IMAGE_ROWS", 0),
        patch.object(repair, "EXPECTED_OM_FACTS", 0),
        patch.object(repair, "EXPECTED_EVENT_ROWS", 0),
        patch.object(repair, "EXPECTED_SOURCE_INDEX_ROWS", 0),
        patch.object(repair, "EXPECTED_QUEUE_ROWS", 0),
    ):
        sql = repair.build_roundtrip_sql(
            payload, minimal_artifact(), minimal_state()
        )
    assert sql.count("BEGIN ISOLATION LEVEL SERIALIZABLE;") == 1
    assert "DROP TABLE _cw_current" in sql
    assert sql.rstrip().endswith("ROLLBACK;")


def test_preimage_chunk_transport_reconstructs_exact_validated_ascii_payload():
    payload = reviewed_empty_preimage()
    payload["innerPayloadPgpBase64"] = base64.b64encode(
        b"synthetic-compressed-inner" * 20_000
    ).decode()
    with patch.multiple(repair, **patched_review_counts(0)):
        chunks = repair.preimage_json_chunks(payload)
        sql = repair.preimage_chunk_transport_sql(payload)

    expected = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    )
    expected_bytes = expected.encode("ascii")
    expected_md5 = hashlib.md5(
        expected_bytes, usedforsecurity=False
    ).hexdigest()
    assert "".join(chunks) == expected
    assert all(
        len(chunk.encode("ascii")) <= repair.PREIMAGE_SQL_CHUNK_BYTES
        for chunk in chunks
    )
    assert len(chunks) > 1
    assert f"HAVING count(*)={len(chunks)}" in sql
    assert f"max(seq)={len(chunks) - 1}" in sql
    assert "string_agg(payload,'' ORDER BY seq)" in sql
    assert f"octet_length(payload)<>{len(expected_bytes)}" in sql
    assert f"md5(payload)<>'{expected_md5}'" in sql


def test_preimage_chunk_transport_validates_before_serialization():
    payload = reviewed_empty_preimage()
    payload["artifactSha256"] = "0" * 64
    payload["unserializableProbe"] = object()
    with pytest.raises(ValueError, match="artifactSha256"):
        repair.preimage_json_chunks(payload)


def test_preimage_chunk_transport_emits_bounded_separate_insert_statements():
    payload = reviewed_empty_preimage()
    payload["innerPayloadPgpBase64"] = base64.b64encode(
        b"x" * (repair.PREIMAGE_SQL_CHUNK_BYTES * 2 + 1)
    ).decode()
    with patch.multiple(repair, **patched_review_counts(0)):
        chunks = repair.preimage_json_chunks(payload)
        sql = repair.preimage_chunk_transport_sql(payload)

    inserts = [
        line
        for line in sql.splitlines()
        if line.startswith("INSERT INTO _cw_preimage_chunks")
    ]
    assert len(inserts) == len(chunks) >= 3
    assert all(line.endswith(";") for line in inserts)
    assert all(
        len(line.encode("utf-8"))
        <= repair.PREIMAGE_SQL_STATEMENT_CEILING_BYTES
        for line in inserts
    )


def test_preimage_chunk_transport_fails_closed_before_jsonb_cast():
    payload = reviewed_empty_preimage()
    with patch.multiple(repair, **patched_review_counts(0)):
        sql = repair.preimage_chunk_transport_sql(payload)

    assert "seq integer PRIMARY KEY CHECK (seq>=0)" in sql
    assert "min(seq)=0" in sql
    assert "Cushman rollback preimage chunk geometry mismatch" in sql
    assert "Cushman rollback preimage payload integrity mismatch" in sql
    assert "SELECT payload::jsonb FROM _cw_preimage_assembled" in sql
    assert "Cushman rollback preimage assembled row count mismatch" in sql
    assert sql.index("payload integrity mismatch") < sql.index("payload::jsonb")
    assert "DROP TABLE _cw_preimage_chunks,_cw_preimage_assembled;" in sql


def test_chunked_roundtrip_preserves_every_rollback_child_guard():
    payload = reviewed_empty_preimage()
    with patch.multiple(repair, **patched_review_counts(0)):
        sql = repair.build_roundtrip_sql(
            payload, minimal_artifact(), minimal_state()
        )

    for key in (
        "contacts",
        "documents",
        "images",
        "media",
        "links",
        "omFacts",
        "events",
        "priceHistory",
        "scrapeLogs",
    ):
        assert f"rollback refused: {key} post-repair mapping drift" in sql
        assert f"Cushman rollback {key} readback failed" in sql
    assert sql.count("BEGIN ISOLATION LEVEL SERIALIZABLE;") == 1
    assert sql.rstrip().endswith("ROLLBACK;")
