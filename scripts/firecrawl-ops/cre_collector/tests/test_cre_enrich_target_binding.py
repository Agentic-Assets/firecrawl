"""Enrichment target binding must precede claims and survive child env drift."""

from types import SimpleNamespace

import pytest

import cre_enrich
from cre_ingest import database_target_fingerprint_from_url


URL = "postgresql://worker:private@db.example.test:5432/postgres"
TARGET = database_target_fingerprint_from_url(URL)["value"]


def args(**overrides):
    return SimpleNamespace(
        **dict(batch=1, source="hanley", dry_run=False, env_file="/private/cre.env")
        | overrides
    )


@pytest.mark.parametrize("fingerprint", ["0" * 64, "invalid", "A" * 64])
def test_target_rejection_precedes_claim(monkeypatch, fingerprint):
    monkeypatch.setattr(cre_enrich, "load_db_url", lambda _: (URL, "/private/cre.env"))
    calls = []
    monkeypatch.setattr(cre_enrich, "_psql_query", lambda *a: calls.append(a))
    with pytest.raises(SystemExit) as error:
        cre_enrich.run(args(expected_db_target_sha256=fingerprint))
    assert not calls
    assert URL not in str(error.value)
    assert "private@" not in str(error.value)


def test_matching_target_allows_empty_queue(monkeypatch):
    monkeypatch.setattr(cre_enrich, "load_db_url", lambda _: (URL, "/private/cre.env"))
    calls = []

    def query(url, sql):
        calls.append(url)
        return []

    monkeypatch.setattr(cre_enrich, "_psql_query", query)
    assert cre_enrich.run(args(expected_db_target_sha256=TARGET)) == 0
    assert calls == [URL]


@pytest.mark.parametrize("ingest_rc", [0, 1])
def test_ingest_receives_same_expected_target(monkeypatch, tmp_path, ingest_rc):
    import json

    monkeypatch.setattr(cre_enrich, "load_db_url", lambda _: (URL, "/private/cre.env"))
    monkeypatch.setattr(cre_enrich, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(
        cre_enrich,
        "_psql_query",
        lambda *_: [("row1", "hanley", "id1", "https://example.test/1", "new", "0")],
    )
    mutations = []
    monkeypatch.setattr(cre_enrich, "_psql_exec", lambda *a: mutations.append(a))
    calls = []

    def worker(argv, **kwargs):
        calls.append(argv)
        if "collect.ts" in argv:
            payload = {
                "runMeta": {"mode": "enrich"},
                "listings": [{"sourceKey": "hanley", "url": "https://example.test/1"}],
            }
            with open(argv[argv.index("--out") + 1], "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        return SimpleNamespace(returncode=0 if "collect.ts" in argv else ingest_rc)

    monkeypatch.setattr(cre_enrich.subprocess, "run", worker)
    assert cre_enrich.run(args(expected_db_target_sha256=TARGET)) == ingest_rc
    ingest = next(a for a in calls if any(str(x).endswith("cre_ingest.py") for x in a))
    assert ingest[ingest.index("--expected-db-target-sha256") + 1] == TARGET
    assert URL not in " ".join(ingest)
    if ingest_rc:
        assert not mutations, (
            "uncertain bound ingest must not release or complete claims"
        )


def test_dry_run_never_loads_credentials(monkeypatch):
    def unexpected(*_):
        pytest.fail("dry-run attempted credential or database access")

    monkeypatch.setattr(cre_enrich, "load_db_url", unexpected)
    monkeypatch.setattr(cre_enrich, "_psql_query", unexpected)
    assert cre_enrich.run(args(dry_run=True, expected_db_target_sha256=TARGET)) == 0
