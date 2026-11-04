"""Synthetic monitor target/transport contracts; no database or providers."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cre_monitor as monitor
import pytest
from cre_ingest import database_target_fingerprint_from_url

URL = "postgresql://worker:credential-canary@db.example.test:5432/postgres?sslmode=require"
TARGET = database_target_fingerprint_from_url(URL)["value"]


def invoke(monkeypatch, tmp_path, *flags, empty=False):
    artifact = tmp_path / "monitor.json"
    artifact.write_text(
        json.dumps(
            {
                "runMeta": {"mode": "monitor", "transactions": ["sale", "lease"]},
                "sources": [
                    {
                        "sourceKey": "interra-realty",
                        "transaction": tx,
                        "supported": True,
                        "truncated": False,
                        "listingsCollected": 0 if empty else 1,
                    }
                    for tx in ("sale", "lease")
                ],
                "listings": []
                if empty
                else [
                    {
                        "sourceKey": "interra-realty",
                        "id": tx,
                        "transactionMode": tx,
                        "url": "https://example.test/" + tx,
                        "name": "Synthetic",
                    }
                    for tx in ("sale", "lease")
                ],
            }
        )
    )
    monkeypatch.setattr(
        sys, "argv", ["cre_monitor.py", "--in", str(artifact), "--quiet", *flags]
    )
    return monitor.main()


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--apply", "--dry-run"]])
def test_default_and_explicit_dry_run_never_load_database(monkeypatch, tmp_path, flags):
    def unexpected(*_args, **_kwargs):
        pytest.fail("dry-run attempted database access")

    monkeypatch.setattr(monitor, "load_db_url", unexpected)
    monkeypatch.setattr(monitor, "load_prior_state", unexpected)
    monkeypatch.setattr(monitor.subprocess, "run", unexpected)
    invoke(monkeypatch, tmp_path, *flags, "--expected-db-target-sha256", "invalid")


@pytest.mark.parametrize("target", ["0" * 64, "invalid", "A" * 64])
def test_target_refusal_precedes_any_prior_read_or_write(monkeypatch, tmp_path, target):
    monkeypatch.setattr(monitor, "load_db_url", lambda _: (URL, "/private/cre.env"))

    def unexpected(*_args, **_kwargs):
        pytest.fail("target refusal happened after database access")

    monkeypatch.setattr(monitor, "load_prior_state", unexpected)
    monkeypatch.setattr(monitor.subprocess, "run", unexpected)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, tmp_path, "--apply", "--expected-db-target-sha256", target)
    assert "credential-canary" not in str(error.value)
    assert URL not in str(error.value)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("rc", [0, 1])
@pytest.mark.parametrize("state", ["baseline", "unmatched", "price_changed"])
def test_apply_retains_existing_single_transaction_and_legacy_invocation(
    monkeypatch, tmp_path, bound, rc, state
):
    calls = []
    monkeypatch.setattr(monitor, "load_db_url", lambda path: (URL, path))
    monkeypatch.setattr(monitor, "find_psql", lambda: "psql")
    monkeypatch.setattr(monitor.tempfile, "mkdtemp", lambda **_: str(tmp_path))

    def prior(url, slugs):
        calls.append(("prior", url, slugs))
        bid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        index = (
            {}
            if state == "baseline"
            else {
                (bid, "sale"): {
                    "fingerprint": "previous-fingerprint",
                    "source_key": "interra-realty",
                    "observed_status": None,
                    "url": "https://example.test/sale",
                    "observation_present": True,
                }
            }
        )
        listings = (
            {
                (bid, "sale"): {
                    "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "status": "active",
                    "deleted": False,
                }
            }
            if state == "price_changed"
            else {}
        )
        return {"interra-realty": bid}, index, listings, {}

    def dispatch(argv, **kwargs):
        sql = Path(argv[argv.index("-f") + 1]).read_text()
        calls.append(("write", argv, kwargs, sql))
        return SimpleNamespace(returncode=rc)

    monkeypatch.setattr(monitor, "load_prior_state", prior)
    monkeypatch.setattr(monitor.subprocess, "run", dispatch)
    flags = ["--apply", "--env-file", "/private/cre.env"]
    if bound:
        flags += ["--expected-db-target-sha256", TARGET]
    if rc:
        with pytest.raises(SystemExit):
            invoke(monkeypatch, tmp_path, *flags)
    else:
        invoke(monkeypatch, tmp_path, *flags)
    assert [c[0] for c in calls] == ["prior", "write"]
    _, argv, kwargs, sql = calls[1]
    assert URL not in argv and "credential-canary" not in " ".join(argv)
    assert kwargs["env"]["PGPASSWORD"] == "credential-canary"
    assert kwargs["env"]["PGHOST"] == "db.example.test"
    assert kwargs["env"]["PGSSLMODE"] == "require"
    assert "ON_ERROR_STOP=1" in argv
    assert sql.count("BEGIN;") == 1 and sql.count("COMMIT;") == 1
    assert "cre_source_index" in sql
    assert ("cre_enrichment_queue" in sql) == (state == "price_changed")
    assert "UPDATE credeals.cre_listings SET status" not in sql


def test_prior_read_credentials_use_environment_not_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "find_psql", lambda: "psql")

    def dispatch(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="one\ttwo\n", stderr="")

    monkeypatch.setattr(monitor.subprocess, "run", dispatch)
    assert monitor._psql_read(URL, "SELECT 1;") == [("one", "two")]
    argv, kwargs = calls[0]
    assert URL not in argv and "credential-canary" not in " ".join(argv)
    assert kwargs["env"]["PGPASSWORD"] == "credential-canary"
    assert kwargs["env"]["PGSSLMODE"] == "require"


def test_clean_zero_artifact_apply_is_database_free_noop(monkeypatch, tmp_path):
    def unexpected(*_args, **_kwargs):
        pytest.fail("empty artifact accessed database")

    monkeypatch.setattr(monitor, "load_db_url", unexpected)
    monkeypatch.setattr(monitor.subprocess, "run", unexpected)
    invoke(
        monkeypatch,
        tmp_path,
        "--apply",
        "--expected-db-target-sha256",
        TARGET,
        empty=True,
    )
