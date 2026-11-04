"""SVN enrichment keeps the prior inventory wrapper alongside new detail."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import cre_ingest


def raw_data_case(*, enrichment):
    sql = cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", set(), svn_enrichment=enrichment)
    return sql.split("raw_data          = ", 1)[1].split(",\n        source_lastmod", 1)[0]


def test_svn_enrichment_policy_is_scoped_and_default_is_off():
    enabled = raw_data_case(enrichment=True)
    disabled = raw_data_case(enrichment=False)
    assert "WHEN TRUE\n" in enabled
    assert "WHEN FALSE\n" in disabled
    assert "EXCLUDED.raw_data->>'sourceKey' = 'svn'" in enabled
    assert "sb.slug='svn'" in enabled
    assert "t.raw_data ? 'inventoryObservedAt'" in enabled
    assert "t.raw_data ? 'latestInventoryObservation'" in enabled
    assert "EXCLUDED.raw_data\n" in enabled
    assert "{svn_enrichment_sql}" not in enabled


@pytest.mark.parametrize(("mode", "predicate"), [("enrich", "TRUE"), ("full", "FALSE")])
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
    assert f"WHEN {predicate}\n" in branch
    assert "EXCLUDED.raw_data->>'sourceKey' = 'svn'" in branch
