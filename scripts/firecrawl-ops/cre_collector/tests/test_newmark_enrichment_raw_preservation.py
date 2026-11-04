"""Newmark enrichment keeps the prior inventory observation and new detail."""
import json
import subprocess
import sys
from pathlib import Path

import cre_ingest


def test_newmark_and_svn_bindings_are_source_scoped_and_other_sources_refused():
    sql = cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", set(),
        inventory_preserve_enrichment_ids={("svn", "shared"), ("newmark", "shared")})
    assert "(sb.slug, EXCLUDED.external_id) IN (('newmark', 'shared'), ('svn', 'shared'))" in sql
    assert "nb.slug='newmark'" in sql
    assert "EXCLUDED.external_id IN ('shared')" in sql
    import pytest
    with pytest.raises(ValueError, match="invalid inventory-preserving"):
        cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", set(),
            inventory_preserve_enrichment_ids={("jll", "shared")})


def test_newmark_mixed_inputs_only_bind_enrichment_only_identity(tmp_path):
    inputs = []
    for n, mode, identity in ((0, "full", "same"), (1, "enrich", "other"),
                              (2, "enrich", "other2"), (3, "full", "full")):
        path = tmp_path / f"{n}.json"
        path.write_text(json.dumps({"runMeta": {"mode": mode, "startedAt": "2026-11-04T00:00:00Z",
                                               "finishedAt": "2026-11-04T00:01:00Z"},
            "sources": [{"sourceKey": "newmark"}],
            "listings": [{"sourceKey": "newmark", "id": identity, "url": f"https://example.test/{identity}",
                          "name": "Synthetic", "transactionType": "sale"}]}))
        inputs += ["--in", str(path)]
    out = tmp_path / "sql"
    result = subprocess.run([sys.executable, "cre_ingest.py", *inputs, "--dry-run", "--keep-artifacts", str(out)],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    sql = (out / "ingest.sql").read_text()
    branch = sql.split("raw_data          = ", 1)[1].split(",\n        source_lastmod", 1)[0]
    assert "EXCLUDED.external_id IN ('other', 'other2')" in branch
    assert "('newmark', 'other')" in branch and "('newmark', 'other2')" in branch
    assert "'same'" not in branch and "'full'" not in branch
