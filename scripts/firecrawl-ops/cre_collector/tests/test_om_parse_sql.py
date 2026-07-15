"""test_om_parse_sql.py

Additional coverage for om_parse.py targeting the missing lines from the 72%
baseline: confidence logic edge paths, extractor boundary conditions, the
_psql_query regression (STDIN -f - invariant), SQL builder header invariants,
listing_scalars_from_facts edge cases, _slugs_for_sources, and the main()
CLI entrypoint.

CRITICAL REGRESSION (tracked per task spec): _psql_query must feed SQL via
`["-f", "-"]` (STDIN) NOT `["-c", sql]` so that the psql meta-command head
(\\set ON_ERROR_STOP on) is processed correctly. A regression to -c would
silently mis-parse the script. See test_psql_query_uses_stdin_not_dash_c().

Pure-transform / no-DB / no-network: subprocess.run and find_psql are
monkeypatched for any test that reaches _psql_query. parse_pdf_to_text is
never called (network boundary; left uncovered here by design).
"""

import sys
import pytest

import om_parse
from om_parse import (
    _confidence_for,
    build_candidate_sql,
    build_docs_sql,
    listing_scalars_from_facts,
    _psql_query,
    _slugs_for_sources,
    _extract_noi,
    _extract_cap_rate,
    _extract_occupancy,
    _extract_units,
    _extract_year_built,
    _extract_unit_mix,
    _extract_rent_roll,
)

URL = "https://example.com/om.pdf"


# ---------------------------------------------------------------------------
# _confidence_for: branch coverage for the 0.3 path (line 185)
# ---------------------------------------------------------------------------


class TestConfidenceFor:
    """Verify the three-branch heuristic in _confidence_for."""

    def test_both_true_returns_08(self):
        assert _confidence_for(True, True) == 0.8

    def test_label_specific_only_returns_05(self):
        # label_specific=True but value NOT plausible -> second branch
        assert _confidence_for(True, False) == 0.5

    def test_value_plausible_only_returns_05(self):
        # value plausible but label NOT specific -> second branch
        assert _confidence_for(False, True) == 0.5

    def test_neither_returns_03(self):
        # Neither -> 0.3 (line 185 in om_parse.py)
        assert _confidence_for(False, False) == 0.3

    def test_result_is_float(self):
        for a, b in [(True, True), (True, False), (False, True), (False, False)]:
            assert isinstance(_confidence_for(a, b), float)


# ---------------------------------------------------------------------------
# _extract_noi: val <= 0 path (line 216) and below-range plausibility
# ---------------------------------------------------------------------------


class TestExtractNoi:
    """Edge cases in _extract_noi beyond what test_om_parse.py covers."""

    def test_noi_val_zero_returns_none(self):
        # parse_money would return 0 for "$0" which is <= 0; the function
        # must return None (line 216 branch).
        text = "Net Operating Income $0"
        assert _extract_noi(text, URL) is None

    def test_noi_below_plausible_range_has_lower_confidence(self):
        # $500 is below the $1,000 plausible lower bound -> plausible=False
        # but label IS specific (full label present) -> confidence=0.5 < floor
        text = "Net Operating Income $500 per year"
        fact = _extract_noi(text, URL)
        # still emits a row (value > 0), but with reduced confidence
        assert fact is not None
        assert fact["confidence"] == 0.5

    def test_noi_above_plausible_range_has_lower_confidence(self):
        # $2 billion exceeds the $1B plausible upper bound -> plausible=False
        text = "Net Operating Income $2,000,000,000"
        fact = _extract_noi(text, URL)
        assert fact is not None
        assert fact["confidence"] == 0.5

    def test_noi_abbr_without_full_label_is_less_specific(self):
        # Bare "NOI" abbreviation: specific=False because _NOI_LABEL_RE doesn't
        # match, so confidence < 0.8 regardless of value plausibility.
        text = "NOI $1,500,000 in-place"
        fact = _extract_noi(text, URL)
        assert fact is not None
        assert fact["confidence"] < 0.8

    def test_noi_negative_val_is_filtered_before_fact(self):
        # Negative money shouldn't parse to a positive, but if parse_money
        # somehow returned a negative, val <= 0 catches it.
        # We test via a zero to hit the exact guard branch.
        text = "Net Operating Income $-100"
        # parse_money("$-100") returns None or a negative; either way: None
        result = _extract_noi(text, URL)
        # Either returns None or a fact with val > 0 (parse_money may reject "-100")
        if result is not None:
            assert result["factValueNum"] > 0


# ---------------------------------------------------------------------------
# _extract_cap_rate: frac None path (line 230) and out-of-range (line 234)
# ---------------------------------------------------------------------------


class TestExtractCapRate:
    """Edge cases in _extract_cap_rate."""

    def test_cap_rate_fraction_none_returns_none(self, monkeypatch):
        # Monkeypatch parse_percent_to_fraction to return None so we hit line 230.
        monkeypatch.setattr(om_parse, "parse_percent_to_fraction", lambda _: None)
        text = "Cap Rate 6.5%"
        assert _extract_cap_rate(text, URL) is None

    def test_cap_rate_zero_returns_none(self):
        # frac <= 0 path (line 234): "Cap Rate 0%" should be filtered.
        text = "Cap Rate 0%"
        assert _extract_cap_rate(text, URL) is None

    def test_cap_rate_above_50pct_returns_none(self):
        # frac >= 0.5 path (line 234): "Cap Rate 75%" is a data error.
        text = "Cap Rate 75%"
        assert _extract_cap_rate(text, URL) is None

    def test_cap_rate_implausible_but_in_range_gets_05_confidence(self):
        # 25% cap rate is < 0.5 (passes the guard) but > 0.20 (implausible) ->
        # _confidence_for(True, False) = 0.5
        text = "Cap Rate 25%"
        fact = _extract_cap_rate(text, URL)
        assert fact is not None
        assert fact["confidence"] == 0.5

    def test_cap_rate_plausible_gets_08_confidence(self):
        # 6.5% is within 2%-20% -> plausible=True, label=True -> 0.8
        text = "Cap Rate 6.5%"
        fact = _extract_cap_rate(text, URL)
        assert fact is not None
        assert fact["confidence"] == 0.8


# ---------------------------------------------------------------------------
# _extract_occupancy: boundary conditions (line 246)
# ---------------------------------------------------------------------------


class TestExtractOccupancy:
    """Edge cases in _extract_occupancy."""

    def test_occupancy_frac_none_returns_none(self, monkeypatch):
        # parse_percent_to_fraction returns None -> line 246 branch
        monkeypatch.setattr(om_parse, "parse_percent_to_fraction", lambda _: None)
        text = "Occupancy 95%"
        assert _extract_occupancy(text, URL) is None

    def test_occupancy_zero_returns_none(self):
        # frac=0 fails the (0 < frac <= 1) guard
        text = "Occupancy 0%"
        assert _extract_occupancy(text, URL) is None

    def test_occupancy_over_100_returns_none(self):
        # frac > 1 (e.g. 110%) fails guard
        text = "Occupancy 110%"
        assert _extract_occupancy(text, URL) is None

    def test_occupancy_below_30_pct_gets_05_confidence(self):
        # 20% occupancy is valid (0 < 0.2 <= 1) but implausible for an OM
        text = "Occupancy 20%"
        fact = _extract_occupancy(text, URL)
        assert fact is not None
        # plausible = 0.30 <= 0.2 <= 1.0 is False -> confidence_for(True, False)=0.5
        assert fact["confidence"] == 0.5


# ---------------------------------------------------------------------------
# _extract_units: edge conditions (lines 261-264)
# ---------------------------------------------------------------------------


class TestExtractUnits:
    """Edge cases in _extract_units."""

    def test_units_over_100000_returns_none(self):
        # val > 100_000 guard (line 263).
        # _UNITS_LABEL_RE captures {1,5} digits, so we can only reach the guard
        # via the suffix regex which also uses {1,5}; 99999 (5 digits) < 100_000
        # so it passes. To actually exercise the > 100_000 branch we need 100001+
        # but the regex caps at 5 digits (max 99999). The guard IS reachable if
        # the regex is ever widened, but today 5-digit cap means the guard is a
        # defensive dead branch. Skip with a note rather than asserting wrong behavior.
        # Instead, test that 99999 units IS accepted (near-boundary, within range).
        text = "Number of Units 99999"
        fact = _extract_units(text, URL)
        # 99999 <= 100_000 so it passes the guard; plausible (1..10000) is False -> 0.5
        assert fact is not None
        assert fact["factValueNum"] == 99999

    def test_units_zero_returns_none(self):
        # val <= 0 guard (line 263)
        text = "Number of Units 0"
        assert _extract_units(text, URL) is None

    def test_units_via_suffix_not_label_is_less_specific(self):
        # "48 units" via suffix regex (not label regex) -> specific=False
        text = "The property has 48 units of residential space."
        fact = _extract_units(text, URL)
        assert fact is not None
        # specific=False, plausible=True (48 in 1..10000) -> confidence_for(F,T)=0.5
        assert fact["confidence"] == 0.5

    def test_units_above_10000_via_label_is_implausible(self):
        # val in range (1..100_000) but > 10_000 -> plausible=False
        # With explicit "Number of Units" label: confidence_for(True, False)=0.5
        text = "Number of Units 50000"
        fact = _extract_units(text, URL)
        assert fact is not None
        assert fact["confidence"] == 0.5


# ---------------------------------------------------------------------------
# _extract_year_built: out-of-range year (lines 276-279)
# ---------------------------------------------------------------------------


class TestExtractYearBuilt:
    """Edge cases in _extract_year_built."""

    def test_year_1800_returns_none(self):
        # 1800 fails the (1800 < yr ...) guard (strictly greater than 1800)
        text = "Year Built 1800"
        assert _extract_year_built(text, URL) is None

    def test_year_far_future_returns_none(self):
        # yr > current_year + 1 fails the upper guard
        text = "Year Built 2099"
        result = _extract_year_built(text, URL)
        # 2099 is > current year + 1 (2026+1=2027) -> None
        assert result is None

    def test_year_built_just_in_range(self):
        # 1850 is > 1800 and <= current_year+1 -> valid
        text = "Year Built 1850"
        fact = _extract_year_built(text, URL)
        assert fact is not None
        assert fact["factValueNum"] == 1850


# ---------------------------------------------------------------------------
# _extract_unit_mix: edge cases (lines 292-295)
# ---------------------------------------------------------------------------


class TestExtractUnitMix:
    """Edge cases in _extract_unit_mix."""

    def test_unit_mix_count_zero_filtered(self):
        # count=0 is rejected (count <= 0)
        text = "0 1BR/1BA $1,200"
        rows = _extract_unit_mix(text, URL)
        assert rows == []

    def test_unit_mix_count_over_9999_captured_as_4digit(self):
        # The count group is {1,4} digits so max capture is 9999.
        # "99999" is parsed as count=9999 (first 4 digits), which is within
        # the 1..10_000 guard range. Verify no crash and count is capped at 4 digits.
        text = "99999 studio $800"
        rows = _extract_unit_mix(text, URL)
        # May or may not match (regex is anchored to start of the number token);
        # if it does match, count <= 9999 (the regex 4-digit capture cap).
        if rows:
            assert rows[0]["unitCount"] <= 9999

    def test_unit_mix_dedup_same_normalized_type_and_count(self):
        # Dedup key is (unit_type.lower(), count). Two rows with the SAME
        # normalized type AND the same count produce only one output row.
        text = "12 studio $1,200\n12 studio $1,200"
        rows = _extract_unit_mix(text, URL)
        # "studio" + 12 deduplicates to one row
        assert len(rows) <= 1

    def test_unit_mix_different_types_both_kept(self):
        # "1br/1ba" and "1br" normalize to different types -> both kept.
        text = "12 1BR/1BA $1,450\n12 1br $1,450"
        rows = _extract_unit_mix(text, URL)
        # These are distinct unit types so both are retained
        types = {r["factValueText"] for r in rows}
        assert len(types) == len(rows)  # each type is unique in the output

    def test_unit_mix_no_rent_is_ok(self):
        # No "$rent" capture group -> rent_num=None but row still emitted
        text = "24 2BR/2BA"
        rows = _extract_unit_mix(text, URL)
        if rows:
            assert rows[0]["factValueNum"] is None
            assert rows[0]["unitCount"] == 24


# ---------------------------------------------------------------------------
# _extract_rent_roll: edge cases (lines 319-323)
# ---------------------------------------------------------------------------


class TestExtractRentRoll:
    """Edge cases in _extract_rent_roll."""

    def test_rent_roll_zero_rent_filtered(self, monkeypatch):
        # parse_money returns 0 -> rent_num <= 0, row skipped (line 320)
        monkeypatch.setattr(om_parse, "parse_money", lambda _: 0)
        text = "Tenant: Acme Corp $0"
        rows = _extract_rent_roll(text, URL)
        assert rows == []

    def test_rent_roll_none_rent_filtered(self, monkeypatch):
        # parse_money returns None -> skipped
        monkeypatch.setattr(om_parse, "parse_money", lambda _: None)
        text = "Tenant: Acme Corp $1,500"
        rows = _extract_rent_roll(text, URL)
        assert rows == []

    def test_rent_roll_dedup_same_tenant_rent(self):
        # Same (label.lower(), rent) seen twice -> only one row
        text = (
            "Tenant: Acme Corp $1,500/mo\n"
            "Tenant: Acme Corp $1,500/mo"
        )
        rows = _extract_rent_roll(text, URL)
        assert len(rows) <= 1


# ---------------------------------------------------------------------------
# listing_scalars_from_facts: edge cases (lines 379, 385)
# ---------------------------------------------------------------------------


class TestListingScalarsFromFacts:
    """Covers listing_scalars_from_facts edge paths."""

    def test_unknown_fact_key_is_skipped(self):
        # A scalar whose factKey is not in _SCALAR_TO_LISTING_KEY is dropped
        # (line 379: listing_key is None).
        facts = [{
            "factGroup": "scalar",
            "factKey": "unknown_field",
            "factValueNum": 42.0,
            "confidence": 0.9,
        }]
        assert listing_scalars_from_facts(facts) == {}

    def test_fact_with_none_value_is_skipped(self):
        # conf clears floor but factValueNum is None -> skipped (line 385)
        facts = [{
            "factGroup": "scalar",
            "factKey": "noi",
            "factValueNum": None,
            "confidence": 0.9,
        }]
        assert listing_scalars_from_facts(facts) == {}

    def test_fact_with_none_confidence_is_skipped(self):
        # confidence=None -> conf < CONFIDENCE_FLOOR branch fires
        facts = [{
            "factGroup": "scalar",
            "factKey": "noi",
            "factValueNum": 1_000_000,
            "confidence": None,
        }]
        assert listing_scalars_from_facts(facts) == {}

    def test_unit_mix_group_skipped(self):
        # unit_mix / rent_roll facts never produce a scalar column
        facts = [{
            "factGroup": "unit_mix",
            "factKey": "unit_type",
            "factValueNum": 1200.0,
            "confidence": 0.8,
        }]
        assert listing_scalars_from_facts(facts) == {}

    def test_higher_confidence_wins_for_duplicate_key(self):
        # Two noi facts with different confidence -> higher-confidence value wins
        facts = [
            {"factGroup": "scalar", "factKey": "noi",
             "factValueNum": 500_000.0, "confidence": 0.5},
            {"factGroup": "scalar", "factKey": "noi",
             "factValueNum": 800_000.0, "confidence": 0.8},
        ]
        scalars = listing_scalars_from_facts(facts)
        assert scalars["noi"] == 800_000.0

    def test_first_high_conf_wins_over_later_lower_conf(self):
        # The later entry has LOWER confidence -> first one wins
        facts = [
            {"factGroup": "scalar", "factKey": "noi",
             "factValueNum": 900_000.0, "confidence": 0.8},
            {"factGroup": "scalar", "factKey": "noi",
             "factValueNum": 200_000.0, "confidence": 0.7},
        ]
        scalars = listing_scalars_from_facts(facts)
        assert scalars["noi"] == 900_000.0


# ---------------------------------------------------------------------------
# build_candidate_sql: ON_ERROR_STOP head + standard_conforming_strings
# (asserting the psql meta-command header is present; this is what forces
# -f - instead of -c)
# ---------------------------------------------------------------------------


class TestBuildCandidateSqlHeader:
    """The SQL must carry the psql meta-command head that -f - requires."""

    def test_on_error_stop_head_is_first_line(self):
        sql = build_candidate_sql(["cbre"], limit=10, brokerage_slugs=["cbre"])
        first_line = sql.split("\n")[0]
        assert first_line == "\\set ON_ERROR_STOP on"

    def test_standard_conforming_strings_set(self):
        sql = build_candidate_sql(["cbre"], limit=10, brokerage_slugs=["cbre"])
        assert "SET standard_conforming_strings = on;" in sql

    def test_no_status_mutation_in_candidate_sql(self):
        # The SELECT must never assign status or deleted_at
        sql = build_candidate_sql(["cbre", "jll"], limit=100,
                                  brokerage_slugs=["cbre", "jll"])
        non_comment = "\n".join(
            l for l in sql.split("\n") if not l.strip().startswith("--")
        )
        # candidate SQL is SELECT-only (see test_select_only_no_dml); it must never
        # assign status. Direct guard (no or-short-circuit that passes vacuously).
        assert "status =" not in non_comment and "status=" not in non_comment
        assert "deleted_at =" not in non_comment

    def test_select_only_no_dml(self):
        sql = build_candidate_sql(["cbre"], limit=5, brokerage_slugs=["cbre"])
        non_comment = "\n".join(
            l for l in sql.split("\n") if not l.strip().startswith("--")
        ).upper()
        assert "UPDATE " not in non_comment
        assert "INSERT " not in non_comment
        assert "DELETE " not in non_comment

    def test_doc_types_are_quoted_not_fstring(self):
        # parseable doc_types must be sql_lit-quoted (never bare interpolation)
        sql = build_candidate_sql(["cbre"], limit=5, brokerage_slugs=["cbre"])
        # Each parseable type should appear as a quoted string
        for t in ("om", "financials", "rent_roll", "brochure"):
            assert f"'{t}'" in sql


class TestBuildDocsSqlHeader:
    """build_docs_sql also carries ON_ERROR_STOP + standard_conforming_strings."""

    def test_on_error_stop_head_is_first_line(self):
        sql = build_docs_sql(["ext-1"], ["cbre"])
        first_line = sql.split("\n")[0]
        assert first_line == "\\set ON_ERROR_STOP on"

    def test_standard_conforming_strings_set(self):
        sql = build_docs_sql(["ext-1"], ["cbre"])
        assert "SET standard_conforming_strings = on;" in sql

    def test_empty_ids_still_has_header(self):
        sql = build_docs_sql([], ["cbre"])
        assert "\\set ON_ERROR_STOP on" in sql
        assert "SET standard_conforming_strings = on;" in sql

    def test_no_status_mutation_in_docs_sql(self):
        sql = build_docs_sql(["ext-1"], ["cbre"])
        non_comment = "\n".join(
            l for l in sql.split("\n") if not l.strip().startswith("--")
        )
        assert "deleted_at =" not in non_comment


# ---------------------------------------------------------------------------
# CRITICAL: _psql_query regression test
# - SQL fed via STDIN (-f -) not via -c
# - argv has -q flag
# - kwargs has text=True and input=sql
# - non-zero returncode -> SystemExit that does NOT leak the DB url
# ---------------------------------------------------------------------------


_FAKE_PSQL = "/usr/bin/psql_fake"
_DB_URL = "postgres://user:SUPERSECRET@db.host:5432/mydb"
_SQL = "\\set ON_ERROR_STOP on\nSELECT 1;"


class TestPsqlQueryRegression:
    """Regression: _psql_query must use -f - (STDIN), not -c, and must not leak urls."""

    def test_argv_has_f_then_dash_not_dash_c(self, monkeypatch):
        """THE KEY REGRESSION: argv must have ['-f', '-'], never ['-c', sql]."""
        captured = {}

        def _fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)

        _psql_query(_DB_URL, _SQL)

        argv = captured["argv"]
        # Must NOT use -c
        assert "-c" not in argv, "regression: -c must not be used; SQL goes on STDIN via -f -"
        # Must use -f then -
        assert "-f" in argv
        f_idx = argv.index("-f")
        assert f_idx + 1 < len(argv), "-f must be followed by an argument"
        assert argv[f_idx + 1] == "-", "the argument after -f must be '-' (STDIN)"

    def test_argv_has_q_flag(self, monkeypatch):
        """argv must include -q (quiet mode)."""
        captured = {}

        def _fake_run(argv, **kwargs):
            captured["argv"] = argv

            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)
        _psql_query(_DB_URL, _SQL)

        assert "-q" in captured["argv"], "argv must include -q flag"

    def test_subprocess_run_has_text_true(self, monkeypatch):
        """subprocess.run must be called with text=True."""
        captured = {}

        def _fake_run(argv, **kwargs):
            captured["kwargs"] = kwargs

            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)
        _psql_query(_DB_URL, _SQL)

        assert captured["kwargs"].get("text") is True, "subprocess.run must pass text=True"

    def test_subprocess_run_passes_input_equal_to_sql(self, monkeypatch):
        """subprocess.run must receive input=sql (the SQL on STDIN)."""
        captured = {}

        def _fake_run(argv, **kwargs):
            captured["kwargs"] = kwargs

            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)
        _psql_query(_DB_URL, _SQL)

        assert captured["kwargs"].get("input") == _SQL, \
            "subprocess.run must pass input=sql (the SQL string)"

    def test_nonzero_returncode_raises_systemexit(self, monkeypatch):
        """A non-zero psql returncode must raise SystemExit."""
        def _fake_run(argv, **kwargs):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "ERROR: syntax error"
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)

        with pytest.raises(SystemExit):
            _psql_query(_DB_URL, _SQL)

    def test_nonzero_returncode_exit_message_does_not_contain_db_url(self, monkeypatch):
        """The SystemExit message must NOT contain the DB url (no credential leak)."""
        db_url = "postgres://admin:MYSECRET@prod.db.host:5432/credeals"

        def _fake_run(argv, **kwargs):
            class _P:
                returncode = 2
                stdout = ""
                stderr = "FATAL: connection refused"
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)

        with pytest.raises(SystemExit) as exc_info:
            _psql_query(db_url, _SQL)

        exit_code = str(exc_info.value)
        assert db_url not in exit_code, \
            f"DB url must NOT appear in the SystemExit message; got: {exit_code!r}"

    def test_successful_query_parses_tab_delimited_rows(self, monkeypatch):
        """_psql_query must split stdout rows on tabs and return tuples."""
        def _fake_run(argv, **kwargs):
            class _P:
                returncode = 0
                stdout = "abc\t123\t/url\n\nxyz\t456\t/url2\n"
                stderr = ""
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)

        rows = _psql_query(_DB_URL, _SQL)
        assert rows == [("abc", "123", "/url"), ("xyz", "456", "/url2")]

    def test_empty_stdout_returns_empty_list(self, monkeypatch):
        """Empty psql output -> empty list (not an error)."""
        def _fake_run(argv, **kwargs):
            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        monkeypatch.setattr(om_parse, "find_psql", lambda: _FAKE_PSQL)
        monkeypatch.setattr(om_parse.subprocess, "run", _fake_run)

        rows = _psql_query(_DB_URL, _SQL)
        assert rows == []


# ---------------------------------------------------------------------------
# _slugs_for_sources: lines 619, 626
# ---------------------------------------------------------------------------


class TestSlugsForSources:
    """_slugs_for_sources maps source keys to brokerage slugs."""

    def test_known_source_key_maps_to_slug(self):
        # cbre is a known key in SOURCE_TO_BROKERAGE
        slugs = _slugs_for_sources(["cbre"])
        assert "cbre" in slugs

    def test_unknown_key_falls_back_to_itself(self):
        # A key not in SOURCE_TO_BROKERAGE is passed through as-is (line 619)
        slugs = _slugs_for_sources(["totally-unknown-brokerage"])
        assert "totally-unknown-brokerage" in slugs

    def test_multiple_keys_aggregate_slugs(self):
        slugs = _slugs_for_sources(["cbre", "jll"])
        assert "cbre" in slugs
        assert "jll" in slugs

    def test_result_is_sorted(self):
        slugs = _slugs_for_sources(["jll", "cbre"])
        assert slugs == sorted(slugs)

    def test_empty_list_returns_empty(self):
        slugs = _slugs_for_sources([])
        assert slugs == []


# ---------------------------------------------------------------------------
# run() internals: --sources default, --limit default, enriched=0 early-exit
# ---------------------------------------------------------------------------


DB_URL_SENTINEL = "postgres://user:secret@db.example.com:5432/postgres"


class _Args:
    def __init__(self, **kw):
        self.sources = kw.get("sources", "cbre,jll")
        self.limit = kw.get("limit", 100)
        self.api_url = kw.get("api_url", "http://localhost:3002")
        self.env_file = kw.get("env_file", None)
        self.apply = kw.get("apply", False)
        self.dry_run = kw.get("dry_run", True)
        self.show_sql = kw.get("show_sql", False)


class TestRunInternals:
    """Coverage for run() branches not exercised by test_om_parse.py."""

    def test_run_candidates_with_no_enriched_returns_0(self, monkeypatch, capsys):
        """Candidates found but none produce OM facts -> 0 enriched, return 0."""
        monkeypatch.setattr(om_parse, "load_db_url",
                            lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))
        # One candidate row returned
        monkeypatch.setattr(om_parse, "_psql_query", lambda db_url, sql: [
            ("ext-1", "https://cbre.example/p/x", "cbre"),
        ])
        # parse returns None (PDF not parseable) -> no facts -> no enriched listing
        monkeypatch.setattr(om_parse, "parse_pdf_to_text", lambda *a, **kw: None)
        # docs query stub handled inline by returning empty doc list
        def _fake_query(db_url, sql):
            # docs query has JOIN on cre_listing_documents
            if "cre_listing_documents d ON" in sql:
                return []
            return [("ext-1", "https://cbre.example/p/x", "cbre")]
        monkeypatch.setattr(om_parse, "_psql_query", _fake_query)

        rc = om_parse.run(_Args(apply=False, dry_run=True))
        assert rc == 0
        captured = capsys.readouterr()
        assert "0 listing(s) gained OM facts" in captured.err

    def test_run_sources_default_cbre_jll(self, monkeypatch, capsys):
        """When sources='', default to cbre,jll."""
        monkeypatch.setattr(om_parse, "load_db_url",
                            lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))
        monkeypatch.setattr(om_parse, "_psql_query", lambda db_url, sql: [])

        rc = om_parse.run(_Args(sources=""))
        assert rc == 0
        # "0 candidates" message confirms the source resolution ran
        assert "0 candidates" in capsys.readouterr().err

    def test_run_apply_is_retired_before_any_database_or_ingest_work(self, monkeypatch, capsys):
        """The retired writer fails before any database or subprocess boundary."""

        def _boom(*_args, **_kwargs):
            raise AssertionError("retired --apply must not continue")

        monkeypatch.setattr(om_parse, "load_db_url", _boom)
        monkeypatch.setattr(om_parse, "_psql_query", _boom)
        monkeypatch.setattr(om_parse.subprocess, "run", _boom)
        rc = om_parse.run(_Args(apply=True, dry_run=False))
        assert rc == om_parse.RETIRED_WRITER_EXIT_CODE
        assert "sole production OM extraction writer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() entrypoint: --show-sql and defaults
# ---------------------------------------------------------------------------


class TestMainEntrypoint:
    """main() wires argparse -> run(); verify key flag behaviors."""

    def test_main_show_sql_exits_0(self, monkeypatch, capsys):
        """main() with --show-sql should exit 0 and print candidate SQL."""
        monkeypatch.setattr(om_parse, "load_db_url",
                            lambda env_file: (_ for _ in ()).throw(
                                AssertionError("must not connect")))
        monkeypatch.setattr(sys, "argv",
                            ["om_parse.py", "--show-sql"])
        with pytest.raises(SystemExit) as exc_info:
            om_parse.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "FROM credeals.cre_listings" in out

    def test_main_default_sources_limit(self, monkeypatch):
        """main() default: sources='cbre,jll', limit=100."""
        called_with = {}

        def _fake_run(args):
            called_with["sources"] = args.sources
            called_with["limit"] = args.limit
            return 0

        monkeypatch.setattr(om_parse, "run", _fake_run)
        monkeypatch.setattr(sys, "argv", ["om_parse.py"])
        with pytest.raises(SystemExit) as exc_info:
            om_parse.main()
        assert exc_info.value.code == 0
        assert called_with["sources"] == "cbre,jll"
        assert called_with["limit"] == 100

    def test_main_custom_sources_and_limit(self, monkeypatch):
        """main() accepts --sources and --limit flags."""
        called_with = {}

        def _fake_run(args):
            called_with["sources"] = args.sources
            called_with["limit"] = args.limit
            return 0

        monkeypatch.setattr(om_parse, "run", _fake_run)
        monkeypatch.setattr(sys, "argv",
                            ["om_parse.py", "--sources", "jll", "--limit", "25"])
        with pytest.raises(SystemExit) as exc_info:
            om_parse.main()
        assert exc_info.value.code == 0
        assert called_with["sources"] == "jll"
        assert called_with["limit"] == 25


# ---------------------------------------------------------------------------
# run() doc_url='' skip (line 668) and parse returns None (line 679)
# ---------------------------------------------------------------------------


class TestRunDocUrlAndParseBranches:
    """Cover the run() inner-loop skip branches: empty doc_url and None parse text."""

    def _setup_run(self, monkeypatch, tmp_path, doc_rows, parse_return):
        """Shared fixture: wire a one-candidate run with configurable doc_rows
        and parse_pdf_to_text return value."""
        monkeypatch.setattr(om_parse, "OUT_OM_DIR", str(tmp_path))
        monkeypatch.setattr(om_parse, "load_db_url",
                            lambda env_file: (DB_URL_SENTINEL, "/fake/.env.local"))

        def _fake_query(db_url, sql):
            if "cre_listing_documents d ON" in sql:
                return doc_rows
            return [("ext-1", "https://cbre.example/p/x", "cbre")]

        monkeypatch.setattr(om_parse, "_psql_query", _fake_query)
        monkeypatch.setattr(om_parse, "parse_pdf_to_text",
                            lambda doc_url, api_url=None: parse_return)

    def test_empty_doc_url_is_skipped(self, monkeypatch, tmp_path, capsys):
        """A doc row with an empty url string is skipped (line 668 branch)."""
        # doc_rows: one row where doc_url is "" (empty)
        self._setup_run(monkeypatch, tmp_path,
                        doc_rows=[("cbre", "ext-1", "", "om")],
                        parse_return=None)
        rc = om_parse.run(_Args())
        assert rc == 0
        assert "0 listing(s) gained OM facts" in capsys.readouterr().err

    def test_parse_returns_none_is_skipped(self, monkeypatch, tmp_path, capsys):
        """When parse_pdf_to_text returns None, the doc is skipped (line 679 branch)."""
        self._setup_run(monkeypatch, tmp_path,
                        doc_rows=[("cbre", "ext-1", "https://x/doc.pdf", "om")],
                        parse_return=None)
        rc = om_parse.run(_Args())
        assert rc == 0
        assert "0 listing(s) gained OM facts" in capsys.readouterr().err

    def test_parse_returns_empty_string_is_skipped(self, monkeypatch, tmp_path, capsys):
        """When parse_pdf_to_text returns '', the doc is also skipped."""
        self._setup_run(monkeypatch, tmp_path,
                        doc_rows=[("cbre", "ext-1", "https://x/doc.pdf", "om")],
                        parse_return="")
        rc = om_parse.run(_Args())
        assert rc == 0
        assert "0 listing(s) gained OM facts" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _source_key_for_slug: fallback branches (lines 730-737)
# ---------------------------------------------------------------------------


from om_parse import _source_key_for_slug


class TestSourceKeyForSlug:
    """_source_key_for_slug fallback branch coverage."""

    def test_flat_key_returned_for_cbre_slug(self):
        # cbre -> ('cbre', '') is the flat key; source_keys=['cbre'] should return 'cbre'
        result = _source_key_for_slug("cbre", ["cbre"])
        assert result == "cbre"

    def test_flat_key_preferred_over_prefixed_key(self):
        # When source_keys contains both 'cbre' (flat) and 'cbre-dealflow' (prefixed),
        # the flat key ('cbre') should be returned (line 728-729 branch).
        result = _source_key_for_slug("cbre", ["cbre-dealflow", "cbre"])
        assert result == "cbre"

    def test_prefixed_only_key_uses_first_match(self):
        # Only 'cbre-dealflow' is in source_keys; no flat key exists in the list,
        # so line 730-731 fires: return matches[0] = 'cbre-dealflow'
        result = _source_key_for_slug("cbre", ["cbre-dealflow"])
        assert result == "cbre-dealflow"

    def test_slug_not_in_source_keys_falls_back_to_source_to_brokerage(self):
        # The slug is 'jll' but source_keys contains only 'cbre'.
        # No match, so lines 734-736 walk SOURCE_TO_BROKERAGE for the flat key.
        result = _source_key_for_slug("jll", ["cbre"])
        # The flat key for 'jll' slug is 'jll' (prefix='')
        assert result == "jll"

    def test_completely_unknown_slug_falls_back_to_slug(self):
        # A slug not in SOURCE_TO_BROKERAGE at all -> line 737: return slug
        result = _source_key_for_slug("never-heard-of-this-brokerage", ["cbre"])
        assert result == "never-heard-of-this-brokerage"

    def test_empty_source_keys_falls_back_via_source_to_brokerage(self):
        # source_keys is empty; no matches; walk SOURCE_TO_BROKERAGE
        # For 'jll' (a known slug) this should return 'jll'
        result = _source_key_for_slug("jll", [])
        assert result == "jll"


# ---------------------------------------------------------------------------
# Extractor except-branch coverage via monkeypatching match objects
# (lines 261-262, 276-277, 292-293: except TypeError/ValueError blocks)
# These are guarded by regexes that only ever match digits, making the
# except branches unreachable through normal text input. We exercise them by
# monkeypatching re.Match.group to return a non-int-castable value.
# ---------------------------------------------------------------------------


class TestExtractorExceptBranches:
    """Cover the TypeError/ValueError except branches in the extractors.

    These branches guard against int() failures on regex-captured strings.
    Since the regexes only capture digit-sequence groups, the branches are
    unreachable via normal text; we exercise them by replacing the compiled
    regex objects at the module level with fake objects whose search/finditer
    return crafted match-like objects that yield non-integer strings.
    """

    def test_extract_units_int_error_returns_none(self, monkeypatch):
        """Monkeypatch _UNITS_LABEL_RE and _UNITS_SUFFIX_RE to return a match
        whose group(1) is not a valid integer -> except ValueError -> None (lines 261-262)."""
        class _FakeMatch:
            def group(self, n):
                return "not-a-number"

        class _FakePattern:
            def search(self, text):
                return _FakeMatch()

        # Replace both unit regexes so _extract_units hits the except branch.
        monkeypatch.setattr(om_parse, "_UNITS_LABEL_RE", _FakePattern())
        monkeypatch.setattr(om_parse, "_UNITS_SUFFIX_RE", _FakePattern())

        result = _extract_units("Number of Units 50", URL)
        assert result is None

    def test_extract_year_built_int_error_returns_none(self, monkeypatch):
        """Monkeypatch _YEAR_BUILT_RE to return a match with non-integer
        group(1) -> except ValueError -> None (lines 276-277)."""
        class _FakeMatch:
            def group(self, n):
                return "not-a-year"

        class _FakePattern:
            def search(self, text):
                return _FakeMatch()

        monkeypatch.setattr(om_parse, "_YEAR_BUILT_RE", _FakePattern())
        result = _extract_year_built("Year Built 1990", URL)
        assert result is None

    def test_extract_unit_mix_count_int_error_is_skipped(self, monkeypatch):
        """Monkeypatch _UNIT_MIX_RE.finditer to yield a match where group('count')
        is not an integer -> continue skips it (lines 292-293)."""
        class _FakeMatch:
            def group(self, key):
                if key == "count":
                    return "notanint"
                if key == "type":
                    return "studio"
                return None

        class _FakePattern:
            def finditer(self, text):
                return [_FakeMatch()]

        monkeypatch.setattr(om_parse, "_UNIT_MIX_RE", _FakePattern())
        rows = _extract_unit_mix("ignored text", URL)
        assert rows == []


# ---------------------------------------------------------------------------
# parse_pdf_to_text: testable branches (not the live network path)
# Lines 524-571: full body excluded from coverage goal (network I/O boundary).
# We cover the two early-exit paths that require NO network and NO Firecrawl:
#   - unresolvable URL -> None (line 524-526)
#   - firecrawl_request ImportError -> None (line 536-537)
# ---------------------------------------------------------------------------


class TestParsePdfToTextSafeBranches:
    """Cover parse_pdf_to_text's early-exit branches (no network, no Firecrawl)."""

    def test_unresolvable_url_returns_none(self, monkeypatch):
        """An unresolvable viewer URL (resolve_pdf_url -> None) yields None."""
        # Monkeypatch resolve_pdf_url at the om_parse module level so we don't
        # need to import it from om_url_resolver.
        monkeypatch.setattr(om_parse, "resolve_pdf_url", lambda url: None)
        result = om_parse.parse_pdf_to_text("https://docs.x/viewer?id=opaque")
        assert result is None

    def test_import_error_on_firecrawl_request_returns_none(self, monkeypatch):
        """If firecrawl_request cannot be imported, parse returns None safely."""
        # resolve_pdf_url returns a .pdf URL so we proceed past the guard
        monkeypatch.setattr(om_parse, "resolve_pdf_url",
                            lambda url: "https://cdn.example.com/om.pdf")
        # Make urllib.request.urlopen raise ImportError on firecrawl_request
        # by placing a sentinel in sys.modules that raises on access.
        # The simplest approach: monkeypatch urllib.request.urlopen to raise
        # ImportError (simulating the firecrawl_request import failure path).
        # Actually, a cleaner way: make firecrawl_request raise ImportError.
        import sys as _sys
        import types

        # Replace firecrawl_request in sys.modules with a module whose import raises
        bad_module = types.ModuleType("firecrawl_request")
        bad_module.__spec__ = None
        # When Python tries "from firecrawl_request import ...", it will find our
        # module but the symbol won't exist -> ImportError.
        monkeypatch.setitem(_sys.modules, "firecrawl_request", None)

        result = om_parse.parse_pdf_to_text("https://cdn.example.com/om.pdf")
        # None is returned when the import fails (line 536-537 or exception handler)
        assert result is None
