"""No-DB contracts for the atomic precommit child-regression guard."""

from datetime import datetime, timezone

import pytest

import cre_checkpoint_refresh as refresh
import cre_ingest as ingest


SCRAPED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc).isoformat()


@pytest.mark.parametrize(
    "before,after,regressed",
    [
        pytest.param(100, 69, True, id="destructive-drop-over-thirty-percent"),
        pytest.param(100, 70, False, id="exact-thirty-percent-boundary"),
        pytest.param(90, 62, True, id="integer-threshold-without-float-drift"),
        pytest.param(90, 63, False, id="integer-thirty-percent-boundary"),
        pytest.param(100, 100, False, id="unchanged"),
        pytest.param(100, 125, False, id="additive"),
        pytest.param(9, 0, False, id="first-or-small-source-below-minimum"),
        pytest.param(0, 0, False, id="first-source-ingest"),
    ],
)
def test_child_count_regression_matches_checkpoint_quality_semantics(
    before, after, regressed
):
    assert ingest.child_count_regressed(before, after) is regressed


@pytest.mark.parametrize(
    "before,after,ok",
    [
        pytest.param(100, 69, False, id="checkpoint-rejects-destructive-drop"),
        pytest.param(100, 70, True, id="checkpoint-allows-threshold-boundary"),
        pytest.param(100, 100, True, id="checkpoint-allows-unchanged"),
        pytest.param(100, 125, True, id="checkpoint-allows-additive"),
        pytest.param(0, 0, True, id="checkpoint-allows-first-source"),
    ],
)
def test_checkpoint_quality_uses_the_central_child_predicate(before, after, ok):
    before_validation = {
        "queries": {
            "child_counts": [
                {"source_key": "svn", "child_type": "links", "count": str(before)}
            ]
        }
    }
    after_validation = {
        "queries": {
            "child_counts": [
                {"source_key": "svn", "child_type": "links", "count": str(after)}
            ]
        }
    }

    assert (
        refresh.compare_validation_quality(before_validation, after_validation)["ok"]
        is ok
    )


def test_build_sql_places_source_scoped_child_guard_before_commit():
    sql = ingest.build_sql([], [], SCRAPED_AT, set())

    baseline = sql.index("CREATE TEMP TABLE _child_counts_before")
    child_delete = sql.index(
        "DELETE FROM credeals.cre_listing_contacts  "
        "WHERE listing_id IN (SELECT id FROM _child_refresh)"
    )
    guard = sql.index("checkpoint child quality regression before commit")
    commit = sql.index("COMMIT;", guard)

    assert baseline < child_delete < guard < commit
    assert "JOIN _ingest_child_sources ingest_scope USING (source_key)" in sql
    assert "s.external_id LIKE 'dealflow:%'" in sql
    assert "l.external_id LIKE 'dealflow:%'" in sql
    assert "prior_counts.child_count >= 10" in sql
    assert "prior_counts.child_count * 7" in sql
    assert "/ 10" in sql
    assert "RAISE EXCEPTION" in sql[guard - 100 : commit]


def test_build_sql_counts_all_checkpoint_child_types_and_guards_optional_tables():
    sql = ingest.build_sql([], [], SCRAPED_AT, set())

    for child_type in ("contacts", "documents", "images", "media", "links"):
        assert f"'{child_type}'" in sql
    assert "to_regclass('credeals.cre_listing_media')" in sql
    assert "to_regclass('credeals.cre_listing_links')" in sql


def test_preserve_and_additive_paths_stay_out_of_wholesale_child_refresh():
    sql = ingest.build_sql([], [], SCRAPED_AT, set())

    child_refresh = sql[
        sql.index("CREATE TEMP TABLE _child_refresh") :
        sql.index("CREATE TEMP TABLE _child_additive")
    ]
    child_additive = sql[
        sql.index("CREATE TEMP TABLE _child_additive") :
        sql.index(
            "DELETE FROM credeals.cre_listing_contacts  "
            "WHERE listing_id IN (SELECT id FROM _child_refresh)"
        )
    ]

    assert "NOT jsonb_path_exists" in child_refresh
    assert "preserveChildCollections" in child_refresh
    assert "preserveChildCollections" in child_additive
    assert "detailError" in child_additive
