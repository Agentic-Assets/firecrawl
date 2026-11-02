"""AGENTIC-5143: the Python guard predicate matches the TypeScript filter through one fixture.

``tests/fixtures/document_boilerplate_rules.json`` is read by ``tests/ts/lib/document-boilerplate.test.ts``
(``lib/document-boilerplate.ts``), by this file (``cre_document_boilerplate.py``) and by
``tests/run_document_boilerplate_guard_postgres_contract.py`` (the SQL on a real PostgreSQL), so a
rule changed in one implementation and not the others fails a test instead of drifting.

The regexes here are the strings the SQL uses, run under ``re``; the PostgreSQL harness is the proof
that PostgreSQL reads them the same way.
"""

import json
import re
from pathlib import Path

import pytest

import cre_document_boilerplate as boilerplate

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "document_boilerplate_rules.json").read_text()
)


def test_rule_list_equals_the_shared_fixture_in_order():
    assert [
        {"name": rule.name, "hosts": list(rule.hosts), "pathPatterns": list(rule.path_patterns)}
        for rule in boilerplate.DOCUMENT_BOILERPLATE_RULES
    ] == [
        {"name": rule["name"], "hosts": rule["hosts"], "pathPatterns": rule["pathPatterns"]}
        for rule in FIXTURE["rules"]
    ]


def test_every_rule_is_host_scoped():
    assert all(rule.hosts for rule in boilerplate.DOCUMENT_BOILERPLATE_RULES)


def test_fixture_is_stamped_with_the_getcredata_producer_it_mirrors():
    producer = FIXTURE["producer"]
    assert producer["repo"] == "Agentic-Assets/GetCREdata"
    assert producer["file"] == "documents/host_rules.py"
    for key in ("commit", "blob", "checked_on_origin_main"):
        assert re.fullmatch(r"[0-9a-f]{40}", producer[key]), key
    mirrored = set(producer["mirrored_rows"])
    by_name = {rule["name"]: rule for rule in FIXTURE["rules"]}
    assert mirrored <= by_name.keys()
    assert {name for name, rule in by_name.items() if rule["origin"] == "getcredata"} == mirrored


@pytest.mark.parametrize("url", FIXTURE["denied"])
def test_predicate_matches_every_denied_url(url):
    assert boilerplate.matches_boilerplate_url(url), url


@pytest.mark.parametrize("url", FIXTURE["kept"])
def test_predicate_does_not_match_kept_urls(url):
    assert not boilerplate.matches_boilerplate_url(url), url


def test_generated_sql_embeds_the_same_strings_the_emulation_uses():
    sql = boilerplate.document_boilerplate_sql("d.url")
    assert boilerplate.sql_literal(boilerplate.URL_HOST_RE) in sql
    assert boilerplate.sql_literal(boilerplate.URL_PATH_RE) in sql
    for rule in boilerplate.DOCUMENT_BOILERPLATE_RULES:
        assert boilerplate.sql_literal(boilerplate.host_regex(rule)) in sql
        if rule.path_patterns:
            assert boilerplate.sql_literal(boilerplate.path_regex(rule)) in sql
    assert "COALESCE(" in sql and sql.endswith(", false)")
