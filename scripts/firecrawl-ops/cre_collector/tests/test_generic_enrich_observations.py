"""Synthetic held enrichment data does not become canonical listing output."""

import json
import sys

import pytest

import cre_enrich
import cre_ingest
from source_evidence import collector, events
from test_cre_enrich import _Args, _wire_run


def artifact(listings=None):
    return {"runMeta": {"mode": "enrich", "finishedAt": "2026-10-28T12:00:00Z"},
            "sources": [{"sourceKey": "svn", "retainedObservations": [
                {"sourceUrl": f"https://example.test/property/{i}", "identity": "unverified",
                 "pageContext": {"unknown": [None, False, "", i]}} for i in range(78)]}],
            "listings": list(listings or [])}


def test_observation_only_survives_private_conversion_without_listing_rows():
    original = artifact()
    wrapped = collector.convert(original, source_key="svn")
    assert wrapped["schema"] == collector.SCHEMA_V2 and wrapped["records"] == []
    assert collector.reconstruct(wrapped) == original
    plan = events.plan(wrapped, source_key="svn")
    assert len(plan["events"]) == 79
    assert all(row["facts"][0]["kind"] == "source_observation" for row in plan["events"][1:])


def test_observation_only_never_ingests_or_completes_claim(monkeypatch, tmp_path):
    claims = [("11111111-1111-1111-1111-111111111111", "svn", "one",
               "https://example.test/property/0", "new", "0")]
    calls = _wire_run(monkeypatch, tmp_path, claimed=claims, write_enriched=artifact())
    assert cre_enrich.run(_Args()) == 0
    assert calls["ingest_called"] is False
    assert all("DELETE FROM credeals.cre_enrichment_queue" not in sql for sql in calls["exec_sql"])
    assert any("attempts = attempts + 1" in sql for sql in calls["exec_sql"])


def test_direct_ingest_refuses_observation_only_artifact_before_database(monkeypatch, tmp_path):
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(artifact()))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(path), "--dry-run"])
    monkeypatch.setattr(cre_ingest, "load_db_url", lambda *args: pytest.fail("unexpected database access"))
    with pytest.raises(SystemExit, match="nothing to ingest"):
        cre_ingest.main()


def test_mixed_artifact_completes_only_exact_emitted_listing_url(monkeypatch, tmp_path):
    claims = [("11111111-1111-1111-1111-111111111111", "svn", "one",
               "https://example.test/property/0", "new", "0"),
              ("22222222-2222-2222-2222-222222222222", "svn", "two",
               "https://example.test/property/1", "new", "0")]
    payload = artifact([{"sourceKey": "svn", "url": claims[0][3], "description": "synthetic detail"}])
    calls = _wire_run(monkeypatch, tmp_path, claimed=claims, write_enriched=payload)
    assert cre_enrich.run(_Args()) == 0 and calls["ingest_called"] is True
    done = next(sql for sql in calls["exec_sql"] if "DELETE FROM credeals.cre_enrichment_queue" in sql)
    assert claims[0][0] in done and claims[1][0] not in done
    retry = next(sql for sql in calls["exec_sql"] if "attempts = attempts + 1" in sql)
    assert claims[1][0] in retry and claims[0][0] not in retry
    assert "--mark-missing" not in calls["ingest_argv"] and "--activate-status" not in calls["ingest_argv"]


def test_generic_thin_row_is_excluded_from_ingest_and_completion(monkeypatch, tmp_path):
    claims = [("11111111-1111-1111-1111-111111111111", "svn", "one",
               "https://example.test/property/0", "new", "0"),
              ("22222222-2222-2222-2222-222222222222", "svn", "two",
               "https://example.test/property/1", "new", "0")]
    thin = {"sourceKey": "svn", "url": claims[0][3], "id": "one", "genericEnrich": {"hadJsonLd": True}}
    useful = {"sourceKey": "svn", "url": claims[1][3], "genericEnrich": {"hadJsonLd": True}, "latitude": 0}
    payload = artifact([thin, useful])
    calls = _wire_run(monkeypatch, tmp_path, claimed=claims, write_enriched=payload)
    assert cre_enrich.run(_Args()) == 0 and calls["ingest_called"] is True
    assert calls["ingest_argv"][calls["ingest_argv"].index("--in") + 1] == calls["collect_out_path"]
    assert cre_enrich.validate_enriched_artifact(
        [{"url": row[3], "source_key": row[1]} for row in claims], payload) == [useful]
    with open(calls["collect_out_path"]) as stream:
        assert len(json.load(stream)["sources"][0]["retainedObservations"]) == 78
    done = next(sql for sql in calls["exec_sql"] if "DELETE FROM credeals.cre_enrichment_queue" in sql)
    assert claims[1][0] in done and claims[0][0] not in done
    retry = next(sql for sql in calls["exec_sql"] if "attempts = attempts + 1" in sql)
    assert claims[0][0] in retry and claims[1][0] not in retry


@pytest.mark.parametrize("key,value,expected", [
    ("name", "  ", False), ("description", "text", True), ("salePriceText", "0", True),
    ("salePriceUsd", 0, True), ("latitude", 0.0, True), ("longitude", float("nan"), False),
    ("salePriceUsd", True, False), ("latitude", float("inf"), False),
])
def test_generic_detail_qualification_requires_substantive_scalar(key, value, expected):
    assert cre_ingest.generic_enrichment_substantive({key: value}) is expected


def test_direct_ingest_refuses_generic_thin_only_before_database(monkeypatch, tmp_path):
    value = artifact([{"sourceKey": "svn", "url": "https://example.test/property/0",
                       "genericEnrich": {"hadJsonLd": True}}])
    path = tmp_path / "thin.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(path), "--dry-run"])
    monkeypatch.setattr(cre_ingest, "load_db_url", lambda *args: pytest.fail("unexpected database access"))
    with pytest.raises(SystemExit, match="nothing to ingest"):
        cre_ingest.main()
