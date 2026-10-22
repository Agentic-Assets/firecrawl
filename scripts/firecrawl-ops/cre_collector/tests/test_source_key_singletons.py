"""Singleton attribution optimization; old SQL oracle is frozen independently."""

import hashlib
from pathlib import Path

import cre_ingest as ingest
import cre_validate as validate
import pytest

ORACLE = Path(__file__).parent / "fixtures/source_key_sql_before_singletons.sql"
ORACLE_SHA256 = "1fb36cd434c81b66b13a77be22970ca76b9d9554533c8bf78477a1dd16bb9b52"


def singleton_clause(sql):
    return next(line for line in sql.splitlines() if line.endswith("THEN b.slug"))


def test_old_oracle_is_frozen_and_slow_path_stays_exact():
    assert hashlib.sha256(ORACLE.read_bytes()).hexdigest() == ORACLE_SHA256
    sql = ingest.source_key_sql()
    fast_line = singleton_clause(sql)
    assert sql.replace(fast_line + "\n", "").strip() == ORACLE.read_text().strip()


def test_all_current_singletons_have_exact_legacy_slug_fallback():
    singleton_slugs = {
        slug for slug, keys in ingest.SOURCE_KEYS_BY_SLUG.items() if len(keys) == 1
    }
    assert len(ingest.SOURCE_TO_BROKERAGE) == 51
    assert len(singleton_slugs) == 45
    clause = singleton_clause(ingest.source_key_sql())
    for slug in singleton_slugs:
        assert ingest.SOURCE_KEYS_BY_SLUG[slug] == {slug}
        assert f"'{slug}'" in clause
        assert ingest.source_key_from_values({}, slug, "fixture-id") == slug
    for slug in ("cbre", "jll", "colliers"):
        assert f"'{slug}'" not in clause


@pytest.mark.parametrize("keys", [{"svn", "future"}, {"different"}, set()])
def test_non_singleton_or_non_fallback_mapping_never_enters_fast_path(
    monkeypatch, keys
):
    mapping = dict(ingest.SOURCE_KEYS_BY_SLUG)
    mapping["svn"] = keys
    monkeypatch.setattr(ingest, "SOURCE_KEYS_BY_SLUG", mapping)
    assert "'svn'" not in singleton_clause(ingest.source_key_sql())


def test_aliases_are_preserved_in_both_paths():
    sql = ingest.source_key_sql("staged", "brokerage")
    assert "THEN brokerage.slug" in sql
    assert "staged.raw_data" in sql
    assert "staged.external_id" in sql
    assert "l.raw_data" not in sql
    assert "b.slug" not in sql


def test_only_query_attribution_changes_and_profiles_still_share_sql():
    preflight = validate.queries_for_profile("checkpoint-preflight")
    full = validate.queries_for_profile("full")
    assert len(preflight) == 8
    assert all(sql == full[name] for name, sql in preflight.items())
    assert ingest.source_key_sql("decoded") in preflight["source_counts"]
    for name in ("duplicates", "child_counts", "quality_by_source"):
        assert ingest.source_key_sql() in preflight[name]


def test_empty_singleton_set_keeps_valid_original_sql(monkeypatch):
    monkeypatch.setattr(ingest, "SOURCE_KEYS_BY_SLUG", {})
    assert ingest.source_key_sql().strip() == ORACLE.read_text().strip()


@pytest.mark.parametrize("slug", ["cbre", "jll", "colliers"])
def test_historical_folded_admission_stays_slow_even_if_mapping_becomes_singleton(
    monkeypatch, slug
):
    mapping = dict(ingest.SOURCE_KEYS_BY_SLUG)
    mapping[slug] = {slug}
    monkeypatch.setattr(ingest, "SOURCE_KEYS_BY_SLUG", mapping)
    assert f"'{slug}'" not in singleton_clause(ingest.source_key_sql())


def test_native_oracle_queries_cannot_silently_compare_current_sql_with_itself(
    monkeypatch,
):
    import runpy

    runner = runpy.run_path(
        str(Path(__file__).parent / "run_source_key_postgres_contract.py")
    )
    with monkeypatch.context() as context:
        context.setitem(validate.QUERIES, "duplicates", "SELECT 1;")
        with pytest.raises(RuntimeError, match="not bound"):
            runner["queries"]()


def test_source_counts_json_root_control_is_frozen_and_compares_distinct_arms(
    tmp_path,
):
    import runpy

    runner = runpy.run_path(
        str(Path(__file__).parent / "run_source_key_postgres_contract.py")
    )
    control_path = (
        Path(__file__).parent
        / "fixtures"
        / "source_counts_before_decoded_source_key.sql"
    )
    arms = runner["queries"](control_path)["source_counts"]
    assert arms["old"] == control_path.read_text().strip().removesuffix(";")
    assert arms["new"] == validate.QUERIES["source_counts"].strip().removesuffix(";")
    assert arms["old"] != arms["new"]

    invalid_control = tmp_path / "mutable-control.sql"
    invalid_control.write_text("SELECT 1;\n")
    with pytest.raises(RuntimeError, match="control hash mismatch"):
        runner["queries"](invalid_control)
