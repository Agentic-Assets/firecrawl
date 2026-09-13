"""No-DB contracts for the atomic precommit child-regression guard."""

import re
from datetime import datetime, timezone

import cre_checkpoint_refresh as refresh
import cre_ingest as ingest
import pytest

SCRAPED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc).isoformat()


def _colliers_row_transition_gate(enabled):
    return f'''(
      {str(enabled).lower()}
      AND jsonb_path_exists(
        EXCLUDED.raw_data,
        '$.**.sourceKey ? (@ == "colliers-main")'
      )
      AND jsonb_path_exists(
        EXCLUDED.raw_data,
        '$.**.freshnessProvenance.detailScope ? (@ == "first_party_detail_api")'
      )
    )'''


def _compact_sql(value):
    return " ".join(value.split())


@pytest.mark.parametrize(
    "before,after,regressed",
    [
        pytest.param(100, 69, True, id="destructive-drop-over-thirty-percent"),
        pytest.param(
            1423,
            20,
            True,
            id="svn-link-preservation-regression-1423-to-20",
        ),
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

    baseline = sql.index("CREATE TEMP TABLE _child_counts_by_listing_before")
    child_delete = sql.index(
        "DELETE FROM credeals.cre_listing_contacts  "
        "WHERE listing_id IN (SELECT id FROM _contact_refresh)"
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


def test_build_sql_excludes_legitimately_retired_parents_from_child_guard():
    sql = ingest.build_sql([], [], SCRAPED_AT, {"cushman-wakefield"})

    snapshot = sql.index("CREATE TEMP TABLE _child_counts_by_listing_before")
    retirement = sql.index("CREATE TEMP TABLE _retired")
    retained = sql.index("CREATE TEMP TABLE _retained_child_scope_after")
    aggregate = sql.index("INSERT INTO _child_counts_before")
    guard = sql.index("checkpoint child quality regression before commit")

    assert snapshot < retirement < retained < aggregate < guard
    assert "JOIN credeals.cre_listings l ON l.id = before.listing_id" in sql
    assert "WHERE l.deleted_at IS NULL" in sql[retained:aggregate]
    assert (
        "JOIN _retained_child_scope_after retained\n"
        "  USING (listing_id, source_key)"
    ) in sql
    assert sql.count("FROM _retained_child_scope_after a") == 6


def test_build_sql_counts_all_checkpoint_child_types_and_guards_optional_tables():
    sql = ingest.build_sql([], [], SCRAPED_AT, set())

    for child_type in ("contacts", "documents", "images", "media", "links"):
        assert f"'{child_type}'" in sql
    assert "to_regclass('credeals.cre_listing_media')" in sql
    assert "to_regclass('credeals.cre_listing_links')" in sql


def test_colliers_first_party_image_transition_uses_narrow_semantic_guard():
    sql = ingest.build_sql(
        [],
        [],
        SCRAPED_AT,
        set(),
        colliers_first_party_transition=True,
    )

    assert "CREATE TEMP TABLE _colliers_semantic_images_before" in sql
    assert "colliers_coveo_image_transition boolean := true" in sql
    assert "prior_counts.source_key = 'colliers-main'" in sql
    assert "prior_counts.child_type = 'images'" in sql
    assert "checkpoint Colliers semantic image regression before commit" in sql
    assert "left a current property without an image" in sql
    assert "&quot;+$" in sql
    assert "-(?:w|672-404)$" in sql


def test_reappearance_event_joins_have_unambiguous_identity_columns():
    sql = ingest.build_sql([], [], SCRAPED_AT, set())

    event_sql = sql[
        sql.index("INSERT INTO credeals.cre_listing_events") :
        sql.index("-- Children: refresh wholesale")
    ]
    assert "JOIN _src s USING (brokerage_id, external_id)" not in event_sql
    assert "JOIN _prior_source_presence p USING (brokerage_id, external_id)" not in event_sql
    assert "JOIN credeals.cre_source_index si USING (brokerage_id, external_id)" not in event_sql
    assert "s.brokerage_id = u.brokerage_id" in event_sql
    assert "p.brokerage_id = u.brokerage_id" in event_sql
    assert "si.brokerage_id = u.brokerage_id" in event_sql


def test_colliers_missing_expert_preserves_contacts_only():
    sql = ingest.build_sql(
        [],
        [],
        SCRAPED_AT,
        set(),
        colliers_first_party_transition=True,
    )

    assert "CREATE TEMP TABLE _contact_preserve" in sql
    contact_preserve = sql[
        sql.index("CREATE TEMP TABLE _contact_preserve") :
        sql.index("CREATE TEMP TABLE _contact_refresh")
    ]
    assert "WHERE true" in contact_preserve
    assert "preserveContactCollections" in contact_preserve
    assert "detailObservedWithContactPreservation" in contact_preserve
    assert "freshnessProvenance.detailScope" in contact_preserve
    assert "first_party_detail_api" in contact_preserve
    assert "colliersMain.unresolvedExpertIds[*]" in contact_preserve
    assert "= 'colliers-main'" in contact_preserve
    assert "s.source_key" not in contact_preserve
    assert re.search(r"(?<![A-Za-z0-9_])s\.source_key\b", sql) is None
    assert (
        "DELETE FROM credeals.cre_listing_contacts  "
        "WHERE listing_id IN (SELECT id FROM _contact_refresh);"
    ) in sql
    assert (
        "DELETE FROM credeals.cre_listing_documents "
        "WHERE listing_id IN (SELECT id FROM _child_refresh);"
    ) in sql
    assert (
        "DELETE FROM credeals.cre_listing_images    "
        "WHERE listing_id IN (SELECT id FROM _child_refresh);"
    ) in sql
    assert "WHERE u.id IN (SELECT id FROM _contact_additive)" in sql
    assert "WHERE u.id IN (SELECT id FROM _child_additive)" in sql


def test_untrusted_colliers_transition_cannot_preserve_contacts_or_clear_scalars():
    sql = ingest.build_sql([], [], SCRAPED_AT, set())

    contact_preserve = sql[
        sql.index("CREATE TEMP TABLE _contact_preserve") :
        sql.index("CREATE TEMP TABLE _contact_refresh")
    ]
    upsert = sql[
        sql.index(
            "ON CONFLICT (brokerage_id, external_id) "
            "WHERE external_id IS NOT NULL"
        ) :
        sql.index("-- Canonical full ingest synchronizes source observation")
    ]

    assert "WHERE false" in contact_preserve
    assert "colliers_coveo_image_transition boolean := false" in sql
    assert "{colliers_transition_row_sql}" not in sql
    gate = _colliers_row_transition_gate(False)
    assert upsert.count(gate) == 11
    compact_upsert = _compact_sql(upsert)
    compact_gate = _compact_sql(gate)
    for column in (
        "sale_price_usd",
        "sale_price_per_sf",
        "lease_rate_min",
        "lease_rate_max",
        "lease_rate_type",
    ):
        assert (
            f"{column} = CASE WHEN {compact_gate} THEN NULL"
            in compact_upsert
        )


def test_trusted_colliers_transition_is_row_scoped_before_clearing_scalars():
    sql = ingest.build_sql(
        [],
        [],
        SCRAPED_AT,
        set(),
        colliers_first_party_transition=True,
    )
    upsert = sql[
        sql.index(
            "ON CONFLICT (brokerage_id, external_id) "
            "WHERE external_id IS NOT NULL"
        ) :
        sql.index("-- Canonical full ingest synchronizes source observation")
    ]

    assert "{colliers_transition_row_sql}" not in sql
    gate = _colliers_row_transition_gate(True)
    assert upsert.count(gate) == 11
    compact_upsert = _compact_sql(upsert)
    compact_gate = _compact_sql(gate)
    assert '$.**.sourceKey ? (@ == "colliers-main")' in upsert
    assert (
        '$.**.freshnessProvenance.detailScope ? '
        '(@ == "first_party_detail_api")'
    ) in upsert
    for column, replacement in (
        ("size_sf", "EXCLUDED.size_sf"),
        ("lot_size_sf", "EXCLUDED.lot_size_sf"),
        ("available_sf", "EXCLUDED.available_sf"),
        ("min_divisible_sf", "EXCLUDED.min_divisible_sf"),
        ("max_divisible_sf", "EXCLUDED.max_divisible_sf"),
        ("units", "EXCLUDED.units"),
        ("sale_price_usd", "NULL"),
        ("sale_price_per_sf", "NULL"),
        ("lease_rate_min", "NULL"),
        ("lease_rate_max", "NULL"),
        ("lease_rate_type", "NULL"),
    ):
        assert (
            f"{column} = CASE WHEN {compact_gate} THEN {replacement}"
            in compact_upsert
        )


def test_contact_preservation_never_weakens_noncontact_wholesale_refresh():
    sql = ingest.build_sql(
        [],
        [],
        SCRAPED_AT,
        set(),
        colliers_first_party_transition=True,
    )

    assert (
        "DELETE FROM credeals.cre_listing_documents "
        "WHERE listing_id IN (SELECT id FROM _child_refresh);"
    ) in sql
    assert (
        "DELETE FROM credeals.cre_listing_images    "
        "WHERE listing_id IN (SELECT id FROM _child_refresh);"
    ) in sql
    assert (
        "DELETE FROM credeals.cre_listing_media "
        "WHERE listing_id IN (SELECT id FROM _child_refresh);"
    ) in sql
    assert (
        "DELETE FROM credeals.cre_listing_links "
        "WHERE listing_id IN (SELECT id FROM _child_refresh);"
    ) in sql
    assert "SELECT id FROM _contact_preserve" not in sql[
        sql.index("DELETE FROM credeals.cre_listing_documents") :
        sql.index("-- OM-parsed facts")
    ]


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
            "WHERE listing_id IN (SELECT id FROM _contact_refresh)"
        )
    ]

    assert "NOT jsonb_path_exists" in child_refresh
    assert "preserveChildCollections" in child_refresh
    assert "preserveChildCollections" in child_additive
    assert "detailError" in child_additive
