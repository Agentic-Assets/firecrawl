"""Offline abstract-socket identity and credential-free connection contracts."""

import hashlib
from urllib.parse import SplitResult, urlsplit

import cre_ingest as ingest
import pytest


def socket_url(host, *, port="", user="user:synthetic@", query=""):
    return f"postgresql://{user}{host}{port}/postgres{query}"


def fingerprint(url):
    return ingest.database_target_fingerprint_from_url(url)


@pytest.mark.parametrize(
    "host", ["%40Case", "%40%43ase", "%40Ca%73e", "%40%43%61%73%65"]
)
def test_abstract_socket_case_and_encoded_equivalence_bind_the_same_exact_name(host):
    url = socket_url(host)
    expected = hashlib.sha256(b"@Case:5432/postgres").hexdigest()
    assert fingerprint(url) == {"algorithm": "sha256", "value": expected}
    assert fingerprint(url) != fingerprint(socket_url("%40case"))
    assert ingest.psql_connection_env(url)["PGHOST"] == "@Case"
    with pytest.raises(SystemExit, match="does not match"):
        ingest.assert_expected_database_target(socket_url("%40case"), expected)


@pytest.mark.parametrize("user", ["", "user:synthetic@", "other:rotated%40password@"])
@pytest.mark.parametrize("port", ["", ":5432", ":6543"])
def test_abstract_socket_credentials_do_not_change_identity_or_leak_into_args(
    user, port
):
    url = socket_url("%40Case", user=user, port=port, query="?keepalives=1")
    assert fingerprint(url) == fingerprint(socket_url("%40%43ase", port=port))
    assert ingest.psql_connection_env(url)["PGHOST"] == "@Case"
    assert ingest.psql_connection_args(url) == [
        "--dbname",
        f"postgresql://%40Case{port}/postgres?keepalives=1",
    ]


@pytest.mark.parametrize(
    "host", ["%40Case.", "%40Case%3Aworker", "%40Case%2Fworker", "%40Case%40worker"]
)
def test_abstract_names_keep_trailing_dot_and_encoded_name_characters(host):
    url = socket_url(host, query="?keepalives=1")
    assert fingerprint(url) != fingerprint(socket_url("%40Case"))
    assert fingerprint(url) != fingerprint(socket_url(host.lower()))
    args = ingest.psql_connection_args(url)
    assert fingerprint(args[1]) == fingerprint(url)
    assert (
        ingest.psql_connection_env(args[1])["PGHOST"]
        == ingest.psql_connection_env(url)["PGHOST"]
    )


def test_abstract_socket_case_survives_a_lowercasing_hostname_parser(monkeypatch):
    class LowercaseHostname(SplitResult):
        @property
        def hostname(self):
            return super().hostname.lower()

    monkeypatch.setattr(
        ingest, "urlsplit", lambda url: LowercaseHostname(*urlsplit(url))
    )
    url = socket_url("%40Case", query="?keepalives=1")
    assert (
        fingerprint(url)["value"] == hashlib.sha256(b"@Case:5432/postgres").hexdigest()
    )
    assert ingest.psql_connection_env(url)["PGHOST"] == "@Case"
    assert ingest.psql_connection_args(url) == [
        "--dbname",
        "postgresql://%40Case/postgres?keepalives=1",
    ]


@pytest.mark.parametrize(
    "host,canonical",
    [
        ("DB.Example.Test.", "db.example.test"),
        ("%44B.example.test", "db.example.test"),
        ("[2001:DB8::1]", "2001:db8::1"),
        ("%2Ftmp%2F%43ase", "/tmp/Case"),
    ],
)
def test_other_transports_keep_legacy_fingerprints(host, canonical):
    expected = hashlib.sha256(f"{canonical}:5432/postgres".encode()).hexdigest()
    assert fingerprint(socket_url(host)) == {"algorithm": "sha256", "value": expected}


@pytest.mark.parametrize("host", ["%40Case%ZZ", "%40Case%FF", "%40Case%2Cother"])
def test_abstract_socket_malformed_or_multi_host_targets_still_fail_closed(host):
    with pytest.raises(ValueError):
        fingerprint(socket_url(host))


def test_abstract_socket_named_like_pooler_keeps_direct_case_sensitive_identity():
    host = "@Case.pooler.supabase.com"
    url = socket_url("%40Case.pooler.supabase.com", user="")
    expected = hashlib.sha256(f"{host}:5432/postgres".encode()).hexdigest()
    assert fingerprint(url)["value"] == expected


@pytest.mark.parametrize(
    "key", ["host", "hostaddr", "port", "dbname", "service", "servicefile"]
)
def test_abstract_socket_query_target_override_still_fails_closed(key):
    with pytest.raises(ValueError, match="may override the target"):
        fingerprint(socket_url("%40Case", query=f"?{key}=other"))


@pytest.mark.parametrize(
    "host",
    ["%40Case%00", "%40Case%0A", "%40Case%0D", "%40Case%09", "%40Case%7F"],
)
@pytest.mark.parametrize(
    "connection", [fingerprint, ingest.psql_connection_env, ingest.psql_connection_args]
)
def test_encoded_abstract_socket_controls_fail_without_exposing_uri(host, connection):
    url = socket_url(host, query="?keepalives=1")
    with pytest.raises(ValueError, match="contains a control character") as caught:
        connection(url)
    assert "synthetic" not in str(caught.value)
    assert host not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        socket_url("%40Ca\nse"),
        socket_url("%40Ca\rse"),
        socket_url("%40Ca\tse"),
        socket_url("%4\n0Case"),
        socket_url("%40Case", user="user:syn\tthetic@"),
        "\x00" + socket_url("%40Case"),
    ],
)
@pytest.mark.parametrize(
    "connection", [fingerprint, ingest.psql_connection_env, ingest.psql_connection_args]
)
def test_literal_abstract_uri_controls_are_not_silently_stripped(url, connection):
    url += "?keepalives=1"
    with pytest.raises(ValueError, match="contains a control character") as caught:
        connection(url)
    assert "synthetic" not in str(caught.value)
    assert "Case" not in str(caught.value)
