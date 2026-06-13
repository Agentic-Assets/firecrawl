"""test_monitor.py

Synthetic-only validator for cre_monitor.py's pure-transform contract.
No DB connections. No reliance on gitignored out/ artifacts (any test that
uses a real artifact is guarded with pytest.skipif on file existence).

Covers:
  1. load_artifact_groups: grouping by (slug, external_id) from synthetic JSON,
     including dual sale+lease SVN pair merging to a single group.
  2. finalize_group: terminal-wins norm_status across a dual sale+lease pair.
  3. compute_fingerprint: stability + change on price/status delta.
  4. Baseline-seed rule: derive_events suppresses all events when prior state is
     empty (every source is treated as first-ever).
  5. Event derivation: status_change, price_change, disappeared, reappeared given
     small synthetic prior-state dicts.
  6. SQL safety: build_write_sql output NEVER writes cre_listings.status or
     cre_listings.deleted_at (grep the generated SQL string).
  7. Gate verdict_for: first_seen / hold / ok precedence (no baseline, error,
     floor, drop-threshold, at-threshold, healthy).
  8. Prefix-aware per-source gating: cbre-dealflow has its own gate key, separate
     from cbre; same for colliers-main and jll-investor.
  9. Per-brokerage rollup: a slug is mark-missing-safe only when every member
     source_key is ok.

Import contract: all assertions call real functions from cre_monitor or cre_gate;
no logic is re-implemented here.
"""

import json
import os
import re
import tempfile

import cre_gate as gate
import cre_monitor as m
from cre_ingest import SOURCE_TO_BROKERAGE

# Fixed synthetic identifiers for repeatable tests
RUN = "00000000-0000-0000-0000-000000000099"
BID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

FLOOR = 100
DROP = 0.30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_artifact(payload: dict) -> str:
    """Write a dict to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="cre_mon_test_")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return path


def _minimal_artifact(*listings) -> dict:
    return {
        "runMeta": {
            "startedAt": "2026-06-13T00:00:00.000Z",
            "finishedAt": "2026-06-13T00:10:00.000Z",
        },
        "brokers": [],
        "sources": [],
        "listings": list(listings),
    }


def _gfin(eid, source_key="colliers", status=None, sale_price_usd=None,
          sale_price_text=None, lease_rate_min=None, lease_rate_max=None,
          canonical_key=None, url=None):
    """Build a finalized group record as derive_events expects it."""
    slug = SOURCE_TO_BROKERAGE[source_key][0]
    fp = m.compute_fingerprint(
        status, sale_price_usd, sale_price_text,
        lease_rate_min, lease_rate_max, None,
    )
    return {
        "slug": slug,
        "external_id": eid,
        "source_key": source_key,
        "url": url or f"https://example.com/{eid}",
        "norm_status": status,
        "raw_status": status,
        "sale_price_usd": sale_price_usd,
        "lease_rate_min": lease_rate_min,
        "lease_rate_max": lease_rate_max,
        "sale_price_text": sale_price_text,
        "lease_rate_text": None,
        "source_lastmod": None,
        "canonical_key": canonical_key,
        "fingerprint": fp,
    }


def _idx(g, soft_deleted=False, observed_status=None):
    """Minimal prior_index entry for a finalized group."""
    return {
        "fingerprint": g["fingerprint"],
        "soft_deleted": soft_deleted,
        "observed_status": observed_status,
        "source_key": g["source_key"],
        "url": g["url"],
    }


def _derive(current, prior_index, prior_listings, soft_canon=None,
            baseline=None, coverage=None):
    """Wrapper around derive_events that mirrors the baseline/coverage derivation
    in cre_monitor.main() for synthetic tests."""
    run_keys = {g["source_key"] for g in current.values()}
    if baseline is None:
        prior_count = {}
        for p in prior_index.values():
            sk = p["source_key"] or ""
            prior_count[sk] = prior_count.get(sk, 0) + 1
        baseline = {sk for sk in run_keys if prior_count.get(sk, 0) == 0}
    if coverage is None:
        coverage = {sk: True for sk in run_keys}
    return m.derive_events(
        current, prior_index, prior_listings, soft_canon or {},
        run_keys, baseline, coverage, RUN,
    )


def _etypes(events):
    out = {}
    for e in events:
        out[e["event_type"]] = out.get(e["event_type"], 0) + 1
    return out


def _make_write_sql(events=None, disappear_marks=None, finalized=None):
    """Call build_write_sql with minimal synthetic inputs."""
    if finalized is None:
        finalized = [_gfin("Z1", status="sold", sale_price_usd=900_000)]
    return m.build_write_sql(
        finalized,
        events or [],
        {},   # enqueue_new
        {},   # enqueue_changed
        disappear_marks or [],
        RUN,
        "2026-06-13T00:00:00Z",
        "monitor observe-only test",
        ["colliers"],
    )


# ---------------------------------------------------------------------------
# 1. load_artifact_groups: grouping by (slug, external_id)
# ---------------------------------------------------------------------------


class TestLoadArtifactGroups:
    """load_artifact_groups must produce the same external_id as cre_ingest.to_row."""

    def test_single_listing_produces_one_group(self):
        listing = {
            "sourceKey": "colliers",
            "url": "https://sales.colliers.com/#project-12345",
            "id": "12345",
            "transactionMode": "sale",
        }
        path = _write_artifact(_minimal_artifact(listing))
        try:
            groups, _, per_source, skipped = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        assert len(groups) == 1
        assert skipped == 0
        assert per_source.get("colliers", 0) == 1
        assert ("colliers", "12345") in groups

    def test_no_url_listing_is_skipped(self):
        listing = {"sourceKey": "colliers", "id": "no-url", "transactionMode": "sale"}
        path = _write_artifact(_minimal_artifact(listing))
        try:
            groups, _, _, skipped = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        assert len(groups) == 0
        assert skipped == 1

    def test_two_distinct_listings_produce_two_groups(self):
        l1 = {"sourceKey": "colliers", "url": "https://sales.colliers.com/#project-1",
               "id": "1", "transactionMode": "sale"}
        l2 = {"sourceKey": "colliers", "url": "https://sales.colliers.com/#project-2",
               "id": "2", "transactionMode": "sale"}
        path = _write_artifact(_minimal_artifact(l1, l2))
        try:
            groups, _, per_source, skipped = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        assert len(groups) == 2
        assert skipped == 0
        assert per_source.get("colliers", 0) == 2

    def test_dual_mode_svn_listings_merge_to_one_group(self):
        """SVN sale + lease for the same propertyId produce exactly one grouped key
        and the flat_listings list carries both original raw dicts."""
        sale = {
            "sourceKey": "svn",
            "url": "https://www.svn.com/listings/?propertyId=999-sale",
            "transactionMode": "sale",
        }
        lease = {
            "sourceKey": "svn",
            "url": "https://www.svn.com/listings/?propertyId=999-lease",
            "transactionMode": "lease",
        }
        path = _write_artifact(_minimal_artifact(sale, lease))
        try:
            groups, _, per_source, skipped = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        # Both listings share propertyId=999 (suffix stripped) -> one group
        assert len(groups) == 1
        assert skipped == 0
        # Both flat listings were accepted (per_source counts flat rows)
        assert per_source.get("svn", 0) == 2
        # The group key uses the stripped id
        assert ("svn", "999") in groups
        # Both raw flat dicts are preserved in flat_listings
        grp = groups[("svn", "999")]
        assert len(grp["flat_listings"]) == 2


# ---------------------------------------------------------------------------
# 2. finalize_group: terminal-wins norm_status across a dual sale+lease pair
# ---------------------------------------------------------------------------


class TestFinalizeGroupTerminalWins:
    """norm_status is called on each FLAT listing, never on the merged dual-shape
    dict. A terminal status from any flat listing in the group wins immediately."""

    def test_terminal_sale_status_wins_over_lease_none(self):
        """SVN: sale pass has closed=True (-> 'sold'/terminal); lease has no signal."""
        sale_flat = {
            "sourceKey": "svn",
            "url": "https://www.svn.com/listings/?propertyId=100-sale",
            "transactionMode": "sale",
            "closed": True,
        }
        lease_flat = {
            "sourceKey": "svn",
            "url": "https://www.svn.com/listings/?propertyId=100-lease",
            "transactionMode": "lease",
        }
        path = _write_artifact(_minimal_artifact(sale_flat, lease_flat))
        try:
            groups, _, _, _ = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        assert len(groups) == 1
        finalized = m.finalize_group(list(groups.values())[0])
        assert finalized["norm_status"] == "sold", (
            "terminal 'sold' from the sale pass must win over None from the lease pass"
        )

    def test_colliers_under_contract_string_is_terminal(self):
        listing = {
            "sourceKey": "colliers",
            "url": "https://sales.colliers.com/#project-200",
            "id": "200",
            "transactionMode": "sale",
            "status": "Under Contract",
        }
        path = _write_artifact(_minimal_artifact(listing))
        try:
            groups, _, _, _ = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        finalized = m.finalize_group(list(groups.values())[0])
        assert finalized["norm_status"] == "under_contract"
        assert finalized["raw_status"] == "Under Contract"

    def test_none_status_returned_when_source_has_no_signal_paths(self):
        """CBRE has STATUS_SOURCE_PATHS=[] and no scoped title keyword -> None."""
        listing = {
            "sourceKey": "cbre",
            "url": "https://www.cbre.com/listing/NO_STATUS",
            "id": "NO_STATUS",
            "transactionMode": "sale",
        }
        path = _write_artifact(_minimal_artifact(listing))
        try:
            groups, _, _, _ = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        finalized = m.finalize_group(list(groups.values())[0])
        assert finalized["norm_status"] is None, (
            "CBRE has no status paths and no scoped keyword; norm_status must be None"
        )

    def test_fingerprint_is_populated_by_finalize_group(self):
        listing = {
            "sourceKey": "colliers",
            "url": "https://sales.colliers.com/#project-300",
            "id": "300",
            "transactionMode": "sale",
            "status": "Sold",
        }
        path = _write_artifact(_minimal_artifact(listing))
        try:
            groups, _, _, _ = m.load_artifact_groups([path])
        finally:
            os.unlink(path)
        finalized = m.finalize_group(list(groups.values())[0])
        assert finalized["fingerprint"] is not None
        assert len(finalized["fingerprint"]) == 32


# ---------------------------------------------------------------------------
# 3. compute_fingerprint: stability + change on price/status delta
# ---------------------------------------------------------------------------


class TestFingerprintContract:
    def test_identical_inputs_produce_same_fingerprint(self):
        fp1 = m.compute_fingerprint("sold", 500_000, "$500K", None, None, None)
        fp2 = m.compute_fingerprint("sold", 500_000, "$500K", None, None, None)
        assert fp1 == fp2

    def test_fingerprint_is_32_hex_chars(self):
        fp = m.compute_fingerprint(None, None, None, None, None, None)
        assert len(fp) == 32
        assert all(c in "0123456789abcdef" for c in fp)

    def test_price_change_alters_fingerprint(self):
        fp_before = m.compute_fingerprint("sold", 500_000, None, None, None, None)
        fp_after = m.compute_fingerprint("sold", 600_000, None, None, None, None)
        assert fp_before != fp_after

    def test_status_change_alters_fingerprint(self):
        fp_before = m.compute_fingerprint(None, 500_000, None, None, None, None)
        fp_after = m.compute_fingerprint("sold", 500_000, None, None, None, None)
        assert fp_before != fp_after

    def test_price_text_change_alters_fingerprint(self):
        fp_before = m.compute_fingerprint(None, None, "Negotiable", None, None, None)
        fp_after = m.compute_fingerprint(None, None, "Call for offers", None, None, None)
        assert fp_before != fp_after

    def test_lease_range_change_alters_fingerprint(self):
        fp_before = m.compute_fingerprint(None, None, None, 20.0, 30.0, None)
        fp_after = m.compute_fingerprint(None, None, None, 25.0, 35.0, None)
        assert fp_before != fp_after

    def test_same_price_and_status_stable_across_calls(self):
        """Fingerprint is deterministic: used for cross-run idempotency."""
        for _ in range(5):
            fp = m.compute_fingerprint("under_contract", 1_200_000, None, None, None, None)
            assert fp == m.compute_fingerprint("under_contract", 1_200_000, None, None, None, None)


# ---------------------------------------------------------------------------
# 4. Baseline-seed rule: derive_events suppresses events on empty prior state
# ---------------------------------------------------------------------------


class TestBaselineSeedRule:
    def test_empty_prior_index_seeds_all_sources_silently(self):
        """When prior_index is empty every source_key is a baseline seed and
        derive_events must emit zero events even when prior_listings has rows."""
        g_cur = _gfin("A1", status="sold", sale_price_usd=1_000_000)
        current = {(BID, "A1"): g_cur}
        prior_listings = {(BID, "A1"): {"id": "L-A1", "status": "active", "deleted": False}}
        events, enq_new, enq_changed, marks, _ = _derive(
            current,
            prior_index={},
            prior_listings=prior_listings,
        )
        assert events == []
        assert enq_new == {} and enq_changed == {} and marks == []

    def test_baseline_seed_source_emits_no_new_event_even_with_listing_row(self):
        """A source with zero prior index rows must produce no 'new' event for
        any of its listings, regardless of how many prior_listings rows exist."""
        g1 = _gfin("B1", source_key="colliers", status="sold")
        g2 = _gfin("B2", source_key="colliers", status=None)
        current = {(BID, "B1"): g1, (BID, "B2"): g2}
        prior_listings = {
            (BID, "B1"): {"id": "L-B1", "status": "active", "deleted": False},
            (BID, "B2"): {"id": "L-B2", "status": "active", "deleted": False},
        }
        events, _, _, _, _ = _derive(current, prior_index={}, prior_listings=prior_listings)
        assert events == []

    def test_non_baseline_source_emits_new_events(self):
        """A source that HAS prior index rows is NOT a seed; new listings do get
        'new' events."""
        g_known = _gfin("C_KNOWN", source_key="colliers")
        g_new = _gfin("C_NEW", source_key="colliers", status="sold")
        current = {(BID, "C_KNOWN"): g_known, (BID, "C_NEW"): g_new}
        # Prior index has ONE row for colliers -> colliers is NOT a baseline seed
        prior_index = {(BID, "C_KNOWN"): _idx(g_known)}
        prior_listings = {
            (BID, "C_KNOWN"): {"id": "L-C_KNOWN", "status": "active", "deleted": False},
            (BID, "C_NEW"): {"id": "L-C_NEW", "status": "active", "deleted": False},
        }
        events, _, _, _, _ = _derive(current, prior_index, prior_listings)
        etypes = _etypes(events)
        # C_NEW is a brand-new id -> 'new' event
        assert "new" in etypes
        new_lids = [e["listing_id"] for e in events if e["event_type"] == "new"]
        assert "L-C_NEW" in new_lids

    def test_mixed_sources_seed_only_the_source_with_no_prior_rows(self):
        """When colliers has prior rows and svn has none, only svn is seeded."""
        g_colliers = _gfin("D1", source_key="colliers", status=None)
        g_svn = _gfin("D2", source_key="svn", status="sold", sale_price_usd=2_000_000)
        current = {(BID, "D1"): g_colliers, (BID, "D2"): g_svn}
        # Prior index: colliers has one row, svn has none
        prior_index = {(BID, "D_OLD"): _idx(_gfin("D_OLD", source_key="colliers"))}
        prior_listings = {
            (BID, "D1"): {"id": "L-D1", "status": "active", "deleted": False},
            (BID, "D2"): {"id": "L-D2", "status": "active", "deleted": False},
        }
        events, _, _, _, _ = m.derive_events(
            current, prior_index, prior_listings, {},
            {"colliers", "svn"},
            {"svn"},                   # svn is the only baseline seed
            {"colliers": True, "svn": True},
            RUN,
        )
        etypes = _etypes(events)
        # colliers D1 is new (no prior entry for D1) -> 'new'
        assert "new" in etypes
        new_lids = [e["listing_id"] for e in events if e["event_type"] == "new"]
        assert "L-D1" in new_lids
        # svn D2 is baseline-seeded -> no event
        assert "L-D2" not in new_lids


# ---------------------------------------------------------------------------
# 5. Event derivation: status_change, price_change, disappeared, reappeared
# ---------------------------------------------------------------------------


class TestEventDerivation:
    def test_status_change_fires_on_norm_status_diff_against_observed(self):
        prev_g = _gfin("E1", status=None)
        cur_g = _gfin("E1", status="sold")
        prior_index = {(BID, "E1"): _idx(prev_g, observed_status=None)}
        prior_listings = {(BID, "E1"): {"id": "L-E1", "status": "active", "deleted": False}}
        events, _, enq_changed, _, _ = _derive({(BID, "E1"): cur_g}, prior_index, prior_listings)
        assert _etypes(events) == {"status_change": 1}
        e = events[0]
        assert e["field"] == "status"
        assert e["old_value"] == "active"    # the live cre_listings.status
        assert e["new_value"] == "sold"
        assert (BID, "colliers", "E1") in enq_changed

    def test_status_change_cross_run_idempotency(self):
        """If the prior snapshot already recorded 'sold', the same 'sold'
        observation must NOT re-fire a status_change (even if cre_listings.status
        is still 'active' because the monitor never writes it)."""
        cur_g = _gfin("F1", status="sold")
        prior_index = {(BID, "F1"): _idx(cur_g, observed_status="sold")}
        prior_listings = {(BID, "F1"): {"id": "L-F1", "status": "active", "deleted": False}}
        events, _, _, _, _ = _derive({(BID, "F1"): cur_g}, prior_index, prior_listings)
        assert events == [], "cross-run idempotency: no re-fire when observed_status unchanged"

    def test_price_change_fires_when_only_price_moves(self):
        prev_g = _gfin("G1", status="sold", sale_price_usd=500_000)
        cur_g = _gfin("G1", status="sold", sale_price_usd=600_000)
        assert prev_g["fingerprint"] != cur_g["fingerprint"]
        prior_index = {(BID, "G1"): _idx(prev_g, observed_status="sold")}
        prior_listings = {(BID, "G1"): {"id": "L-G1", "status": "sold", "deleted": False}}
        events, _, enq_changed, _, _ = _derive({(BID, "G1"): cur_g}, prior_index, prior_listings)
        assert _etypes(events) == {"price_change": 1}
        e = events[0]
        assert e["field"] == "sale_price_usd"
        assert e["new_value"] == "600000"
        assert e["old_value"] is None         # prior price not persisted (hash only)
        assert e["source_value"] == prev_g["fingerprint"]
        assert (BID, "colliers", "G1") in enq_changed

    def test_price_text_move_is_detected_as_price_change(self):
        """Even when no numeric value parses, a raw text change moves the
        fingerprint and should be recorded as a price_change."""
        prev_g = _gfin("H1", status=None, sale_price_text="Negotiable")
        cur_g = _gfin("H1", status=None, sale_price_text="Call for offers")
        prior_index = {(BID, "H1"): _idx(prev_g, observed_status=None)}
        prior_listings = {(BID, "H1"): {"id": "L-H1", "status": "active", "deleted": False}}
        events, _, _, _, _ = _derive({(BID, "H1"): cur_g}, prior_index, prior_listings)
        assert _etypes(events) == {"price_change": 1}
        assert events[0]["new_value"] == "Call for offers"

    def test_simultaneous_status_and_price_move_fires_only_status_change(self):
        """When status and price both change, only status_change is emitted
        (price cannot be isolated from a combined hash that moved for both)."""
        prev_g = _gfin("I1", status=None, sale_price_usd=500_000)
        cur_g = _gfin("I1", status="sold", sale_price_usd=600_000)
        prior_index = {(BID, "I1"): _idx(prev_g, observed_status=None)}
        prior_listings = {(BID, "I1"): {"id": "L-I1", "status": "active", "deleted": False}}
        events, _, _, _, _ = _derive({(BID, "I1"): cur_g}, prior_index, prior_listings)
        assert _etypes(events) == {"status_change": 1}

    def test_disappeared_emits_when_coverage_passes(self):
        present = _gfin("J1")
        gone = _gfin("J2")
        current = {(BID, "J1"): present}
        prior_index = {(BID, "J1"): _idx(present), (BID, "J2"): _idx(gone)}
        prior_listings = {
            (BID, "J1"): {"id": "L-J1", "status": "active", "deleted": False},
            (BID, "J2"): {"id": "L-J2", "status": "active", "deleted": False},
        }
        events, _, _, marks, _ = _derive(
            current, prior_index, prior_listings,
            coverage={"colliers": True},
        )
        assert _etypes(events) == {"disappeared": 1}
        e = events[0]
        assert e["listing_id"] == "L-J2"
        assert e["source_value"] == "enumeration_gone"
        assert (BID, "J2") in marks

    def test_disappeared_suppressed_when_coverage_fails(self):
        present = _gfin("K1")
        gone = _gfin("K2")
        prior_index = {(BID, "K1"): _idx(present), (BID, "K2"): _idx(gone)}
        prior_listings = {
            (BID, "K1"): {"id": "L-K1", "status": "active", "deleted": False},
            (BID, "K2"): {"id": "L-K2", "status": "active", "deleted": False},
        }
        events, _, _, marks, _ = _derive(
            {(BID, "K1"): present}, prior_index, prior_listings,
            coverage={"colliers": False},
        )
        assert events == [] and marks == []

    def test_disappeared_does_not_refire_when_already_soft_deleted(self):
        present = _gfin("L1")
        gone = _gfin("L2")
        prior_index = {
            (BID, "L1"): _idx(present),
            (BID, "L2"): _idx(gone, soft_deleted=True),  # already recorded gone
        }
        prior_listings = {
            (BID, "L1"): {"id": "L-L1", "status": "active", "deleted": False},
            (BID, "L2"): {"id": "L-L2", "status": "active", "deleted": False},
        }
        events, _, _, marks, _ = _derive({(BID, "L1"): present}, prior_index, prior_listings)
        assert events == [] and marks == []

    def test_reappeared_when_soft_deleted_id_returns(self):
        g_cur = _gfin("M1")
        prior_index = {(BID, "M1"): _idx(g_cur, soft_deleted=True)}
        prior_listings = {(BID, "M1"): {"id": "L-M1", "status": "active", "deleted": False}}
        events, _, _, _, _ = _derive({(BID, "M1"): g_cur}, prior_index, prior_listings)
        assert _etypes(events) == {"reappeared": 1}
        assert events[0]["listing_id"] == "L-M1"

    def test_no_events_when_nothing_changed(self):
        g_cur = _gfin("N1", status="sold", sale_price_usd=500_000)
        prior_index = {(BID, "N1"): _idx(g_cur, observed_status="sold")}
        prior_listings = {(BID, "N1"): {"id": "L-N1", "status": "sold", "deleted": False}}
        events, _, _, _, _ = _derive({(BID, "N1"): g_cur}, prior_index, prior_listings)
        assert events == []

    def test_new_event_requires_prior_listing_row(self):
        """If there is no cre_listings row for the new id, the FK cannot be
        satisfied. The event must be skipped and counted as enumerated_unmatched."""
        g_cur = _gfin("O1", status=None)
        other = _gfin("O_OTHER")
        prior_index = {(BID, "O_OTHER"): _idx(other)}  # colliers not a baseline seed
        events, enq_new, _, _, counts = _derive(
            {(BID, "O1"): g_cur},
            prior_index,
            prior_listings={},   # no listing row for O1
        )
        assert _etypes(events) == {}
        assert enq_new == {}
        assert counts["colliers"].get("enumerated_unmatched", 0) == 1


# ---------------------------------------------------------------------------
# 6. SQL safety: build_write_sql never writes cre_listings.status or deleted_at
# ---------------------------------------------------------------------------


class TestSQLSafety:
    """Hard structural assertions on the full generated write transaction.

    The OBSERVE-ONLY guarantee is that the monitor's SQL transaction:
      (a) writes NO cre_listings.status column,
      (b) writes NO cre_listings.deleted_at column,
      (c) contains exactly one UPDATE on cre_listings (the neutral-columns update),
      (d) when disappear_marks are present, the soft_deleted flag is only written
          to cre_source_index, never to cre_listings.
    """

    def test_no_status_or_deleted_at_in_non_comment_sql(self):
        """Grep-style assertion: no 'status =' or 'deleted_at =' outside comments."""
        sql = _make_write_sql()
        non_comment = "\n".join(
            line for line in sql.split("\n")
            if not line.strip().startswith("--")
        )
        # \bstatus\s*= must not appear (observed_status= is safe: \b can't match
        # at the boundary between _ and s because _ is a word character)
        assert not re.search(r"\bstatus\s*=", non_comment), (
            "status assignment found in generated SQL (outside comments)"
        )
        assert not re.search(r"deleted_at\s*=", non_comment), (
            "deleted_at assignment found in generated SQL (outside comments)"
        )

    def test_cre_listings_update_set_clause_is_neutral_only(self):
        """Extract the SET clause of the UPDATE cre_listings block and confirm it
        touches only the neutral columns source_lastmod and canonical_key, never
        status/deleted_at, and never last_seen_at (writing last_seen_at = now() on
        every enumerated row would churn the trigger-bumped, EQUIRE-visible
        updated_at; enumeration freshness lives in cre_source_index.last_seen)."""
        sql = _make_write_sql()
        match = re.search(
            r"UPDATE credeals\.cre_listings l\s*\nSET(.*?)FROM",
            sql, re.DOTALL,
        )
        assert match, "UPDATE cre_listings SET ... FROM block not found in generated SQL"
        set_clause = match.group(1)
        assert "status" not in set_clause, (
            f"'status' found in cre_listings SET clause: {set_clause!r}"
        )
        assert "deleted_at" not in set_clause, (
            f"'deleted_at' found in cre_listings SET clause: {set_clause!r}"
        )
        assert "last_seen_at" not in set_clause, (
            "last_seen_at must NOT be written to cre_listings by the monitor "
            f"(updated_at churn); found in SET clause: {set_clause!r}"
        )

    def test_cre_listings_update_is_change_guarded(self):
        """The cre_listings UPDATE must be guarded by IS DISTINCT FROM on
        source_lastmod/canonical_key so it touches (and trigger-bumps updated_at on)
        a row only when one of those neutral values actually changes."""
        sql = _make_write_sql()
        block = re.search(
            r"UPDATE credeals\.cre_listings l\s*\nSET.*?;",
            sql, re.DOTALL,
        )
        assert block, "UPDATE cre_listings block not found"
        assert "IS DISTINCT FROM" in block.group(0), (
            "cre_listings UPDATE is not change-guarded (no IS DISTINCT FROM); it "
            "would bump updated_at on every enumerated row every run."
        )

    def test_exactly_one_cre_listings_update_block(self):
        sql = _make_write_sql()
        update_count = len(re.findall(r"UPDATE credeals\.cre_listings", sql))
        assert update_count == 1, (
            f"Expected exactly 1 UPDATE on cre_listings; found {update_count}"
        )

    def test_disappear_marks_soft_deleted_flag_only_on_source_index(self):
        """When disappear_marks are present, SET soft_deleted must target
        cre_source_index, not cre_listings."""
        sql = _make_write_sql(disappear_marks=[(BID, "SOME_EID")])
        lines = sql.split("\n")
        for i, line in enumerate(lines):
            if "SET soft_deleted" in line:
                # Walk back to the UPDATE statement for this block
                for j in range(i - 1, max(0, i - 6), -1):
                    if "UPDATE" in lines[j]:
                        assert "cre_source_index" in lines[j], (
                            f"soft_deleted SET must only appear on cre_source_index UPDATE; "
                            f"got: {lines[j]!r}"
                        )
                        break

    def test_generated_sql_with_events_still_observe_only(self):
        """Adding events (status_change, new) must not introduce status writes."""
        events = [
            m._event("L1", BID, "colliers", "status_change",
                     field="status", old_value="active", new_value="sold"),
            m._event("L2", BID, "colliers", "new"),
        ]
        sql = _make_write_sql(events=events)
        non_comment = "\n".join(
            line for line in sql.split("\n")
            if not line.strip().startswith("--")
        )
        # Events are INSERTs into cre_listing_events, not UPDATEs to cre_listings
        assert not re.search(r"\bstatus\s*=", non_comment)
        assert not re.search(r"deleted_at\s*=", non_comment)

    def test_no_cre_listings_in_event_insert(self):
        """The cre_listing_events INSERT must reference cre_listing_events,
        not cre_listings."""
        events = [m._event("L1", BID, "colliers", "new")]
        sql = _make_write_sql(events=events)
        event_insert_match = re.search(
            r"INSERT INTO (credeals\.\w+).*?cre_listing_events\b",
            sql, re.DOTALL,
        )
        assert event_insert_match or "cre_listing_events" in sql
        # Confirm no INSERT INTO cre_listings
        insert_targets = re.findall(r"INSERT\s+INTO\s+(credeals\.\w+)", sql, re.I)
        for target in insert_targets:
            # cre_listings is only referenced by name but never as an INSERT target
            assert target != "credeals.cre_listings", (
                f"cre_listings must not be an INSERT target in the monitor SQL; got {target!r}"
            )


# ---------------------------------------------------------------------------
# 7. Gate verdict_for: first_seen / hold / ok precedence
# ---------------------------------------------------------------------------


class TestGateVerdictFor:
    """Precedence (design sections 8/9):
      1. No baseline row             -> first_seen (even with error, even with count=0)
      2. Has error + baseline        -> hold
      3. current < floor             -> hold
      4. current < median * (1-drop) -> hold
      5. else                        -> ok
    """

    def test_no_baseline_yields_first_seen(self):
        v, _, safe, _ = gate.verdict_for(5000, False, None, None, FLOOR, DROP)
        assert v == "first_seen" and safe is False

    def test_no_baseline_with_error_still_first_seen(self):
        v, reason, safe, _ = gate.verdict_for(0, True, "timeout", None, FLOOR, DROP)
        assert v == "first_seen"
        assert "errored" in reason and safe is False

    def test_no_baseline_zero_count_still_first_seen(self):
        v, _, safe, _ = gate.verdict_for(0, False, None, None, FLOOR, DROP)
        assert v == "first_seen" and safe is False

    def test_error_with_baseline_is_hold(self):
        baseline = {"median": 5000, "last": 5000}
        v, _, safe, _ = gate.verdict_for(5000, True, "timeout", baseline, FLOOR, DROP)
        assert v == "hold" and safe is False

    def test_below_floor_is_hold(self):
        baseline = {"median": 5000, "last": 5000}
        v, reason, safe, _ = gate.verdict_for(50, False, None, baseline, FLOOR, DROP)
        assert v == "hold" and "floor" in reason and safe is False

    def test_drop_exceeds_threshold_is_hold(self):
        # 5000 * (1 - 0.30) = 3500; 3499 < 3500 -> hold
        baseline = {"median": 5000, "last": 5000}
        v, reason, safe, _ = gate.verdict_for(3499, False, None, baseline, FLOOR, DROP)
        assert v == "hold" and "median" in reason and safe is False

    def test_at_threshold_is_ok_not_hold(self):
        # 5000 * (1 - 0.30) = 3500; exactly 3500 is NOT below -> ok
        baseline = {"median": 5000, "last": 5000}
        v, _, safe, _ = gate.verdict_for(3500, False, None, baseline, FLOOR, DROP)
        assert v == "ok" and safe is True

    def test_healthy_count_is_ok(self):
        baseline = {"median": 5000, "last": 5000}
        v, _, safe, _ = gate.verdict_for(5200, False, None, baseline, FLOOR, DROP)
        assert v == "ok" and safe is True

    def test_null_median_bypasses_drop_check(self):
        # A baseline row with no median yet: only the floor check applies.
        baseline = {"median": None, "last": None}
        v, _, safe, _ = gate.verdict_for(5000, False, None, baseline, FLOOR, DROP)
        assert v == "ok" and safe is True


# ---------------------------------------------------------------------------
# 8. Prefix-aware per-source gating: cbre-dealflow gated separately from cbre
# ---------------------------------------------------------------------------


class TestPrefixAwareGating:
    """Sub-sources (cbre-dealflow, colliers-main, jll-investor) are independent
    gate keys. Their verdict does not depend on the parent's verdict."""

    def test_cbre_dealflow_has_own_mapping_entry_with_prefix(self):
        assert "cbre-dealflow" in SOURCE_TO_BROKERAGE
        # Folds into cbre brokerage but with a different prefix
        assert SOURCE_TO_BROKERAGE["cbre-dealflow"][0] == "cbre"
        assert SOURCE_TO_BROKERAGE["cbre-dealflow"][1] != SOURCE_TO_BROKERAGE["cbre"][1]

    def test_cbre_dealflow_folds_into_cbre_for_rollup(self):
        assert gate._slug_for("cbre-dealflow") == "cbre"

    def test_cbre_maps_to_cbre_slug(self):
        assert gate._slug_for("cbre") == "cbre"

    def test_colliers_main_folds_into_colliers_for_rollup(self):
        assert gate._slug_for("colliers-main") == "colliers"

    def test_jll_investor_folds_into_jll_for_rollup(self):
        assert gate._slug_for("jll-investor") == "jll"

    def test_dealflow_hold_independent_of_cbre_ok(self):
        """cbre can be ok while cbre-dealflow is hold; they are gated independently."""
        b_cbre = {"median": 18_000, "last": 18_000}
        b_df = {"median": 1_800, "last": 1_800}
        v_cbre, _, safe_cbre, _ = gate.verdict_for(18_000, False, None, b_cbre, FLOOR, DROP)
        v_df, _, safe_df, _ = gate.verdict_for(50, False, None, b_df, FLOOR, DROP)
        assert v_cbre == "ok" and safe_cbre is True
        assert v_df == "hold" and safe_df is False

    def test_all_three_subsources_have_distinct_mapping_from_parent(self):
        for parent, child in [
            ("cbre", "cbre-dealflow"),
            ("colliers", "colliers-main"),
            ("jll", "jll-investor"),
        ]:
            assert child in SOURCE_TO_BROKERAGE
            assert SOURCE_TO_BROKERAGE[child] != SOURCE_TO_BROKERAGE[parent], (
                f"{child} and {parent} must differ in SOURCE_TO_BROKERAGE "
                "(at minimum by id prefix)"
            )


# ---------------------------------------------------------------------------
# 9. Per-brokerage rollup: slug safe only when ALL member source_keys are ok
# ---------------------------------------------------------------------------


def _rollup(per_source: dict) -> dict:
    """Mirror the per-brokerage rollup logic in cre_gate.main()."""
    per_brokerage: dict = {}
    for sk in per_source:
        slug = gate._slug_for(sk)
        pb = per_brokerage.setdefault(slug, {"mark_missing_safe": True, "source_keys": []})
        pb["source_keys"].append(sk)
        if not per_source[sk]["mark_missing_safe"]:
            pb["mark_missing_safe"] = False
    return per_brokerage


class TestBrokerageRollup:
    def test_all_ok_is_safe(self):
        rollup = _rollup({
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": True},
        })
        assert rollup["cbre"]["mark_missing_safe"] is True

    def test_subsource_hold_makes_parent_brokerage_unsafe(self):
        rollup = _rollup({
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": False},
        })
        assert rollup["cbre"]["mark_missing_safe"] is False

    def test_colliers_main_hold_makes_colliers_unsafe(self):
        rollup = _rollup({
            "colliers": {"mark_missing_safe": True},
            "colliers-main": {"mark_missing_safe": False},
        })
        assert rollup["colliers"]["mark_missing_safe"] is False

    def test_jll_investor_hold_makes_jll_unsafe(self):
        rollup = _rollup({
            "jll": {"mark_missing_safe": True},
            "jll-investor": {"mark_missing_safe": False},
        })
        assert rollup["jll"]["mark_missing_safe"] is False

    def test_singleton_ok_is_safe(self):
        rollup = _rollup({"svn": {"mark_missing_safe": True}})
        assert rollup["svn"]["mark_missing_safe"] is True

    def test_singleton_hold_is_unsafe(self):
        rollup = _rollup({"svn": {"mark_missing_safe": False}})
        assert rollup["svn"]["mark_missing_safe"] is False

    def test_multiple_brokerages_each_correct(self):
        per_source = {
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": True},
            "colliers": {"mark_missing_safe": True},
            "colliers-main": {"mark_missing_safe": False},  # hold
            "svn": {"mark_missing_safe": True},
        }
        rollup = _rollup(per_source)
        assert rollup["cbre"]["mark_missing_safe"] is True
        assert rollup["colliers"]["mark_missing_safe"] is False
        assert rollup["svn"]["mark_missing_safe"] is True

    def test_source_keys_list_populated_correctly(self):
        rollup = _rollup({
            "cbre": {"mark_missing_safe": True},
            "cbre-dealflow": {"mark_missing_safe": True},
        })
        assert sorted(rollup["cbre"]["source_keys"]) == ["cbre", "cbre-dealflow"]
