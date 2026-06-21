"""
test_env_discovery.py

Portability contract for cre_ingest.load_db_url (2026-06-14).

The pipeline must run from any clone location / machine, so the POSTGRES_URL
env file is resolved in this precedence:

    --env-file flag  >  CRE_ENV_FILE env var  >  ~/Documents defaults

cre_monitor.py and cre_gate.py import the same loader, so this one contract
covers the whole pipeline. Pure-transform: writes temp files, reads them back,
never connects to a database, never prints the URL value.
"""

import pytest

import cre_ingest
from cre_ingest import load_db_url


def _write_env(path, url, key="POSTGRES_URL"):
    path.write_text(f"# comment line\n{key}={url}\n", encoding="utf-8")
    return str(path)


def test_explicit_env_file_wins_over_cre_env_file_and_defaults(tmp_path, monkeypatch):
    explicit = _write_env(tmp_path / "explicit.env", "postgres://explicit/db")
    other = _write_env(tmp_path / "other.env", "postgres://other/db")
    monkeypatch.setenv("CRE_ENV_FILE", other)
    monkeypatch.setattr(cre_ingest, "ENV_FILE_CANDIDATES", [other])

    url, path = load_db_url(explicit)

    assert url == "postgres://explicit/db"
    assert path == explicit


def test_cre_env_file_used_when_no_flag(tmp_path, monkeypatch):
    env = _write_env(tmp_path / "viaenv.env", "postgres://viaenv/db")
    monkeypatch.setenv("CRE_ENV_FILE", env)
    # Defaults point nowhere real so only CRE_ENV_FILE can satisfy the call.
    monkeypatch.setattr(cre_ingest, "ENV_FILE_CANDIDATES", [str(tmp_path / "nope.env")])

    url, path = load_db_url(None)

    assert url == "postgres://viaenv/db"
    assert path == env


def test_cre_env_file_beats_defaults(tmp_path, monkeypatch):
    override = _write_env(tmp_path / "override.env", "postgres://override/db")
    default = _write_env(tmp_path / "default.env", "postgres://default/db")
    monkeypatch.setenv("CRE_ENV_FILE", override)
    monkeypatch.setattr(cre_ingest, "ENV_FILE_CANDIDATES", [default])

    url, path = load_db_url(None)

    assert url == "postgres://override/db"
    assert path == override


def test_falls_back_to_defaults_when_no_flag_or_env(tmp_path, monkeypatch):
    default = _write_env(tmp_path / "default.env", "postgres://default/db")
    monkeypatch.delenv("CRE_ENV_FILE", raising=False)
    monkeypatch.setattr(cre_ingest, "ENV_FILE_CANDIDATES", [default])

    url, path = load_db_url(None)

    assert url == "postgres://default/db"
    assert path == default


def test_missing_everywhere_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("CRE_ENV_FILE", raising=False)
    monkeypatch.setattr(cre_ingest, "ENV_FILE_CANDIDATES", [str(tmp_path / "absent.env")])

    with pytest.raises(SystemExit):
        load_db_url(None)


def test_explicit_env_file_expands_user_tilde(tmp_path, monkeypatch):
    # A --env-file value that reaches load_db_url unexpanded (a literal ~) must
    # still resolve; load_db_url applies os.path.expanduser before reading. This
    # exercises the expanduser branch the code comments document.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CRE_ENV_FILE", raising=False)
    env_dir = tmp_path / "equire"
    env_dir.mkdir()
    _write_env(env_dir / ".env.local", "postgres://tilde/db")

    url, path = load_db_url("~/equire/.env.local")

    assert url == "postgres://tilde/db"
    assert path == str(env_dir / ".env.local")


def test_cre_env_file_expands_user_tilde(tmp_path, monkeypatch):
    # The CRE_ENV_FILE branch also expanduser's its value (cre_ingest.py:1158).
    monkeypatch.setenv("HOME", str(tmp_path))
    env_dir = tmp_path / "viaenv"
    env_dir.mkdir()
    _write_env(env_dir / ".env.local", "postgres://tildeenv/db")
    monkeypatch.setenv("CRE_ENV_FILE", "~/viaenv/.env.local")
    monkeypatch.setattr(cre_ingest, "ENV_FILE_CANDIDATES", [str(tmp_path / "nope.env")])

    url, path = load_db_url(None)

    assert url == "postgres://tildeenv/db"
    assert path == str(env_dir / ".env.local")


def test_non_pooling_url_preferred_over_pooling(tmp_path, monkeypatch):
    env_path = tmp_path / "both.env"
    env_path.write_text(
        "POSTGRES_URL=postgres://pooled/db\n"
        "POSTGRES_URL_NON_POOLING=postgres://direct/db\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CRE_ENV_FILE", raising=False)

    url, _ = load_db_url(str(env_path))

    assert url == "postgres://direct/db"
