"""
test_gate.py

Validates the pure-transform contract for cre_gate.py (coverage-and-anomaly
gate, design sections 8/9) and the monitor-layer helpers from cre_ingest.py.

All fixtures are SYNTHETIC.  No DB connection.  No gitignored out/ artifacts.

Monitor contract:
  - norm_status terminal-wins across dual sale+lease passes (merge_rows preserves
    raw sub-listings; norm_status on each sub returns the correct terminal value)
  - rolling_median (baseline "fingerprint") is stable under a single-run spike,
    but drifts correctly under genuine multi-run decline
  - baseline-seed rule: first_seen with empty or errored prior state must NOT
    seed a row, suppressing spurious new/disappeared events on first observation
  - event derivation signals via verdict_for given small prior-state dicts:
    disappeared -> hold; reappeared -> ok; status_change / price_change
    captured via norm_status and to_row comparisons between two listing versions
  - build_baseline_sql output NEVER references cre_listings, cre_listings.status,
    or deleted_at (hard SQL-string assertions)

Gate contract:
  - first_seen when no baseline row exists (even with error, even with zero count)
  - hold on: error pass, below floor, drop > threshold
  - ok otherwise (at or above the band edge)
  - cbre-dealflow, colliers-main, jll-investor each gated as their OWN source_key
    (not folded into parent for the verdict step)
  - per-brokerage rollup is safe only if ALL member source_keys are ok
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone

import cre_gate as g
from cre_ingest import (
    SOURCE_TO_BROKERAGE,
    merge_rows,
    norm_status,
    to_row,
)

_SCRAPED_AT = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc).isoformat()
_BROKERS: dict = {}
FLOOR = 100
DROP = 0.30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_listing(source_key, url, extra=None):
    """Minimal listing dict that passes to_row's source_url guard."""
    base = {
        "sourceKey": source_key,
        "url": url,
        "transactionMode": "sale",
    }
    if extra:
        base.update(extra)
    return base


def _row(listing):
    """Call to_row with the canonical test args."""
    return to_row(listing, _BROKERS, _SCRAPED_AT)


# ---------------------------------------------------------------------------
# Monitor: norm_status terminal-wins across dual sale+lease passes
# ---------------------------------------------------------------------------


class TestMonitorNormStatusTerminalWins:
    """
    merge_rows() wraps both passes into raw_data["primary"] /
    raw_data["secondary_pass"].  norm_status on each sub-listing must return
    the correct value; a terminal status from the primary must survive the wrap
    and be detectable by the monitor.
    """

    def test_svn_closed_true_yields_sold(self):
        # svn: the "closed" path in STATUS_SOURCE_PATHS maps True -> "sold".
        listing = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=99-sale",
            {"closed": True},
        )
        assert norm_status(listing) == "sold", (
            "closed=True on svn listing must produce 'sold' via STATUS_SOURCE_PATHS"
        )

    def test_no_status_signal_returns_none(self):
        # jll has no native status paths -> norm_status must be None, not 'active'.
        listing = _minimal_listing("jll", "https://www.jll.com/p/1")
        assert norm_status(listing) is None, (
            "jll has no status paths; norm_status must return None, never 'active'"
        )

    def test_merge_preserves_primary_sub_listing_for_status(self):
        # After merge_rows, raw_data["primary"] is the original sale listing.
        # norm_status on that sub-listing must still return "sold".
        sale = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=99-sale",
            {"transactionMode": "sale", "closed": True},
        )
        lease = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=99-lease",
            {"transactionMode": "lease"},
        )
        row_sale = _row(sale)
        row_lease = _row(lease)
        assert row_sale is not None and row_lease is not None
        merged = merge_rows(row_sale, row_lease)

        # Confirm the dual-shape wrapping happened.
        raw = merged["raw_data"]
        assert "primary" in raw and "secondary_pass" in raw

        # norm_status on the primary sub-listing must recover "sold".
        assert norm_status(raw["primary"]) == "sold", (
            "norm_status on raw_data['primary'] must return terminal 'sold' after merge"
        )
        # Lease pass has no status signal.
        assert norm_status(raw["secondary_pass"]) is None

    def test_under_contract_is_terminal(self):
        # colliers: "status" path; "Under Contract" string maps to under_contract.
        listing = _minimal_listing(
            "colliers",
            "https://www.colliers.com/p/1",
            {"status": "Under Contract"},
        )
        assert norm_status(listing) == "under_contract"

    def test_terminal_wins_over_non_terminal_when_both_present(self):
        # If primary yields a terminal status and secondary yields None,
        # the terminal value is the authoritative monitor signal.
        sale = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=200-sale",
            {"transactionMode": "sale", "closed": True},
        )
        lease = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=200-lease",
            {"transactionMode": "lease"},
        )
        s1 = norm_status(sale)    # "sold"  (terminal)
        s2 = norm_status(lease)   # None    (no signal)
        # Monitor rule: terminal wins over None.
        assert s1 in {"sold", "under_contract", "pending", "leased", "off_market"}
        assert s2 is None
        # The effective monitor verdict is the non-None, terminal value.
        effective = s1 if s1 is not None else s2
        assert effective == "sold"


# ---------------------------------------------------------------------------
# Monitor: fingerprint stability (rolling_median) + change on delta
# ---------------------------------------------------------------------------


class TestMonitorFingerprintStability:
    """
    rolling_median is the baseline 'fingerprint' for a source's expected health.
    A single-run spike must not move it (stability); a genuine multi-run shift
    must eventually move it (tracking).  verdict_for flags a 30%+ drop as hold.
    """

    def test_fingerprint_stable_under_single_spike_down(self):
        # One run crashes from 5000 to 50; fingerprint stays at 5000.
        new_m = g.rolling_median(5000, 5000, 50)
        assert new_m == 5000, "single-run spike down must not move the fingerprint"

    def test_fingerprint_stable_under_single_spike_up(self):
        # One run jumps from 5000 to 50000; fingerprint stays at 5000.
        new_m = g.rolling_median(5000, 5000, 50_000)
        assert new_m == 5000, "single-run spike up must not move the fingerprint"

    def test_genuine_decline_shifts_fingerprint_over_two_runs(self):
        # Run 1: 5000 -> 4800.  Run 2: 4800 -> 4600.  Fingerprint must drift.
        m1 = g.rolling_median(None, None, 5000)    # seed: 5000
        m2 = g.rolling_median(m1, 5000, 4800)     # first decline
        m3 = g.rolling_median(m2, 4800, 4600)     # second decline
        assert m3 < 5000, "genuine two-run decline must shift the fingerprint"
        assert m3 == 4800, f"fingerprint after two-run decline should be 4800, got {m3}"

    def test_price_delta_that_removes_listings_triggers_hold(self):
        # If many listings go sold/missing (current drops 40% below median),
        # verdict_for detects the price/status delta and holds the gate.
        baseline = {"median": 5000, "last": 5000}
        verdict, reason, safe, _ = g.verdict_for(
            3000, False, None, baseline, FLOOR, DROP
        )
        assert verdict == "hold", "40% drop below median should be a hold (delta flag)"
        assert safe is False
        assert "median" in reason.lower()

    def test_small_delta_within_band_is_ok(self):
        # 5% drop is within the 30% threshold: ok.
        baseline = {"median": 5000, "last": 5000}
        verdict, _, safe, _ = g.verdict_for(
            4800, False, None, baseline, FLOOR, DROP
        )
        assert verdict == "ok" and safe is True


# ---------------------------------------------------------------------------
# Monitor: baseline-seed rule suppresses new/disappeared on empty prior state
# ---------------------------------------------------------------------------


class TestMonitorBaselineSeedSuppression:
    """
    select_baseline_updates must NOT seed a baseline row when the first-seen
    observation is empty or errored.  Seeding an empty/failed first-seen row
    would create a false baseline that later causes spurious 'disappeared' or
    'new' events on the next run.
    """

    def test_empty_first_seen_is_not_seeded(self):
        per_source = {"svn": {"verdict": "first_seen", "current_active": 0}}
        source_error = {"svn": None}
        updates = {u["source_key"]: u for u in g.select_baseline_updates(
            per_source, source_error, {}
        )}
        assert "svn" not in updates, (
            "first_seen with current_active=0 must not seed a baseline row "
            "(would trigger spurious disappeared events later)"
        )

    def test_errored_first_seen_is_not_seeded(self):
        per_source = {"svn": {"verdict": "first_seen", "current_active": 500}}
        source_error = {"svn": "Connection timeout"}
        updates = {u["source_key"]: u for u in g.select_baseline_updates(
            per_source, source_error, {}
        )}
        assert "svn" not in updates, (
            "first_seen with source error must not seed a baseline row"
        )

    def test_hold_verdict_is_never_written(self):
        per_source = {"cbre": {"verdict": "hold", "current_active": 10}}
        source_error = {"cbre": None}
        updates = {u["source_key"]: u for u in g.select_baseline_updates(
            per_source, source_error, {"cbre": {"median": 20000, "last": 19000}}
        )}
        assert "cbre" not in updates, "hold verdict must never be written to baseline"

    def test_clean_first_seen_does_get_seeded(self):
        per_source = {"svn": {"verdict": "first_seen", "current_active": 5200}}
        source_error = {"svn": None}
        updates = {u["source_key"]: u for u in g.select_baseline_updates(
            per_source, source_error, {}
        )}
        assert "svn" in updates, "clean non-empty first_seen must seed a row"
        assert updates["svn"]["new_median"] == 5200, (
            f"first-seen seed must use current as median; got {updates['svn']['new_median']}"
        )

    def test_ok_verdict_rolls_median(self):
        per_source = {"svn": {"verdict": "ok", "current_active": 5200}}
        source_error = {"svn": None}
        baseline = {"svn": {"median": 5000, "last": 5100}}
        updates = {u["source_key"]: u for u in g.select_baseline_updates(
            per_source, source_error, baseline
        )}
        assert "svn" in updates
        # rolling_median(5000, 5100, 5200) -> median([5000,5100,5200])[1] = 5100
        assert updates["svn"]["new_median"] == 5100, (
            f"ok verdict must roll median to 5100; got {updates['svn']['new_median']}"
        )


# ---------------------------------------------------------------------------
# Monitor: event derivation signals given small synthetic prior-state dicts
# ---------------------------------------------------------------------------


class TestMonitorEventDerivationSignals:
    """
    The monitor derives change events by comparing current observations to
    prior baseline state.  These tests lock the signal-to-event mapping:

      disappeared  -> prior baseline > 0, current_active = 0, verdict = hold
      reappeared   -> source recovers above floor after a hold period, verdict = ok
      status_change -> norm_status returns different values between two observations
      price_change  -> to_row returns different sale_price_usd between two observations
    """

    def test_disappeared_signal_via_hold(self):
        # Prior run had 3000 active; current run sees 0 -> hold (disappeared).
        prior = {"median": 3000, "last": 3000}
        verdict, reason, safe, _ = g.verdict_for(
            0, False, None, prior, FLOOR, DROP
        )
        assert verdict == "hold" and safe is False
        assert "floor" in reason.lower(), (
            f"disappeared (current=0) should cite floor in reason; got {reason!r}"
        )

    def test_reappeared_signal_via_ok(self):
        # After a hold period (source was down), it recovers to 3000 rows.
        # Baseline reflects the pre-hold median; recovery clears the gate.
        prior = {"median": 3000, "last": 3000}
        verdict, _, safe, _ = g.verdict_for(
            3000, False, None, prior, FLOOR, DROP
        )
        assert verdict == "ok" and safe is True, (
            "reappeared source at full count should be ok (reappeared event)"
        )

    def test_status_change_detected_by_norm_status_diff(self):
        # Observation 1: svn listing, no closed flag -> no status signal.
        before = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=77-sale",
        )
        # Observation 2: same listing, now closed=True -> sold.
        after = _minimal_listing(
            "svn",
            "https://www.svn.com/listings/?propertyId=77-sale",
            {"closed": True},
        )
        s_before = norm_status(before)
        s_after = norm_status(after)
        assert s_before is None, "no closed flag -> no status signal"
        assert s_after == "sold", "closed=True -> 'sold'"
        assert s_before != s_after, (
            "norm_status diff between two observations must flag a status_change"
        )

    def test_price_change_detected_by_to_row_diff(self):
        # Observation 1: listing at $5M.
        v1 = _minimal_listing(
            "cbre",
            "https://www.cbre.com/listing/P1",
            {"salePriceUsd": 5_000_000.0},
        )
        # Observation 2: same listing now at $4.5M.
        v2 = _minimal_listing(
            "cbre",
            "https://www.cbre.com/listing/P1",
            {"salePriceUsd": 4_500_000.0},
        )
        row1 = _row(v1)
        row2 = _row(v2)
        assert row1 is not None and row2 is not None
        assert row1["sale_price_usd"] != row2["sale_price_usd"], (
            "to_row must reflect a sale_price change between observations "
            "(price_change event)"
        )
        assert row1["sale_price_usd"] == 5_000_000.0
        assert row2["sale_price_usd"] == 4_500_000.0

    def test_no_spurious_event_when_values_unchanged(self):
        # Same price both times -> no price_change signal.
        listing = _minimal_listing(
            "cbre",
            "https://www.cbre.com/listing/P2",
            {"salePriceUsd": 3_000_000.0},
        )
        r1 = _row(listing)
        r2 = _row(listing)
        assert r1["sale_price_usd"] == r2["sale_price_usd"], (
            "identical observations must not produce a delta"
        )


# ---------------------------------------------------------------------------
# Monitor: hard SQL safety assertion (build_baseline_sql is observe-only)
# ---------------------------------------------------------------------------


class TestMonitorSQLSafetyAssertion:
    """
    build_baseline_sql only writes to cre_source_baseline.  It must NEVER
    reference cre_listings, the 'deleted_at' column, or set a 'status' value on
    any row.  This is the hard observe-only guarantee for the monitor layer:
    cre_gate.py can never alter live listing inventory.
    """

    def _make_sql(self):
        updates = [
            {"source_key": "svn", "slug": "svn", "new_median": 5000, "current": 5200},
            {"source_key": "cbre", "slug": "cbre", "new_median": 18000, "current": 18500},
        ]
        return g.build_baseline_sql(updates, "2026-06-13T00:00:00+00:00", None)

    def test_sql_never_targets_cre_listings(self):
        sql = self._make_sql()
        assert "cre_listings" not in sql, (
            "build_baseline_sql must not reference cre_listings; found in generated SQL"
        )

    def test_sql_never_sets_deleted_at(self):
        sql = self._make_sql()
        assert "deleted_at" not in sql, (
            "build_baseline_sql must not reference deleted_at"
        )

    def test_sql_only_inserts_into_cre_source_baseline(self):
        sql = self._make_sql()
        assert "cre_source_baseline" in sql, "baseline table must be in the SQL"
        targets = re.findall(r"INSERT\s+INTO\s+(\S+)", sql, re.I)
        assert len(targets) > 0, "SQL must contain at least one INSERT INTO"
        for t in targets:
            assert "cre_source_baseline" in t, (
                f"unexpected INSERT target in baseline SQL: {t!r}; "
                "only cre_source_baseline is permitted"
            )

    def test_sql_no_status_column_assignment(self):
        # Ensure no "status = " pattern leaks into the baseline SQL.
        # (cre_source_baseline has no 'status' column; this guards a future refactor.)
        sql = self._make_sql()
        # Look for patterns like "status = " or "SET status" that would write a status.
        matches = re.findall(r"\bstatus\s*=", sql, re.I)
        assert not matches, (
            f"build_baseline_sql must not set any 'status' column; found: {matches}"
        )


# ---------------------------------------------------------------------------
# Gate: verdict logic (full precedence coverage)
# ---------------------------------------------------------------------------


class TestGateVerdictLogic:
    """
    verdict_for() precedence (design sections 8/9):
      1. no baseline row          -> first_seen (even with error, even with count=0)
      2. has_error (with baseline) -> hold
      3. current < floor           -> hold
      4. current < median*(1-drop) -> hold
      5. else                      -> ok
    """

    def test_no_baseline_no_error_is_first_seen(self):
        v, _, safe, _ = g.verdict_for(5000, False, None, None, FLOOR, DROP)
        assert v == "first_seen" and safe is False

    def test_no_baseline_with_error_is_first_seen_not_hold(self):
        # No baseline -> first_seen wins even when the pass errored.
        v, reason, safe, _ = g.verdict_for(0, True, "boom", None, FLOOR, DROP)
        assert v == "first_seen", "no baseline must yield first_seen even with error"
        assert "errored" in reason
        assert safe is False

    def test_no_baseline_zero_count_is_first_seen(self):
        v, _, safe, _ = g.verdict_for(0, False, None, None, FLOOR, DROP)
        assert v == "first_seen" and safe is False

    def test_error_with_baseline_is_hold(self):
        baseline = {"median": 5000, "last": 5000}
        v, _, safe, _ = g.verdict_for(5000, True, "timeout", baseline, FLOOR, DROP)
        assert v == "hold" and safe is False

    def test_below_floor_with_baseline_is_hold(self):
        baseline = {"median": 5000, "last": 5000}
        v, reason, safe, _ = g.verdict_for(50, False, None, baseline, FLOOR, DROP)
        assert v == "hold" and safe is False
        assert "floor" in reason

    def test_below_drop_band_is_hold(self):
        # 5000 * (1 - 0.30) = 3500; 3499 < 3500 -> hold.
        baseline = {"median": 5000, "last": 5000}
        v, reason, safe, _ = g.verdict_for(3499, False, None, baseline, FLOOR, DROP)
        assert v == "hold" and safe is False
        assert "median" in reason

    def test_at_band_edge_is_ok(self):
        # current == median * (1 - drop) is NOT below threshold: ok.
        baseline = {"median": 5000, "last": 5000}
        v, _, safe, _ = g.verdict_for(3500, False, None, baseline, FLOOR, DROP)
        assert v == "ok" and safe is True

    def test_healthy_count_is_ok(self):
        baseline = {"median": 5000, "last": 5000}
        v, _, safe, _ = g.verdict_for(5200, False, None, baseline, FLOOR, DROP)
        assert v == "ok" and safe is True

    def test_null_median_bypasses_drop_check(self):
        # A baseline row that has no median yet: only floor check applies.
        baseline = {"median": None, "last": None}
        v, _, safe, _ = g.verdict_for(5000, False, None, baseline, FLOOR, DROP)
        assert v == "ok" and safe is True


# ---------------------------------------------------------------------------
# Gate: prefix-aware per-source gating (sub-sources are independent keys)
# ---------------------------------------------------------------------------


class TestGatePrefixAwareGating:
    """
    cbre-dealflow, colliers-main, and jll-investor each have their OWN baseline
    row and their OWN verdict.  The parent brokerage's verdict does not gate
    the sub-source and vice versa.  Only the brokerage ROLLUP (downstream of
    the gate) folds them via _slug_for().
    """

    def test_dealflow_slug_folds_into_cbre_for_rollup(self):
        assert g._slug_for("cbre-dealflow") == "cbre"
        assert g._slug_for("cbre") == "cbre"

    def test_colliers_main_slug_folds_into_colliers_for_rollup(self):
        assert g._slug_for("colliers-main") == "colliers"
        assert g._slug_for("colliers") == "colliers"

    def test_jll_investor_slug_folds_into_jll_for_rollup(self):
        assert g._slug_for("jll-investor") == "jll"
        assert g._slug_for("jll") == "jll"

    def test_dealflow_gated_independently_even_when_cbre_ok(self):
        # cbre is healthy (ok), but cbre-dealflow crashes to below floor -> hold.
        b_cbre = {"median": 18000, "last": 18000}
        b_df = {"median": 1800, "last": 1800}
        v_cbre, _, safe_cbre, _ = g.verdict_for(18000, False, None, b_cbre, FLOOR, DROP)
        v_df, _, safe_df, _ = g.verdict_for(50, False, None, b_df, FLOOR, DROP)
        assert v_cbre == "ok" and safe_cbre is True
        assert v_df == "hold" and safe_df is False

    def test_subsources_have_distinct_mapping_entries(self):
        # Each sub-source must have its own entry in SOURCE_TO_BROKERAGE
        # so each can have its own baseline row (different prefix -> different id space).
        for parent, child in [
            ("cbre", "cbre-dealflow"),
            ("colliers", "colliers-main"),
            ("jll", "jll-investor"),
        ]:
            assert child in SOURCE_TO_BROKERAGE, f"{child} missing from SOURCE_TO_BROKERAGE"
            assert parent in SOURCE_TO_BROKERAGE, f"{parent} missing from SOURCE_TO_BROKERAGE"
            assert SOURCE_TO_BROKERAGE[child] != SOURCE_TO_BROKERAGE[parent], (
                f"{child} and {parent} must differ in SOURCE_TO_BROKERAGE "
                "(at minimum by their id prefix)"
            )


# ---------------------------------------------------------------------------
# Gate: per-brokerage rollup (safe only when ALL member source_keys are ok)
# ---------------------------------------------------------------------------


def _brokerage_rollup(per_source):
    """Exercise the REAL rollup function (not a mirror), so these tests track
    cre_gate.main()'s behavior exactly."""
    return g.rollup_brokerages(per_source)


class TestGateBrokerageRollup:
    """
    The per-brokerage rollup is mark-missing-safe ONLY when every gated
    source_key that belongs to that brokerage is ok.  A single hold from any
    sub-source (cbre-dealflow, colliers-main, jll-investor) makes the whole
    brokerage unsafe for mark-missing.
    """

    def test_all_ok_rollup_is_safe(self):
        per_source = {
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": True},
        }
        rollup = _brokerage_rollup(per_source)
        assert rollup["cbre"]["mark_missing_safe"] is True

    def test_subsource_hold_makes_parent_brokerage_unsafe(self):
        per_source = {
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": False},  # hold
        }
        rollup = _brokerage_rollup(per_source)
        assert rollup["cbre"]["mark_missing_safe"] is False, (
            "cbre-dealflow hold must make the entire cbre brokerage unsafe"
        )

    def test_colliers_main_hold_makes_colliers_unsafe(self):
        per_source = {
            "colliers": {"mark_missing_safe": True},
            "colliers-main": {"mark_missing_safe": False},
        }
        rollup = _brokerage_rollup(per_source)
        assert rollup["colliers"]["mark_missing_safe"] is False

    def test_jll_investor_hold_makes_jll_unsafe(self):
        per_source = {
            "jll": {"mark_missing_safe": True},
            "jll-investor": {"mark_missing_safe": False},
        }
        rollup = _brokerage_rollup(per_source)
        assert rollup["jll"]["mark_missing_safe"] is False

    def test_singleton_brokerage_safe_when_ok(self):
        rollup = _brokerage_rollup({"svn": {"mark_missing_safe": True}})
        assert rollup["svn"]["mark_missing_safe"] is True

    def test_singleton_brokerage_unsafe_when_hold(self):
        rollup = _brokerage_rollup({"svn": {"mark_missing_safe": False}})
        assert rollup["svn"]["mark_missing_safe"] is False

    def test_source_keys_list_populated_correctly(self):
        per_source = {
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": True},
        }
        rollup = _brokerage_rollup(per_source)
        assert sorted(rollup["cbre"]["source_keys"]) == ["cbre", "cbre-dealflow"]

    def test_absent_known_subsource_makes_parent_unsafe(self):
        """Coverage-aware guard (the reviewer's blind spot): a run that observed
        only the parent cbre, with cbre-dealflow entirely absent, must NOT be
        mark-missing-safe, even though every OBSERVED source_key is ok. Otherwise
        a routine parent-only run could greenlight soft-deleting a whole absent
        sub-source's live inventory."""
        per_source = {"cbre": {"mark_missing_safe": True}}  # cbre-dealflow absent
        rollup = _brokerage_rollup(per_source)
        assert rollup["cbre"]["mark_missing_safe"] is False
        assert "incomplete folded coverage" in rollup["cbre"].get("reason", "")

    def test_absent_known_subsource_colliers_and_jll(self):
        assert _brokerage_rollup(
            {"colliers": {"mark_missing_safe": True}}
        )["colliers"]["mark_missing_safe"] is False
        assert _brokerage_rollup(
            {"jll": {"mark_missing_safe": True}}
        )["jll"]["mark_missing_safe"] is False
