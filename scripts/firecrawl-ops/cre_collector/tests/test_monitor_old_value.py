"""test_monitor_old_value.py

Offline tests for H4b-populate in cre_monitor.py:
  - persist current price into cre_source_index.prior_sale_price /
    prior_lease_rate / prior_status on each enumeration write, so the
    NEXT run can read a real before-value when emitting a price_change event.
  - populate old_value on price_change events from the prior persisted value
    instead of NULL when a prior price is available.

No DB or network connections. Pure Python and SQL-string assertions.

Helpers _g and _index_entry are replicated locally (do not import from
test_monitor_events.py) and extended with the three new prior_* keys.
"""

import cre_monitor as m

RUN = "00000000-0000-0000-0000-000000000002"
BID = "22222222-2222-2222-2222-222222222222"

STARTED_AT = "2026-06-15T00:00:00Z"


# ---------------------------------------------------------------------------
# Local helpers (replicated from test_monitor_events.py + extended)
# ---------------------------------------------------------------------------


def _g(eid, source_key="colliers", status=None, sale_price_usd=None,
       lease_rate_min=None, sale_price_text=None, url=None):
    """A finalized group as derive_events expects it, extended with lease fields."""
    return {
        "slug": "colliers",
        "external_id": eid,
        "source_key": source_key,
        "url": url or f"https://example.com/{eid}",
        "norm_status": status,
        "raw_status": status,
        "sale_price_usd": sale_price_usd,
        "lease_rate_min": lease_rate_min,
        "lease_rate_max": None,
        "sale_price_text": sale_price_text,
        "lease_rate_text": None,
        "source_lastmod": None,
        "canonical_key": None,
        "fingerprint": m.compute_fingerprint(
            status, sale_price_usd, sale_price_text,
            lease_rate_min, None, None,
        ),
    }


def _index_entry(g, soft_deleted=False, observed_status=None,
                 prior_sale_price=None, prior_lease_rate=None, prior_status=None):
    """Prior index entry extended with the three H4b columns.

    Use .get with default None everywhere in derive_events so that an entry
    without these keys (pre-H4b callers in test_monitor_events.py) is
    handled transparently and old_value stays None.
    """
    return {
        "fingerprint": g["fingerprint"],
        "soft_deleted": soft_deleted,
        "observed_status": observed_status,
        "source_key": g["source_key"],
        "url": g["url"],
        "prior_sale_price": prior_sale_price,
        "prior_lease_rate": prior_lease_rate,
        "prior_status": prior_status,
    }


def _derive(current, prior_index, prior_listings, soft_canon=None,
            baseline=None, coverage=None):
    run_keys = {g["source_key"] for g in current.values()}
    if baseline is None:
        prior_count = {}
        for p in prior_index.values():
            prior_count[p["source_key"] or ""] = prior_count.get(
                p["source_key"] or "", 0) + 1
        baseline = {sk for sk in run_keys if prior_count.get(sk, 0) == 0}
    if coverage is None:
        coverage = {sk: True for sk in run_keys}
    return m.derive_events(
        current, prior_index, prior_listings, soft_canon or {},
        run_keys, baseline, coverage, RUN,
    )


# ---------------------------------------------------------------------------
# H4b: old_value populated from prior_sale_price on price_change
# ---------------------------------------------------------------------------


def test_price_change_old_value_from_prior_sale_price():
    """When prior_sale_price is set, a price_change event carries old_value
    as the string representation of that prior price."""
    prior_g = _g("500", status="sold", sale_price_usd=500000)
    cur_g = _g("500", status="sold", sale_price_usd=600000)
    # The fingerprint must differ (status unchanged, price moved).
    assert prior_g["fingerprint"] != cur_g["fingerprint"]

    prior_index = {
        (BID, "500"): _index_entry(
            prior_g,
            observed_status="sold",
            prior_sale_price=500000.0,
        )
    }
    prior_listings = {
        (BID, "500"): {"id": "L500", "status": "sold", "deleted": False}
    }
    events, _, _, _, _ = _derive({(BID, "500"): cur_g}, prior_index, prior_listings)

    price_events = [e for e in events if e["event_type"] == "price_change"]
    assert len(price_events) == 1
    e = price_events[0]
    assert e["field"] == "sale_price_usd"
    # Integer-valued float renders without decimal point.
    assert e["old_value"] == "500000", f"expected '500000', got {e['old_value']!r}"
    assert e["new_value"] == "600000"
    assert e["source_value"] == prior_g["fingerprint"]


def test_price_change_old_value_integer_float_renders_without_decimal():
    """A prior_sale_price that is an integer-valued float renders as an int string."""
    prior_g = _g("501", status=None, sale_price_usd=1000000)
    cur_g = _g("501", status=None, sale_price_usd=1100000)
    assert prior_g["fingerprint"] != cur_g["fingerprint"]

    prior_index = {
        (BID, "501"): _index_entry(
            prior_g,
            observed_status=None,
            prior_sale_price=1000000.0,
        )
    }
    prior_listings = {
        (BID, "501"): {"id": "L501", "status": "active", "deleted": False}
    }
    events, _, _, _, _ = _derive({(BID, "501"): cur_g}, prior_index, prior_listings)
    price_events = [e for e in events if e["event_type"] == "price_change"]
    assert len(price_events) == 1
    assert price_events[0]["old_value"] == "1000000"


def test_price_change_old_value_from_prior_lease_rate_fallback():
    """When prior_sale_price is None, old_value falls back to prior_lease_rate.
    An integer-valued lease rate (25.0) renders as '25' (no decimal).
    """
    prior_g = _g("502", status=None, lease_rate_min=25.0)
    cur_g = _g("502", status=None, lease_rate_min=30.0)
    assert prior_g["fingerprint"] != cur_g["fingerprint"]

    prior_index = {
        (BID, "502"): _index_entry(
            prior_g,
            observed_status=None,
            prior_sale_price=None,
            prior_lease_rate=25.0,
        )
    }
    prior_listings = {
        (BID, "502"): {"id": "L502", "status": "active", "deleted": False}
    }
    events, _, _, _, _ = _derive({(BID, "502"): cur_g}, prior_index, prior_listings)
    price_events = [e for e in events if e["event_type"] == "price_change"]
    assert len(price_events) == 1
    e = price_events[0]
    assert e["old_value"] == "25", f"expected '25', got {e['old_value']!r}"


def test_price_change_old_value_non_integer_lease_rate_keeps_decimal():
    """A non-integer prior_lease_rate (25.5) renders with its decimal portion."""
    prior_g = _g("503", status=None, lease_rate_min=25.5)
    cur_g = _g("503", status=None, lease_rate_min=30.0)
    assert prior_g["fingerprint"] != cur_g["fingerprint"]

    prior_index = {
        (BID, "503"): _index_entry(
            prior_g,
            observed_status=None,
            prior_sale_price=None,
            prior_lease_rate=25.5,
        )
    }
    prior_listings = {
        (BID, "503"): {"id": "L503", "status": "active", "deleted": False}
    }
    events, _, _, _, _ = _derive({(BID, "503"): cur_g}, prior_index, prior_listings)
    price_events = [e for e in events if e["event_type"] == "price_change"]
    assert len(price_events) == 1
    assert price_events[0]["old_value"] == "25.5"


def test_price_change_old_value_none_when_no_prior_price():
    """When both prior_sale_price and prior_lease_rate are None (no prior
    persisted yet), old_value is None: the pre-H4b behavior is preserved."""
    prior_g = _g("504", status="sold", sale_price_usd=500000)
    cur_g = _g("504", status="sold", sale_price_usd=600000)
    assert prior_g["fingerprint"] != cur_g["fingerprint"]

    # Neither prior price key is set (entry from before H4b columns existed).
    prior_index = {
        (BID, "504"): _index_entry(
            prior_g,
            observed_status="sold",
            prior_sale_price=None,
            prior_lease_rate=None,
        )
    }
    prior_listings = {
        (BID, "504"): {"id": "L504", "status": "sold", "deleted": False}
    }
    events, _, _, _, _ = _derive({(BID, "504"): cur_g}, prior_index, prior_listings)
    price_events = [e for e in events if e["event_type"] == "price_change"]
    assert len(price_events) == 1
    assert price_events[0]["old_value"] is None


def test_price_change_old_value_none_when_prior_keys_missing():
    """An index entry without the H4b keys at all (pre-migration callers that
    use the old _index_entry from test_monitor_events.py without adding
    prior_sale_price / prior_lease_rate) must still yield old_value=None via
    .get() with default None, not raise a KeyError."""
    prior_g = _g("505", status="sold", sale_price_usd=500000)
    cur_g = _g("505", status="sold", sale_price_usd=700000)
    assert prior_g["fingerprint"] != cur_g["fingerprint"]

    # Build entry without the new keys (simulates a pre-H4b prior_index entry).
    prior_index_entry_no_h4b = {
        "fingerprint": prior_g["fingerprint"],
        "soft_deleted": False,
        "observed_status": "sold",
        "source_key": "colliers",
        "url": prior_g["url"],
        # prior_sale_price, prior_lease_rate, prior_status intentionally absent
    }
    prior_index = {(BID, "505"): prior_index_entry_no_h4b}
    prior_listings = {
        (BID, "505"): {"id": "L505", "status": "sold", "deleted": False}
    }
    events, _, _, _, _ = _derive({(BID, "505"): cur_g}, prior_index, prior_listings)
    price_events = [e for e in events if e["event_type"] == "price_change"]
    assert len(price_events) == 1
    # Must not raise; old_value falls back to None gracefully.
    assert price_events[0]["old_value"] is None


# ---------------------------------------------------------------------------
# H4b: build_write_sql persists prior columns into cre_source_index
# ---------------------------------------------------------------------------


def test_build_write_sql_includes_prior_columns_in_insert():
    """The cre_source_index INSERT must include prior_sale_price, prior_lease_rate,
    and prior_status in its column list so that THIS run's prices become the NEXT
    run's prior values."""
    g = _g("600", status="active", sale_price_usd=750000)
    sql = m.build_write_sql(
        [g], [], {}, {}, [], RUN, STARTED_AT, "test prior columns", ["colliers"]
    )
    assert "prior_sale_price" in sql, "expected prior_sale_price in generated SQL"
    assert "prior_lease_rate" in sql, "expected prior_lease_rate in generated SQL"
    assert "prior_status" in sql, "expected prior_status in generated SQL"


def test_build_write_sql_prior_columns_in_insert_column_list():
    """prior_sale_price / prior_lease_rate / prior_status appear in the INSERT
    column list of the cre_source_index INSERT statement."""
    g = _g("601", status="active", sale_price_usd=500000)
    sql = m.build_write_sql(
        [g], [], {}, {}, [], RUN, STARTED_AT, "test insert cols", ["colliers"]
    )
    # Find the INSERT INTO credeals.cre_source_index block and check its
    # column list. The three columns must all appear in the INSERT preamble.
    insert_idx = sql.index("INSERT INTO credeals.cre_source_index")
    on_conflict_idx = sql.index("ON CONFLICT (brokerage_id, external_id)", insert_idx)
    insert_block = sql[insert_idx:on_conflict_idx]
    assert "prior_sale_price" in insert_block
    assert "prior_lease_rate" in insert_block
    assert "prior_status" in insert_block


def test_build_write_sql_prior_columns_in_do_update_set():
    """prior_sale_price / prior_lease_rate / prior_status appear in the
    ON CONFLICT ... DO UPDATE SET clause so subsequent runs overwrite them."""
    g = _g("602", status="active", sale_price_usd=500000)
    sql = m.build_write_sql(
        [g], [], {}, {}, [], RUN, STARTED_AT, "test do update cols", ["colliers"]
    )
    on_conflict_idx = sql.index("ON CONFLICT (brokerage_id, external_id)")
    do_update_block = sql[on_conflict_idx:]
    assert "prior_sale_price" in do_update_block
    assert "prior_lease_rate" in do_update_block
    assert "prior_status" in do_update_block


# ---------------------------------------------------------------------------
# Observe-only invariant: exactly one cre_listings UPDATE, no status/deleted_at
# ---------------------------------------------------------------------------


def test_build_write_sql_observe_only_exactly_one_listings_update():
    """The generated SQL must contain exactly one UPDATE credeals.cre_listings
    block (the neutral-column update). No status or deleted_at assignment."""
    g = _g("700", status="sold", sale_price_usd=900000)
    sql = m.build_write_sql(
        [g], [], {}, {}, [], RUN, STARTED_AT,
        "observe-only check (owner C re-assert)", ["colliers"]
    )
    update_blocks = [b for b in sql.split("\n\n") if "UPDATE credeals.cre_listings" in b]
    assert len(update_blocks) == 1, (
        f"expected exactly 1 UPDATE credeals.cre_listings block, found {len(update_blocks)}"
    )
    for line in sql.split("\n"):
        if line.lstrip().startswith("--"):
            continue
        assert "deleted_at" not in line, (
            f"deleted_at must not appear in a non-comment line: {line!r}"
        )
        if "UPDATE credeals.cre_listings" in line or line.strip().startswith("SET "):
            assert "status" not in line, (
                f"status must not appear in a cre_listings SET line: {line!r}"
            )
