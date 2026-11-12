"""Unbound Newmark enrichment stays out of canonical ingest staging."""

import copy
import json
import subprocess
import sys

import pytest

from cre_ingest import hold_unbound_newmark_enrichment


def artifact(*, mode="enrich"):
    return {
        "runMeta": {"mode": mode, "startedAt": "2026-11-11T12:00:00Z",
                    "finishedAt": "2026-11-11T12:00:00Z"},
        "sources": [
            {"sourceKey": "newmark", "transaction": "sale", "listingsCollected": 1,
             "retainedObservations": [{"newmarkRcmObservation": True}]},
            {"sourceKey": "cbre", "transaction": "sale", "listingsCollected": 1},
        ],
        "listings": [
            {"sourceKey": "newmark", "id": "held", "url": "https://example.test/held",
             "name": "Held unbound observation", "salePriceUsd": 1234567},
            {"sourceKey": "cbre", "id": "peer", "url": "https://example.test/peer",
             "name": "Eligible peer"},
        ],
        "brokers": [],
    }


def test_partition_retains_original_and_unrelated_peers():
    original = artifact()
    before = copy.deepcopy(original)
    filtered, rows, sources = hold_unbound_newmark_enrichment(original, page_attempt=None)
    assert (rows, sources) == (1, 1)
    assert original == before
    assert [row["sourceKey"] for row in filtered["listings"]] == ["cbre"]
    assert [row["sourceKey"] for row in filtered["sources"]] == ["cbre"]


def test_full_inventory_and_page_attempt_keep_existing_admission():
    full = artifact(mode="full")
    page = artifact()
    assert hold_unbound_newmark_enrichment(full, page_attempt=None) == (full, 0, 0)
    assert hold_unbound_newmark_enrichment(page, page_attempt="a" * 64) == (page, 0, 0)


@pytest.mark.parametrize("field", ["listings", "sources"])
def test_enrichment_partition_refuses_malformed_arrays(field):
    data = artifact()
    data[field] = {"unexpected": "shape"}
    with pytest.raises(ValueError, match="listings and sources arrays"):
        hold_unbound_newmark_enrichment(data, page_attempt=None)


def test_mixed_cli_generates_only_peer_sql_and_reports_partial(tmp_path):
    path, sql_dir = tmp_path / "artifact.json", tmp_path / "sql"
    original = json.dumps(artifact()).encode()
    path.write_bytes(original)
    result = subprocess.run(
        [sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run",
         "--keep-artifacts", str(sql_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 20, result.stderr
    assert "holding unbound Newmark enrichment" in result.stderr
    assert str(path) in result.stderr
    assert path.read_bytes() == original
    sql = (sql_dir / "ingest.sql").read_text()
    assert "Eligible peer" in sql
    assert "Held unbound observation" not in sql
    assert "1234567" not in sql


def test_only_unbound_cli_reports_partial_without_sql_or_db(tmp_path):
    path, sql_dir = tmp_path / "artifact.json", tmp_path / "sql"
    data = artifact()
    data["listings"] = data["listings"][:1]
    data["sources"] = data["sources"][:1]
    original = json.dumps(data).encode()
    path.write_bytes(original)
    result = subprocess.run(
        [sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run",
         "--keep-artifacts", str(sql_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 20, result.stderr
    assert "no Newmark enrichment write was admitted" in result.stderr
    assert not sql_dir.exists()
    assert path.read_bytes() == original


def test_published_detail_observation_is_explicitly_held_without_rows(tmp_path):
    data = artifact()
    data["listings"] = []
    data["sources"] = data["sources"][:1]
    path = tmp_path / "published.json"
    original = json.dumps(data).encode()
    path.write_bytes(original)
    result = subprocess.run(
        [sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 20, result.stderr
    assert "0 listing(s), 1 source result(s)" in result.stderr
    assert path.read_bytes() == original


def test_full_inventory_cli_retains_existing_path(tmp_path):
    data = artifact(mode="full")
    path, sql_dir = tmp_path / "full.json", tmp_path / "sql"
    path.write_text(json.dumps(data))
    result = subprocess.run(
        [sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run",
         "--keep-artifacts", str(sql_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "holding unbound Newmark" not in result.stderr
    assert "Held unbound observation" in (sql_dir / "ingest.sql").read_text()
