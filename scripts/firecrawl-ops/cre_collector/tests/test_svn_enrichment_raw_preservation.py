"""SVN enrichment keeps the prior inventory wrapper alongside new detail."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import cre_ingest


def raw_data_case(*, enrichment):
    sql = cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", set(), svn_enrichment_ids=("synthetic",) if enrichment else ())
    return sql.split("raw_data          = ", 1)[1].split(",\n        source_lastmod", 1)[0]


def test_svn_enrichment_policy_is_scoped_and_default_is_off():
    enabled = raw_data_case(enrichment=True)
    disabled = raw_data_case(enrichment=False)
    assert "(sb.slug, EXCLUDED.external_id) IN (('svn', 'synthetic'))" in enabled
    assert "WHEN FALSE\n" in disabled
    assert "EXCLUDED.raw_data - 'inventoryObservedAt' - 'latestInventoryObservation'" in enabled
    assert "('svn', 'synthetic')" in enabled
    assert "t.raw_data ? 'inventoryObservedAt'" in enabled
    assert "t.raw_data ? 'latestInventoryObservation'" in enabled
    assert "EXCLUDED.raw_data\n" in enabled
    assert "{svn_enrichment_sql}" not in enabled


@pytest.mark.parametrize(("mode", "predicate"), [("enrich", "IN ("), ("full", "FALSE")])
def test_ingest_cli_passes_artifact_mode_to_real_generated_sql(tmp_path, mode, predicate):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps({
        "runMeta": {"mode": mode, "startedAt": "2026-11-04T00:00:00Z",
                    "finishedAt": "2026-11-04T00:01:00Z"},
        "sources": [{"sourceKey": "svn"}],
        "listings": [{"sourceKey": "svn", "id": "synthetic", "url": "https://example.test/listing",
                      "name": "Synthetic", "transactionType": "sale"}],
    }))
    out = tmp_path / "sql"
    result = subprocess.run(
        [sys.executable, "cre_ingest.py", "--in", str(artifact), "--dry-run", "--keep-artifacts", str(out)],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    sql = (out / "ingest.sql").read_text()
    branch = sql.split("raw_data          = ", 1)[1].split(",\n        source_lastmod", 1)[0]
    assert predicate in branch
    assert "(sb.slug, EXCLUDED.external_id) IN" in branch or "AND FALSE" in branch


def test_multiple_inputs_bind_only_svn_enrichment_ids_and_merged_identity(tmp_path):
    paths = []
    for index, (mode, identity) in enumerate((("full", "same"), ("enrich", "same"),
                                               ("enrich", "other"), ("full", "inventory"))):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps({
            "runMeta": {"mode": mode, "startedAt": "2026-11-04T00:00:00Z",
                        "finishedAt": "2026-11-04T00:01:00Z"},
            "sources": [{"sourceKey": "svn"}],
            "listings": [{"sourceKey": "svn", "id": identity,
                          "url": f"https://example.test/{identity}", "name": "Synthetic",
                          "transactionType": "sale"}],
        }))
        paths += ["--in", str(path)]
    out = tmp_path / "sql"
    result = subprocess.run(
        [sys.executable, "cre_ingest.py", *paths, "--dry-run", "--keep-artifacts", str(out)],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    sql = (out / "ingest.sql").read_text()
    branch = sql.split("raw_data          = ", 1)[1].split(",\n        source_lastmod", 1)[0]
    assert "(sb.slug, EXCLUDED.external_id) IN (('svn', 'other'))" in branch
    assert "'same'" not in branch
    assert "'inventory'" not in branch
    index = sql.split("-- Canonical full ingest synchronizes", 1)[1].split("-- Children:", 1)[0]
    assert index.count("s.external_id IN ('other')") == 2
    assert "s.external_id IN ('same')" not in index
    assert "s.external_id IN ('inventory')" not in index


def test_svn_queue_without_tenure_omits_synthetic_sale():
    import cre_enrich

    items = cre_enrich.claim_rows_to_items([
        {"source_key": "svn", "external_id": "x", "url": "https://example.test/x"},
        {"source_key": "newmark", "external_id": "y", "url": "https://example.test/y"},
    ])["items"]
    assert "transaction" not in items[0]
    assert items[1]["transaction"] == "sale"
