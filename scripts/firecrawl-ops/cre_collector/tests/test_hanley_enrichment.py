"""Source-backed Hanley enrichment: real producers, mocked worker IO only."""

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import cre_enrich as enrich
import cre_ingest as ingest
from test_cre_enrich import _Args, _wire_run

HERE = Path(__file__).resolve().parents[1]
URL = "https://hanleyinvestmentgroup.com/listings/?id=H-1"


def node_json(script):
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            "--import",
            "tsx",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def generic_artifact():
    listing = node_json(
        "import {parseGenericJsonLd} from './lib/enrich.ts';"
        "console.log(JSON.stringify(parseGenericJsonLd("
        + json.dumps({"sourceKey": "hanley", "externalId": "H-1", "url": URL})
        + ',\'<script type="application/ld+json">{"@type":"Product","name":"Synthetic property"}</script>\')));'
    )
    return {
        "runMeta": {"mode": "enrich"},
        "listings": [{"sourceKey": "hanley", "transactionMode": "sale", **listing}],
    }


def test_actual_generic_parser_worker_refuses_before_ingest_and_completion(
    monkeypatch, tmp_path, generic_artifact
):
    calls = _wire_run(
        monkeypatch,
        tmp_path,
        claimed=[("claim-one", "hanley", "H-1", URL, "changed", "1")],
        write_enriched=generic_artifact,
    )
    assert enrich.run(_Args(source="hanley", batch=1)) == 1
    assert not calls["ingest_called"]
    assert not any(
        "DELETE FROM credeals.cre_enrichment_queue" in sql for sql in calls["exec_sql"]
    )


def test_generic_direct_cli_refuses_before_database(
    monkeypatch, tmp_path, generic_artifact
):
    artifact = tmp_path / "input.json"
    artifact.write_text(json.dumps(generic_artifact))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(artifact)])
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *_: pytest.fail("unsafe DB access")
    )
    monkeypatch.setattr(
        ingest, "build_sql", lambda *_a, **_kw: pytest.fail("unsafe SQL generation")
    )
    with pytest.raises(SystemExit, match="Hanley enrichment"):
        ingest.main()


@pytest.fixture(scope="module")
def backed_artifact(tmp_path_factory):
    root = tmp_path_factory.mktemp("hanley-source")
    html = (
        "<script>var rethink_properties = "
        + json.dumps(
            [
                {
                    "id": "H-1",
                    "name": "Synthetic sale",
                    "visibility": "Public",
                    "dealRecordType": "Seller_Rep",
                    "image": "https://example.com/sale.jpg",
                },
                {
                    "id": "H-2",
                    "name": "Synthetic lease",
                    "visibility": "Public",
                    "dealRecordType": "Landlord_Rep",
                },
                {"id": "H-3", "name": "Private", "visibility": "Private"},
            ]
        )
        + ';</script><div id="rethink-properties-container"><article data-id="H-1" data-result="true"></article><article data-id="H-2" data-result="true"></article></div>'
    )
    preload = root / "preload.mjs"
    preload.write_text(
        "globalThis.fetch=async(url)=>{if(String(url)!=='https://hanleyinvestmentgroup.com/listings/')throw Error('unexpected offline request');return new Response("
        + json.dumps(html)
        + ",{status:200});};"
    )
    claims = root / "claims.json"
    claims.write_text(
        json.dumps(
            {
                "items": [
                    {"sourceKey": "hanley", "externalId": "H-1", "url": URL},
                    {
                        "sourceKey": "hanley",
                        "externalId": "H-2",
                        "url": URL.replace("H-1", "H-2"),
                    },
                    {
                        "sourceKey": "hanley",
                        "externalId": "H-3",
                        "url": URL.replace("H-1", "H-3"),
                    },
                    {
                        "sourceKey": "hanley",
                        "externalId": "missing",
                        "url": URL.replace("H-1", "missing"),
                    },
                    {
                        "sourceKey": "hanley",
                        "externalId": "H-1",
                        "url": "https://wrong.example/property",
                    },
                ]
            }
        )
    )
    output = root / "artifact.json"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("CRE_")
    }
    subprocess.run(
        [
            shutil.which("node") or "node",
            "--import",
            str(preload),
            "--import",
            "tsx",
            "collect.ts",
            "--enrich-input",
            str(claims),
            "--out",
            str(output),
        ],
        cwd=HERE,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(output.read_text())


def test_actual_source_collect_worker_staging_and_sql_preserve_children(
    monkeypatch, tmp_path, backed_artifact
):
    data = backed_artifact
    assert [row["id"] for row in data["listings"]] == ["H-1", "H-2"]
    assert data["sources"][0]["enricher"] == "bespoke"
    assert data["runMeta"]["mode"] == "enrich"
    assert not (data["runMeta"].get("freshness") or {}).get("requireFreshDetails")
    assert [row["transactionType"] for row in data["listings"]] == ["Sale", "Lease"]
    calls = _wire_run(
        monkeypatch,
        tmp_path,
        claimed=[
            ("claim-" + row["id"], "hanley", row["id"], row["url"], "changed", "0")
            for row in data["listings"]
        ],
        write_enriched=data,
    )
    assert enrich.run(_Args(source="hanley", batch=2)) == 0
    assert calls["ingest_called"]
    ingest.validate_strict_artifact_freshness(data)
    rows = [
        ingest.to_row(row, {}, data["runMeta"]["finishedAt"])
        for row in data["listings"]
    ]
    assert all(row["raw_data"]["preserveChildCollections"] is True for row in rows)
    assert all(row["raw_data"]["inventoryObservedAt"] for row in rows)
    assert [row["transaction_type"] for row in rows] == ["sale", "lease"]
    assert rows[0]["images"]
    sql = ingest.build_sql(
        rows,
        [
            {
                "slug": rows[0]["slug"],
                "discovered": 2,
                "saved": 2,
                "errors": 0,
                "notes": "test",
            }
        ],
        data["runMeta"]["startedAt"],
        set(),
    )
    assert "CREATE TEMP TABLE _child_additive" in sql
    assert "preserveChildCollections" in sql
    assert "OR NOT EXISTS (" in sql  # New parents still receive public-card children.


@pytest.mark.parametrize(
    "mutation",
    [
        "absent",
        "false",
        "string",
        "nested",
        "wrong_id",
        "wrong_url",
        "wrong_raw_id",
        "private",
        "missing_observation",
        "bad_scope",
    ],
)
def test_unsafe_hanley_enrichment_is_refused(backed_artifact, mutation):
    data = copy.deepcopy(backed_artifact)
    row = data["listings"][0]
    if mutation == "absent":
        row.pop("preserveChildCollections")
    elif mutation in {"false", "string"}:
        row["preserveChildCollections"] = False if mutation == "false" else "true"
    elif mutation == "nested":
        row.pop("preserveChildCollections")
        row["nested"] = {"preserveChildCollections": True}
    elif mutation == "wrong_id":
        row["id"] = "another"
    elif mutation == "wrong_url":
        row["url"] = "https://wrong.example/property"
    elif mutation == "wrong_raw_id":
        row["rawHanley"]["id"] = "another"
    elif mutation == "private":
        row["rawHanley"]["visibility"] = "Private"
    elif mutation == "missing_observation":
        row.pop("inventoryObservedAt")
    else:
        row["freshnessProvenance"]["detailScope"] = "detail_page"
    with pytest.raises(ValueError, match="Hanley enrichment"):
        ingest.validate_strict_artifact_freshness(data)


def test_hanley_worker_checks_claimed_native_identity(backed_artifact):
    row = backed_artifact["listings"][0]
    with pytest.raises(ValueError, match="Hanley enrichment"):
        enrich.validate_enriched_artifact(
            [{"url": row["url"], "source_key": "hanley", "external_id": "different"}],
            backed_artifact,
        )


def test_unmatched_hanley_claims_remain_retryable(
    monkeypatch, tmp_path, backed_artifact
):
    rows = backed_artifact["listings"]
    calls = _wire_run(
        monkeypatch,
        tmp_path,
        claimed=[
            ("done", "hanley", rows[0]["id"], rows[0]["url"], "changed", "0"),
            (
                "missing",
                "hanley",
                "missing",
                URL.replace("H-1", "missing"),
                "changed",
                "0",
            ),
        ],
        write_enriched={**backed_artifact, "listings": rows[:1]},
    )
    assert enrich.run(_Args(source="hanley", batch=2)) == 0
    sql = "\n".join(calls["exec_sql"])
    assert "WHERE id IN ('done'::uuid)" in sql
    assert "attempts = attempts + 1" in sql
    assert "WHERE id IN ('missing'::uuid)" in sql


def test_full_source_hanley_contract_is_unchanged(generic_artifact):
    full = {**generic_artifact, "runMeta": {"mode": "full"}}
    ingest.validate_strict_artifact_freshness(full)
