"""Offline target admission contracts for Supabase's username-routed pooler."""

import hashlib
import json

import cre_ingest as ingest
import pytest

PROJECT_A = "a" * 20
PROJECT_B = "b" * 20
POOLER = "aws-0-us-east-1.pooler.supabase.com"


def pooled(user, *, query="", password="synthetic", host=POOLER, port=5432):
    authority = f"{user}:{password}@" if user is not None else ""
    return f"postgresql://{authority}{host}:{port}/postgres{query}"


def fingerprint(url):
    return ingest.database_target_fingerprint_from_url(url)


def test_shared_pooler_tenants_cannot_pass_each_others_checkpoint_binding():
    first = pooled(f"postgres.{PROJECT_A}")
    second = pooled(f"postgres.{PROJECT_B}")
    assert fingerprint(first) != fingerprint(second)
    with pytest.raises(SystemExit, match="does not match"):
        ingest.assert_expected_database_target(second, fingerprint(first)["value"])


def test_pooler_identity_ignores_role_and_password_rotation_but_not_port():
    first = pooled(f"postgres.{PROJECT_A}")
    rotated = pooled(f"collector.{PROJECT_A}", password="rotated")
    assert fingerprint(first) == fingerprint(rotated)
    assert fingerprint(first) != fingerprint(pooled(f"postgres.{PROJECT_A}", port=6543))
    assert "synthetic" not in json.dumps(fingerprint(first))
    assert PROJECT_A not in json.dumps(fingerprint(first))


def test_query_only_user_and_same_tenant_override_match_effective_psql_user():
    first = pooled(f"postgres.{PROJECT_A}")
    query_only = pooled(None, query=f"?user=collector.{PROJECT_A}")
    overridden = pooled(f"postgres.{PROJECT_A}", query=f"?UsEr=collector.{PROJECT_A}")
    assert fingerprint(first) == fingerprint(query_only) == fingerprint(overridden)
    assert ingest.psql_connection_env(overridden)["PGUSER"] == f"collector.{PROJECT_A}"


def test_cross_tenant_query_override_is_refused_before_connection():
    url = pooled(f"postgres.{PROJECT_A}", query=f"?user=postgres.{PROJECT_B}")
    with pytest.raises(ValueError, match="conflicting project routing"):
        fingerprint(url)


@pytest.mark.parametrize(
    "query",
    [
        f"?options=reference%3D{PROJECT_B}",
        f"?options=-c%20reference%3D{PROJECT_B}",
        f"?options=--reference%3D{PROJECT_B}",
        "?options=-c%20statement_timeout%3D15000",
        f"?OPTIONS=reference%3D{PROJECT_B}&options=",
    ],
)
def test_alternate_startup_routing_is_conservatively_refused(query):
    with pytest.raises(ValueError, match="startup options may override"):
        fingerprint(pooled(f"postgres.{PROJECT_A}", query=query))


@pytest.mark.parametrize("query", ["", f"?user=postgres.cluster.{PROJECT_A}"])
def test_cluster_user_routing_is_refused(query):
    with pytest.raises(ValueError, match="cluster routing"):
        fingerprint(pooled(f"postgres.cluster.{PROJECT_A}", query=query))


def test_empty_startup_options_do_not_change_target():
    assert fingerprint(
        pooled(f"postgres.{PROJECT_A}", query="?options=")
    ) == fingerprint(pooled(f"postgres.{PROJECT_A}"))


@pytest.mark.parametrize(
    ("host", "encoded_host"),
    [
        ("/tmp/local.pooler.supabase.com", "%2Ftmp%2Flocal.pooler.supabase.com"),
        ("@local.pooler.supabase.com", "%40local.pooler.supabase.com"),
    ],
)
def test_unix_socket_named_like_a_pooler_retains_direct_identity(host, encoded_host):
    expected = hashlib.sha256(f"{host}:5432/postgres".encode()).hexdigest()
    assert (
        fingerprint(f"postgresql://user:synthetic@{encoded_host}/postgres")["value"]
        == expected
    )


@pytest.mark.parametrize(
    "user",
    [
        None,
        "postgres",
        "." + PROJECT_A,
        "postgres.short",
        "postgres." + "A" * 20,
        "postgres." + "1" * 20,
        "postgres.%ZZ",
        "postgres.%FF",
        "bad%00." + PROJECT_A,
    ],
)
def test_missing_malformed_and_ambiguous_pooler_users_fail_closed(user):
    with pytest.raises(ValueError):
        fingerprint(pooled(user))


@pytest.mark.parametrize(
    "query",
    [
        "?user=",
        "?user=postgres",
        f"?user=postgres.{PROJECT_A}&USER=postgres.{PROJECT_A}",
    ],
)
def test_invalid_or_repeated_query_routing_fails_closed(query):
    with pytest.raises(ValueError):
        fingerprint(pooled(f"postgres.{PROJECT_A}", query=query))


def test_encoded_pooler_user_and_canonical_dns_bind_the_same_project():
    assert fingerprint(
        pooled(f"postgres%2E{PROJECT_A}", host=POOLER.upper() + ".")
    ) == fingerprint(pooled(f"postgres.{PROJECT_A}"))


@pytest.mark.parametrize(
    "host",
    ["db.example.test", "db.project.supabase.co", "pooler.supabase.com.attacker.test"],
)
def test_direct_and_unrelated_hosts_retain_legacy_identity(host):
    url = f"postgresql://user:synthetic@{host}/postgres"
    expected = hashlib.sha256(f"{host}:5432/postgres".encode()).hexdigest()
    assert fingerprint(url) == {"algorithm": "sha256", "value": expected}
    assert fingerprint(url) == fingerprint(
        url.replace("user:synthetic", "other:rotated")
    )
