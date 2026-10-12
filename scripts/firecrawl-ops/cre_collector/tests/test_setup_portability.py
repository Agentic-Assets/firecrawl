"""Client discovery honors overrides without assuming a Mac installation."""

import os

import pytest

import cre_ingest


def test_psql_override_precedes_platform_candidates(tmp_path, monkeypatch):
    override = tmp_path / "custom-psql"
    fallback = tmp_path / "apt-psql"
    for path in (override, fallback):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    monkeypatch.setattr(cre_ingest, "PSQL_CANDIDATES", [str(override), str(fallback)])
    assert cre_ingest.find_psql() == str(override)


def test_psql_path_fallback_and_actionable_missing_client(monkeypatch):
    monkeypatch.setattr(cre_ingest, "PSQL_CANDIDATES", [""])
    monkeypatch.setattr(cre_ingest.shutil, "which", lambda _name: "/synthetic/psql")
    assert cre_ingest.find_psql() == "/synthetic/psql"
    monkeypatch.setattr(cre_ingest.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit, match="apt install postgresql-client"):
        cre_ingest.find_psql()


def test_linux_client_locations_are_candidates():
    assert "/usr/bin/psql" in cre_ingest.PSQL_CANDIDATES
    if os.path.isfile("/usr/bin/psql"):
        assert cre_ingest.find_psql()
