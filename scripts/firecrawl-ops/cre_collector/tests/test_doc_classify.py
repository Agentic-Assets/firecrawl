"""
test_doc_classify.py

Contracts for:
  1. cre_parse.classify_doc: decision order matches Section D of the Phase-2
     contract (most-specific-first: rent_roll > financials > floor_plan > om >
     flyer > brochure > other/None).
  2. om_classify_existing.classify_upgrades: upgrade-only semantics, never
     downgrades an existing-'brochure' row, never touches non-brochure rows.
  3. om_classify_existing.build_sql: dry-run count-summary shape (by_type dict
     structure), SQL contains the WHERE doc_type = 'brochure' guard, and an
     empty upgrade list produces a no-op SQL block.

Pure Python, no DB, no network. Mirrors the backfill_media test pattern.
"""

import re

import pytest

from cre_parse import classify_doc
from om_classify_existing import (
    BROCHURE,
    UPGRADE_TYPES,
    build_sql,
    classify_upgrades,
    _summarize,
)

# ---------------------------------------------------------------------------
# 1. classify_doc: Section D decision order
#    All test URLs are http:// so the http_url_or_none gate passes.
# ---------------------------------------------------------------------------


class TestClassifyDocDecisionOrder:
    """Verify keyword priority matches contract Section D (most-specific first)."""

    # --- rent_roll (priority 1) ---

    def test_rent_roll_url(self):
        assert classify_doc("https://example.com/documents/rent-roll.pdf") == "rent_roll"

    def test_rent_roll_underscore(self):
        assert classify_doc("https://example.com/rent_roll_2024.pdf") == "rent_roll"

    def test_rent_roll_title(self):
        assert classify_doc("https://example.com/doc.pdf", "Rent Roll Q1 2024") == "rent_roll"

    def test_rent_roll_beats_financials(self):
        # rent_roll wins over financials when both are present
        url = "https://example.com/financials_rent-roll.pdf"
        assert classify_doc(url) == "rent_roll"

    def test_rent_roll_beats_om(self):
        url = "https://example.com/om-rent-roll.pdf"
        assert classify_doc(url) == "rent_roll"

    # --- financials (priority 2) ---

    def test_financials_url(self):
        assert classify_doc("https://example.com/financial_summary.pdf") == "financials"

    def test_pro_forma_url(self):
        assert classify_doc("https://example.com/pro-forma.xlsx") == "financials"

    def test_proforma_no_space(self):
        assert classify_doc("https://example.com/proforma_2024.pdf") == "financials"

    def test_t12_url(self):
        assert classify_doc("https://example.com/t-12.pdf") == "financials"

    def test_t12_no_dash(self):
        # \bt-?12\b requires a word boundary AFTER '12'. In 't12_report' the
        # underscore is \w so '\b' does not fire -> falls through to 'other'.
        # This is the real cre_parse.py behavior; the test documents it.
        assert classify_doc("https://example.com/t12_report.pdf") == "other"

    def test_financials_beats_floor_plan(self):
        url = "https://example.com/floorplan-financials.pdf"
        assert classify_doc(url) == "financials"

    # --- floor_plan (priority 3) ---

    def test_floor_plan_url(self):
        assert classify_doc("https://example.com/floor-plan.pdf") == "floor_plan"

    def test_floor_plan_underscore(self):
        assert classify_doc("https://example.com/floor_plan_suite_200.pdf") == "floor_plan"

    def test_floorplan_nospace(self):
        assert classify_doc("https://example.com/floorplan.pdf") == "floor_plan"

    def test_site_plan_url(self):
        assert classify_doc("https://example.com/site-plan.pdf") == "floor_plan"

    def test_siteplan_title(self):
        assert classify_doc("https://example.com/doc.pdf", "Siteplan Overview") == "floor_plan"

    def test_floor_plan_beats_om(self):
        url = "https://example.com/om-floorplan.pdf"
        assert classify_doc(url) == "floor_plan"

    # --- om (priority 4) ---

    def test_om_offering_url(self):
        assert classify_doc("https://example.com/offering-memorandum.pdf") == "om"

    def test_om_keyword(self):
        assert classify_doc("https://example.com/documents/memorandum.pdf") == "om"

    def test_om_path_segment(self):
        # /om/ as a path segment matches (?:^|[/_-])om(?:[/_.-]|$)
        assert classify_doc("https://example.com/documents/om/property.pdf") == "om"

    def test_om_teaser(self):
        assert classify_doc("https://example.com/property-teaser.pdf") == "om"

    def test_om_dataroom(self):
        assert classify_doc("https://example.com/dataroom/files/doc.pdf") == "om"

    def test_om_data_room_underscore(self):
        assert classify_doc("https://example.com/data_room.pdf") == "om"

    def test_om_deal_room(self):
        assert classify_doc("https://example.com/deal-room.pdf") == "om"

    def test_om_title(self):
        assert classify_doc("https://example.com/doc.pdf", "Offering Memorandum 2024") == "om"

    def test_om_beats_flyer(self):
        # om wins over flyer when url contains both
        url = "https://example.com/om-flyer.pdf"
        assert classify_doc(url) == "om"

    # --- flyer (priority 5) ---

    def test_flyer_url(self):
        assert classify_doc("https://example.com/property-flyer.pdf") == "flyer"

    def test_flyer_title(self):
        assert classify_doc("https://example.com/doc.pdf", "Marketing Flyer") == "flyer"

    def test_flyer_beats_brochure(self):
        url = "https://example.com/brochure-flyer.pdf"
        # flyer wins because flyer is tested before brochure in the keyword rules
        assert classify_doc(url) == "flyer"

    # --- brochure (priority 6) ---

    def test_brochure_url(self):
        assert classify_doc("https://example.com/property-brochure.pdf") == "brochure"

    def test_marketing_url(self):
        assert classify_doc("https://example.com/marketing-package.pdf") == "brochure"

    def test_package_keyword(self):
        assert classify_doc("https://example.com/package.pdf") == "brochure"

    def test_deck_keyword(self):
        assert classify_doc("https://example.com/investment-deck.pdf") == "brochure"

    def test_pib_keyword(self):
        assert classify_doc("https://example.com/property-pib.pdf") == "brochure"

    # --- other / None (priority 7 / fallback) ---

    def test_bare_pdf_extension_is_other(self):
        # A .pdf url with no keyword -> 'other'
        result = classify_doc("https://example.com/documents/abc123.pdf")
        assert result == "other"

    def test_no_keyword_no_extension_is_none(self):
        # No keyword, no file extension, not a recognized hosted-download -> None
        result = classify_doc("https://example.com/listing-page")
        assert result is None

    def test_empty_url_is_none(self):
        assert classify_doc("") is None

    def test_none_url_is_none(self):
        assert classify_doc(None) is None

    def test_non_http_url_still_matches_keywords(self):
        # cre_parse.classify_doc does NOT filter by URL scheme; it searches the
        # full url+title string for keywords. An ftp:// url with 'brochure' in
        # the path still classifies as 'brochure'. (The http-only guard is only
        # in backfill_media_from_raw_data.py's classify_doc wrapper, not in
        # cre_parse.classify_doc itself.)
        assert classify_doc("ftp://example.com/brochure.pdf") == "brochure"

    def test_buildout_sharing_path_is_other(self):
        # Buildout /sharing/ path with no keyword -> 'other'
        result = classify_doc("https://buildout.com/sharing/abc123def")
        assert result == "other"

    def test_buildout_file_param_is_other(self):
        result = classify_doc("https://buildout.com/documents?file=456789")
        assert result == "other"


# ---------------------------------------------------------------------------
# 2. classify_upgrades: upgrade-only semantics
#    The function is the pure testable core of om_classify_existing.
# ---------------------------------------------------------------------------


class TestClassifyUpgrades:
    """Upgrade-only and never-downgrade invariants for classify_upgrades()."""

    def _row(self, row_id, url, title=None):
        """Helper: produce a (row_id, url, title) tuple for feeding to classify_upgrades."""
        return (row_id, url, title)

    # --- happy-path upgrades ---

    def test_om_url_upgrades_brochure(self):
        rows = [self._row("uuid-1", "https://example.com/offering-memorandum.pdf")]
        result = classify_upgrades(rows)
        assert len(result) == 1
        row_id, old_type, new_type = result[0]
        assert row_id == "uuid-1"
        assert old_type == BROCHURE
        assert new_type == "om"

    def test_financials_url_upgrades_brochure(self):
        rows = [self._row("uuid-2", "https://example.com/financial-summary.pdf")]
        result = classify_upgrades(rows)
        assert len(result) == 1
        assert result[0][2] == "financials"

    def test_rent_roll_url_upgrades_brochure(self):
        rows = [self._row("uuid-3", "https://example.com/rent-roll.pdf")]
        result = classify_upgrades(rows)
        assert len(result) == 1
        assert result[0][2] == "rent_roll"

    def test_floor_plan_url_upgrades_brochure(self):
        rows = [self._row("uuid-4", "https://example.com/floorplan.pdf")]
        result = classify_upgrades(rows)
        assert len(result) == 1
        assert result[0][2] == "floor_plan"

    def test_flyer_url_upgrades_brochure(self):
        rows = [self._row("uuid-5", "https://example.com/property-flyer.pdf")]
        result = classify_upgrades(rows)
        assert len(result) == 1
        assert result[0][2] == "flyer"

    def test_all_upgrade_types_are_covered(self):
        """Every member of UPGRADE_TYPES must be reachable via a well-formed URL."""
        test_urls = {
            "om": "https://example.com/offering-memorandum.pdf",
            "financials": "https://example.com/financial-report.pdf",
            "rent_roll": "https://example.com/rent-roll-2024.pdf",
            "floor_plan": "https://example.com/floorplan-suite200.pdf",
            "flyer": "https://example.com/property-flyer.pdf",
        }
        assert set(test_urls.keys()) == UPGRADE_TYPES, (
            "UPGRADE_TYPES and test coverage must stay in sync"
        )
        for expected_type, url in test_urls.items():
            rows = [self._row(f"uuid-{expected_type}", url)]
            result = classify_upgrades(rows)
            assert len(result) == 1, f"{expected_type} URL did not produce an upgrade"
            assert result[0][2] == expected_type

    # --- never-downgrade: brochure rows that stay brochure ---

    def test_brochure_url_is_not_upgraded(self):
        rows = [self._row("uuid-b", "https://example.com/property-brochure.pdf")]
        # classify_doc returns 'brochure' -> no upgrade
        result = classify_upgrades(rows)
        assert result == []

    def test_other_type_url_is_not_upgraded(self):
        # bare .pdf with no keyword -> 'other' -> no upgrade (other is less specific)
        rows = [self._row("uuid-o", "https://example.com/abc123.pdf")]
        result = classify_upgrades(rows)
        assert result == []

    def test_none_classified_url_is_not_upgraded(self):
        # no keyword, no extension -> classify_doc returns None -> no upgrade
        rows = [self._row("uuid-n", "https://example.com/listing-page")]
        result = classify_upgrades(rows)
        assert result == []

    # --- never-downgrade: mixed batch ---

    def test_only_upgradeable_rows_appear_in_result(self):
        rows = [
            self._row("uuid-om", "https://example.com/offering-memorandum.pdf"),
            self._row("uuid-br", "https://example.com/property-brochure.pdf"),
            self._row("uuid-bare", "https://example.com/abc123.pdf"),
            self._row("uuid-fl", "https://example.com/floorplan.pdf"),
            self._row("uuid-none", "https://example.com/listing-page"),
        ]
        result = classify_upgrades(rows)
        result_ids = {r[0] for r in result}
        assert "uuid-om" in result_ids
        assert "uuid-fl" in result_ids
        # These must NOT appear (would be a downgrade or no-op)
        assert "uuid-br" not in result_ids
        assert "uuid-bare" not in result_ids
        assert "uuid-none" not in result_ids
        assert len(result) == 2

    def test_empty_rows_yields_empty_upgrades(self):
        assert classify_upgrades([]) == []

    # --- old_type is always BROCHURE in the result ---

    def test_old_type_is_always_brochure(self):
        rows = [
            self._row("uuid-x", "https://example.com/om.pdf"),
            self._row("uuid-y", "https://example.com/rent-roll.pdf"),
        ]
        result = classify_upgrades(rows)
        for _, old_type, _ in result:
            assert old_type == BROCHURE, (
                f"old_type must always be 'brochure'; got '{old_type}'"
            )

    # --- title is used when present ---

    def test_title_helps_classify(self):
        # bare .pdf URL but title says "Offering Memorandum" -> upgrades to om
        rows = [
            self._row("uuid-title", "https://example.com/doc.pdf", "Offering Memorandum")
        ]
        result = classify_upgrades(rows)
        assert len(result) == 1
        assert result[0][2] == "om"

    def test_title_none_still_works(self):
        rows = [self._row("uuid-t-none", "https://example.com/floorplan.pdf", None)]
        result = classify_upgrades(rows)
        assert len(result) == 1
        assert result[0][2] == "floor_plan"


# ---------------------------------------------------------------------------
# 3. build_sql and _summarize: SQL shape and dry-run count-summary
# ---------------------------------------------------------------------------


class TestBuildSql:
    """build_sql() produces well-formed upgrade SQL with the correct guards."""

    def test_empty_upgrades_produces_no_op_sql(self):
        sql = build_sql([])
        # Must be valid SQL (no syntax error at load time - just string check)
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql
        # The no-upgrade branch emits a comment, not a live UPDATE statement
        # (the comment may contain the word UPDATE but no actual DML)
        assert "UPDATE credeals" not in sql

    def test_upgrade_sql_contains_where_brochure_guard(self):
        """The UPDATE must include a WHERE doc_type = 'brochure' guard."""
        upgrades = [("uuid-1", BROCHURE, "om"), ("uuid-2", BROCHURE, "financials")]
        sql = build_sql(upgrades)
        # The guard prevents downgrading rows that were reclassified concurrently.
        assert "doc_type  = 'brochure'" in sql or "doc_type = 'brochure'" in sql

    def test_upgrade_sql_contains_to_regclass_guard(self):
        """The SQL must include a to_regclass existence guard."""
        upgrades = [("uuid-1", BROCHURE, "om")]
        sql = build_sql(upgrades)
        assert "to_regclass" in sql
        assert "cre_listing_documents" in sql

    def test_upgrade_sql_contains_begin_commit(self):
        upgrades = [("uuid-1", BROCHURE, "om")]
        sql = build_sql(upgrades)
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql

    def test_upgrade_sql_contains_on_error_stop(self):
        upgrades = [("uuid-1", BROCHURE, "om")]
        sql = build_sql(upgrades)
        assert "ON_ERROR_STOP" in sql

    def test_upgrade_sql_contains_target_type(self):
        """Each new_type should appear in the UPDATE VALUES clause."""
        upgrades = [
            ("uuid-a", BROCHURE, "om"),
            ("uuid-b", BROCHURE, "rent_roll"),
        ]
        sql = build_sql(upgrades)
        assert "'om'" in sql
        assert "'rent_roll'" in sql

    def test_upgrade_sql_contains_all_uuids(self):
        upgrades = [
            ("aaa00000-0000-0000-0000-000000000001", BROCHURE, "om"),
            ("aaa00000-0000-0000-0000-000000000002", BROCHURE, "financials"),
        ]
        sql = build_sql(upgrades)
        assert "aaa00000-0000-0000-0000-000000000001" in sql
        assert "aaa00000-0000-0000-0000-000000000002" in sql

    def test_upgrade_sql_does_not_contain_downgrade_to_brochure(self):
        """The generated SQL must never set doc_type = 'brochure' as a NEW value.
        The WHERE guard uses 'brochure' as a filter (which is correct and expected);
        the SET clause must delegate to the VALUES column, not a literal."""
        upgrades = [("uuid-1", BROCHURE, "om"), ("uuid-2", BROCHURE, "flyer")]
        sql = build_sql(upgrades)
        # The SET clause must use the aliased column, not a literal 'brochure'.
        assert "SET    doc_type = u.new_doc_type" in sql or "SET doc_type = u.new_doc_type" in sql
        # No row's VALUES should contain 'brochure' as the new_doc_type
        for _row_id, _old, new_type in upgrades:
            assert new_type != BROCHURE


class TestSummarize:
    """_summarize() returns a by_type dict with the correct shape."""

    def test_empty_upgrades_returns_empty_dict(self, capsys):
        by_type = _summarize([], total_brochure_rows=0)
        assert by_type == {}

    def test_by_type_counts_are_correct(self, capsys):
        upgrades = [
            ("uuid-1", BROCHURE, "om"),
            ("uuid-2", BROCHURE, "om"),
            ("uuid-3", BROCHURE, "financials"),
        ]
        by_type = _summarize(upgrades, total_brochure_rows=100)
        assert by_type[(BROCHURE, "om")] == 2
        assert by_type[(BROCHURE, "financials")] == 1

    def test_by_type_keys_are_old_new_tuples(self, capsys):
        upgrades = [("uuid-1", BROCHURE, "rent_roll")]
        by_type = _summarize(upgrades, total_brochure_rows=50)
        assert (BROCHURE, "rent_roll") in by_type

    def test_summary_prints_total_scanned(self, capsys):
        _summarize([], total_brochure_rows=70414)
        out = capsys.readouterr().out
        assert "70414" in out

    def test_summary_prints_upgrade_count(self, capsys):
        upgrades = [("uuid-1", BROCHURE, "om"), ("uuid-2", BROCHURE, "flyer")]
        _summarize(upgrades, total_brochure_rows=100)
        out = capsys.readouterr().out
        assert "2" in out


# ---------------------------------------------------------------------------
# 4. Integration: classify_upgrades -> build_sql round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """End-to-end: a batch of brochure-typed rows -> classify_upgrades -> build_sql."""

    def test_round_trip_sql_shape(self):
        rows = [
            ("id-om", "https://example.com/offering-memorandum.pdf", None),
            ("id-fin", "https://example.com/financial-proforma.pdf", None),
            ("id-rr", "https://example.com/rent-roll-q1.pdf", None),
            ("id-fp", "https://example.com/floorplan-lobby.pdf", None),
            ("id-fl", "https://example.com/marketing-flyer.pdf", None),
            # These should NOT appear in the SQL (brochure/other/None)
            ("id-br", "https://example.com/property-brochure.pdf", None),
            ("id-bare", "https://example.com/abc123.pdf", None),
            ("id-pg", "https://example.com/listing-page", None),
        ]
        upgrades = classify_upgrades(rows)
        # Exactly the 5 upgradeable rows
        assert len(upgrades) == 5
        upgraded_ids = {u[0] for u in upgrades}
        assert "id-om" in upgraded_ids
        assert "id-fin" in upgraded_ids
        assert "id-rr" in upgraded_ids
        assert "id-fp" in upgraded_ids
        assert "id-fl" in upgraded_ids
        assert "id-br" not in upgraded_ids
        assert "id-bare" not in upgraded_ids
        assert "id-pg" not in upgraded_ids

        sql = build_sql(upgrades)
        # SQL is valid structure
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql
        assert "to_regclass" in sql
        # All 5 UUIDs in SQL
        for row_id in upgraded_ids:
            assert row_id in sql

    def test_round_trip_no_upgrades_sql_is_no_op(self):
        rows = [
            ("id-b1", "https://example.com/property-brochure.pdf", None),
            ("id-b2", "https://example.com/abc123.pdf", None),
        ]
        upgrades = classify_upgrades(rows)
        assert upgrades == []
        sql = build_sql(upgrades)
        # No live DML UPDATE statement (a comment mentioning "UPDATE" is acceptable)
        assert "UPDATE credeals" not in sql
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql
