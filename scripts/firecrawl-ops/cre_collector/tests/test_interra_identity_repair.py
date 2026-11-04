"""Exact synthetic Interra repair plans; no provider or application database."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import cre_repair_interra_identity as repair
import pytest


def fixture(directory):
    now = datetime.now(UTC)
    observed = now.isoformat()
    brokerage = str(uuid.UUID(int=1))
    pairs, parents, contacts, images, source_index = [], [], [], [], []
    listings = [
        {
            "sourceKey": "interra-realty",
            "transactionMode": "sale",
            "id": str(1000 + index),
            "url": f"https://interrarealty.com/listing/synthetic-{index}/",
        }
        for index in range(62)
    ]
    for index in range(22):
        survivor, duplicate, image = (
            str(uuid.UUID(int=100 + index * 3 + offset)) for offset in range(3)
        )
        native, url = listings[index]["id"], listings[index]["url"]
        legacy = "listing/synthetic-" + str(index)
        pairs.append(
            {
                "survivor_id": survivor,
                "superseded_id": duplicate,
                "image_id": image,
                "legacy_external_id": legacy,
                "provider_external_id": native,
                "source_url": url,
                "superseded_external_id": "interra-superseded:v1:" + duplicate,
            }
        )
        for parent_id, external in [(survivor, legacy), (duplicate, native)]:
            parents.append(
                {
                    "id": parent_id,
                    "brokerage_id": brokerage,
                    "external_id": external,
                    "source_url": url,
                    "status": "active",
                    "deleted_at": None,
                    "raw_data": {"sourceKey": "interra-realty", "id": external},
                    "updated_at": observed,
                }
            )
        images.append(
            {
                "id": image,
                "listing_id": duplicate,
                "url": "https://example.com/synthetic.jpg",
            }
        )
        source_index.append(
            {
                "id": str(uuid.UUID(int=10000 + index)),
                "brokerage_id": brokerage,
                "external_id": native,
                "source_key": "interra-realty",
                "url": url,
            }
        )
        for n in range(2 if index < 18 else 1):
            contacts.append(
                {
                    "id": str(uuid.UUID(int=20000 + index * 2 + n)),
                    "listing_id": survivor,
                    "name": "Synthetic",
                }
            )
    artifact = {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": (now - timedelta(seconds=1)).isoformat(),
            "finishedAt": observed,
            "freshness": {"generationId": "synthetic-generation"},
        },
        "sources": [
            {
                "sourceKey": "interra-realty",
                "transaction": tx,
                "supported": True,
                "truncated": False,
                "listingsCollected": count,
            }
            for tx, count in [("sale", 62), ("lease", 0)]
        ],
        "listings": listings,
        "totalListings": 62,
    }
    path = directory / "artifact.json"
    path.write_text(json.dumps(artifact))
    plan = {
        "schema_version": 1,
        "repair": repair.OPERATION,
        "approval_ref": repair.APPROVAL_REF,
        "database_target_sha256": "b" * 64,
        "untouched_unmatched_legacy_rows": 874,
        "artifact": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "collector_sha": "a" * 40,
            "finished_at": observed,
            "generation_id": "synthetic-generation",
        },
        "pairs": pairs,
        "before": {
            "parents": parents,
            "contacts": contacts,
            "images": images,
            "source_index": source_index,
            "foreign_key_definitions": [
                {"table": "credeals." + table, "column": column, "arity": 1}
                for table, column in sorted(repair.FK_COLUMNS.items())
            ],
            "fk_surfaces": ["credeals." + table for table in sorted(repair.FK_COLUMNS)],
            "foreign_key_rows": {
                "credeals." + table: contacts
                if table == "cre_listing_contacts"
                else images
                if table == "cre_listing_images"
                else []
                for table in repair.FK_COLUMNS
            },
            "soft_reference_rows": {table: [] for table in repair.ARCHIVES},
        },
    }
    references = [
        *list(repair.FK_COLUMNS)[:9],
        *repair.ARCHIVES,
        "queue",
        "sourceIndex",
    ]
    survivors = {pair["survivor_id"] for pair in pairs}
    plan["reference_counts"] = [
        {
            "id": row["id"],
            "parent": row,
            "references": {
                table: sum(x["listing_id"] == row["id"] for x in contacts)
                if table == "cre_listing_contacts"
                else 1
                if table == "cre_listing_images" and row["id"] not in survivors
                else 1
                if table == "sourceIndex" and row["id"] not in survivors
                else 0
                for table in references
            },
        }
        for row in parents
    ]
    plan["plan_hash"] = repair.digest(plan)
    return plan, path


def test_exact_fixture_plan_and_generated_transaction(tmp_path):
    plan, path = fixture(tmp_path)
    assert repair.validate_plan(plan, path) == plan
    sql = repair.build_sql(plan, "cayman")
    assert sql.startswith("BEGIN ISOLATION LEVEL SERIALIZABLE;")
    assert (
        sql.index("cre_source_index WHERE")
        < sql.index("cre_listings WHERE id IN")
        < sql.index("cre_listing_images WHERE")
    )
    assert sql.count("approved_identity_supersession") == 23
    assert "DELETE FROM" not in sql and "UPDATE credeals.cre_source_index" not in sql
    assert "UPDATE credeals.cre_listing_contacts" not in sql
    assert "UPDATE credeals.cre_source_baseline" not in sql
    assert "UPDATE credeals.cre_listing_om_facts" not in sql


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "pair",
        "image",
        "parent_status",
        "source_index",
        "extra_fk",
        "hidden_reference",
        "archive",
        "expiry",
        "artifact_bytes",
    ],
)
def test_plan_drift_fails_closed(tmp_path, case):
    plan, path = fixture(tmp_path)
    now = None
    if case == "hash":
        plan["pairs"][0]["legacy_external_id"] = "tampered"
    elif case == "pair":
        plan["pairs"][0]["provider_external_id"] = "999999"
    elif case == "image":
        plan["before"]["images"][0]["listing_id"] = plan["pairs"][0]["survivor_id"]
    elif case == "parent_status":
        plan["before"]["parents"][0]["status"] = "sold"
    elif case == "source_index":
        plan["before"]["source_index"][0]["source_key"] = "hanley"
    elif case == "extra_fk":
        plan["before"]["foreign_key_definitions"].append(
            {"table": "credeals.other", "column": "listing_id", "arity": 1}
        )
    elif case == "hidden_reference":
        plan["before"]["foreign_key_rows"]["credeals.search_results"] = [
            {"source_listing_id": plan["pairs"][0]["survivor_id"]}
        ]
    elif case == "archive":
        plan["before"]["soft_reference_rows"][repair.ARCHIVES[0]] = [
            {"source_listing_id": plan["pairs"][0]["survivor_id"]}
        ]
    elif case == "expiry":
        now = datetime.now(UTC) + timedelta(hours=25)
    elif case == "artifact_bytes":
        path.write_text("{}")
    if case != "hash":
        plan["plan_hash"] = repair.digest(
            {k: v for k, v in plan.items() if k != "plan_hash"}
        )
    with pytest.raises((ValueError, KeyError)):
        repair.validate_plan(plan, path, now)


def test_private_plan_read_rejects_shared_or_symlink_input(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        repair.read_private_json(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        repair.read_private_json(link)


def test_cli_default_is_private_sql_preview_without_backend_or_lock(
    tmp_path, monkeypatch
):
    plan, artifact = fixture(tmp_path)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    path.chmod(0o600)
    monkeypatch.setattr(
        repair.ingest, "load_db_url", lambda *_: pytest.fail("preview used credentials")
    )
    monkeypatch.setattr(
        repair.checkpoint,
        "SharedLock",
        lambda *_a, **_k: pytest.fail("preview acquired live lock"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "repair",
            "--plan",
            str(path),
            "--artifact",
            str(artifact),
            "--out-dir",
            str(tmp_path / "preview"),
        ],
    )
    assert repair.main() == 0
    assert (tmp_path / "preview/repair.sql").stat().st_mode & 0o077 == 0
    assert (tmp_path / "preview/preimage.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "case",
    [
        "missing_contract",
        "wrong_operator",
        "wrong_reference",
        "wrong_contract_hash",
        "wrong_confirmation",
        "wrong_target",
        "wrong_sha",
        "dirty_checkout",
    ],
)
def test_apply_admission_refuses_before_credentials_or_output(
    tmp_path, monkeypatch, case
):
    plan, artifact = fixture(tmp_path)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    path.chmod(0o600)
    contract = {
        "schema_version": 1,
        "operation": repair.OPERATION,
        "operator": "cayman",
        "approval_ref": repair.APPROVAL_REF,
        "plan_hash": plan["plan_hash"],
    }
    if case == "wrong_operator":
        contract["operator"] = "someone"
    if case == "wrong_reference":
        contract["approval_ref"] = "AGENTIC-4177"
    if case == "wrong_contract_hash":
        contract["plan_hash"] = "0" * 64
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(contract))
    approval.chmod(0o600)
    out = tmp_path / "apply"
    argv = [
        "repair",
        "--plan",
        str(path),
        "--artifact",
        str(artifact),
        "--out-dir",
        str(out),
        "--apply",
        "--plan-hash",
        plan["plan_hash"],
        "--confirm",
        "wrong"
        if case == "wrong_confirmation"
        else "APPLY " + repair.OPERATION + " " + plan["plan_hash"],
        "--expected-db-target-sha256",
        "0" * 64 if case == "wrong_target" else plan["database_target_sha256"],
        "--expected-sha",
        "0" * 40 if case == "wrong_sha" else "a" * 40,
    ]
    if case != "missing_contract":
        argv += ["--approval-contract", str(approval)]
    monkeypatch.setattr(
        repair.checkpoint, "git_identity", lambda: ("a" * 40, case == "dirty_checkout")
    )
    monkeypatch.setattr(
        repair.ingest,
        "load_db_url",
        lambda *_: pytest.fail("invalid approval used credentials"),
    )
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(ValueError):
        repair.main()
    assert not out.exists()
