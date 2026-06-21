"""test_monitor_events.py

Validator for the cre_monitor change-detection core (design doc sections 6, 7,
9, 12). The --dry-run path only ever exercises the BASELINE SEED branch (it
assumes empty prior state), so these tests drive cre_monitor.derive_events
directly with synthetic prior state to prove the real diff logic:

  - baseline-seed suppression (a first-ever source emits no events),
  - new / status_change / price_change / disappeared / reappeared / possible_relist,
  - the per-source coverage gate for disappearance,
  - status_change cross-run idempotency (no re-fire of an unchanged terminal status),
  - within-run event idempotency dedupe,
  - the enumeration-key / dual-shape transform (terminal-wins, group fingerprint).

OBSERVE-ONLY is structural: derive_events returns events plus enqueue/mark work
and never produces any cre_listings.status / deleted_at mutation. The SQL-side
guarantee is covered by inspecting build_write_sql output below.
"""

import cre_monitor as m

RUN = "00000000-0000-0000-0000-000000000001"
BID = "11111111-1111-1111-1111-111111111111"


def _g(eid, source_key="colliers", status=None, sale_price_usd=None,
       sale_price_text=None, canonical_key=None, url=None):
    """A finalized group as derive_events expects it."""
    return {
        "slug": "colliers",
        "external_id": eid,
        "source_key": source_key,
        "url": url or f"https://example.com/{eid}",
        "norm_status": status,
        "raw_status": status,
        "sale_price_usd": sale_price_usd,
        "lease_rate_min": None,
        "lease_rate_max": None,
        "sale_price_text": sale_price_text,
        "lease_rate_text": None,
        "source_lastmod": None,
        "canonical_key": canonical_key,
        "fingerprint": m.compute_fingerprint(
            status, sale_price_usd, sale_price_text, None, None, None
        ),
    }


def _index_entry(g, soft_deleted=False, observed_status=None):
    return {
        "fingerprint": g["fingerprint"],
        "soft_deleted": soft_deleted,
        "observed_status": observed_status,
        "source_key": g["source_key"],
        "url": g["url"],
    }


def _derive(current, prior_index, prior_listings, soft_canon=None,
            baseline=None, coverage=None, force=False):
    run_keys = {g["source_key"] for g in current.values()}
    if baseline is None:
        # Mirror main(): a source with zero prior index rows is a baseline seed.
        prior_count = {}
        for p in prior_index.values():
            prior_count[p["source_key"] or ""] = prior_count.get(p["source_key"] or "", 0) + 1
        baseline = {sk for sk in run_keys if prior_count.get(sk, 0) == 0}
    if coverage is None:
        coverage = {sk: True for sk in run_keys}
    return m.derive_events(
        current, prior_index, prior_listings, soft_canon or {},
        run_keys, baseline, coverage, RUN,
    )


def _types(events):
    out = {}
    for e in events:
        out[e["event_type"]] = out.get(e["event_type"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# Baseline seed
# ---------------------------------------------------------------------------


def test_baseline_seed_emits_no_events():
    g = _g("100", status="sold", sale_price_usd=500000)
    current = {(BID, "100"): g}
    events, enq_new, enq_changed, marks, counts = _derive(
        current, prior_index={}, prior_listings={(BID, "100"): {"id": "L100", "status": "active", "deleted": False}},
    )
    assert events == []
    assert enq_new == {} and enq_changed == {} and marks == []


# ---------------------------------------------------------------------------
# New + possible_relist
# ---------------------------------------------------------------------------


def test_new_requires_listing_row_else_unmatched():
    g = _g("200", status="sold")
    current = {(BID, "200"): g}
    # Prior index non-empty for the source (so not baseline) but this id is new.
    other = _g("999")
    prior_index = {(BID, "999"): _index_entry(other)}
    # No cre_listings row for 200 -> cannot satisfy FK -> no event, counted unmatched.
    events, enq_new, _, _, counts = _derive(current, prior_index, prior_listings={})
    assert _types(events) == {}
    assert counts["colliers"]["enumerated_unmatched"] == 1
    assert enq_new == {}


def test_new_with_listing_emits_new_and_enqueues():
    g = _g("200", status=None, sale_price_usd=750000)
    current = {(BID, "200"): g}
    prior_index = {(BID, "999"): _index_entry(_g("999"))}
    prior_listings = {(BID, "200"): {"id": "L200", "status": "active", "deleted": False}}
    events, enq_new, enq_changed, marks, _ = _derive(current, prior_index, prior_listings)
    assert _types(events) == {"new": 1}
    assert events[0]["listing_id"] == "L200"
    assert events[0]["field"] is None
    assert (BID, "colliers", "200") in enq_new


def test_possible_relist_links_soft_deleted_same_brokerage():
    g = _g("300", status=None, canonical_key="123 main st|tx|30.0001")
    current = {(BID, "300"): g}
    prior_index = {(BID, "999"): _index_entry(_g("999"))}
    prior_listings = {(BID, "300"): {"id": "L300", "status": "active", "deleted": False}}
    soft_canon = {(BID, "123 main st|tx|30.0001"): ["OLD_DEAD_ROW"]}
    events, _, _, _, _ = _derive(current, prior_index, prior_listings, soft_canon=soft_canon)
    assert _types(events) == {"new": 1, "possible_relist": 1}
    relist = [e for e in events if e["event_type"] == "possible_relist"][0]
    assert relist["field"] == "canonical_key"
    assert relist["new_value"] == "123 main st|tx|30.0001"
    assert relist["source_value"] == "OLD_DEAD_ROW"


# ---------------------------------------------------------------------------
# status_change
# ---------------------------------------------------------------------------


def test_status_change_fires_on_real_move():
    # Prior snapshot saw it as Available (norm None); now Sold.
    prior_g = _g("400", status=None)
    cur_g = _g("400", status="sold")
    prior_index = {(BID, "400"): _index_entry(prior_g, observed_status=None)}
    prior_listings = {(BID, "400"): {"id": "L400", "status": "active", "deleted": False}}
    events, _, enq_changed, _, _ = _derive({(BID, "400"): cur_g}, prior_index, prior_listings)
    assert _types(events) == {"status_change": 1}
    e = events[0]
    assert e["field"] == "status"
    assert e["old_value"] == "active"      # the live cre_listings.status
    assert e["new_value"] == "sold"
    assert (BID, "colliers", "400") in enq_changed


def test_status_change_does_not_refire_when_observed_status_unchanged():
    # cre_listings.status is still 'active' (monitor never updates it), but the
    # prior index already observed 'sold'. No re-fire: cross-run idempotency.
    cur_g = _g("400", status="sold")
    prior_index = {(BID, "400"): _index_entry(cur_g, observed_status="sold")}
    prior_listings = {(BID, "400"): {"id": "L400", "status": "active", "deleted": False}}
    events, _, _, _, _ = _derive({(BID, "400"): cur_g}, prior_index, prior_listings)
    assert _types(events) == {}


# ---------------------------------------------------------------------------
# price_change
# ---------------------------------------------------------------------------


def test_price_change_fires_when_only_price_moves():
    prior_g = _g("500", status="sold", sale_price_usd=500000)
    cur_g = _g("500", status="sold", sale_price_usd=600000)
    assert prior_g["fingerprint"] != cur_g["fingerprint"]
    prior_index = {(BID, "500"): _index_entry(prior_g, observed_status="sold")}
    prior_listings = {(BID, "500"): {"id": "L500", "status": "sold", "deleted": False}}
    events, _, enq_changed, _, _ = _derive({(BID, "500"): cur_g}, prior_index, prior_listings)
    assert _types(events) == {"price_change": 1}
    e = events[0]
    assert e["field"] == "sale_price_usd"
    assert e["old_value"] is None             # prior price not persisted (hash only)
    assert e["new_value"] == "600000"
    assert e["source_value"] == prior_g["fingerprint"]
    assert (BID, "colliers", "500") in enq_changed


def test_price_text_only_move_is_detected():
    # parse_money returns None for both, but the raw text changed -> fingerprint
    # moves -> price_change, with the text carried as evidence.
    prior_g = _g("510", status=None, sale_price_text="Negotiable")
    cur_g = _g("510", status=None, sale_price_text="Call for offers")
    prior_index = {(BID, "510"): _index_entry(prior_g, observed_status=None)}
    prior_listings = {(BID, "510"): {"id": "L510", "status": "active", "deleted": False}}
    events, _, _, _, _ = _derive({(BID, "510"): cur_g}, prior_index, prior_listings)
    assert _types(events) == {"price_change": 1}
    assert events[0]["new_value"] == "Call for offers"
    assert events[0]["sale_price_text"] == "Call for offers"


def test_simultaneous_status_and_price_move_is_status_change_only():
    # When both move, the status_change captures it; price_change is suppressed
    # because the price component cannot be isolated from a combined hash.
    prior_g = _g("520", status=None, sale_price_usd=500000)
    cur_g = _g("520", status="sold", sale_price_usd=600000)
    prior_index = {(BID, "520"): _index_entry(prior_g, observed_status=None)}
    prior_listings = {(BID, "520"): {"id": "L520", "status": "active", "deleted": False}}
    events, _, _, _, _ = _derive({(BID, "520"): cur_g}, prior_index, prior_listings)
    assert _types(events) == {"status_change": 1}


# ---------------------------------------------------------------------------
# disappeared (coverage-gated) + reappeared
# ---------------------------------------------------------------------------


def test_disappeared_emits_when_coverage_passes():
    # 'gone' is in prior index + cre_listings, absent from the current enumeration.
    present = _g("601", status=None)
    gone = _g("602", status=None)
    current = {(BID, "601"): present}
    prior_index = {
        (BID, "601"): _index_entry(present),
        (BID, "602"): _index_entry(gone),
    }
    prior_listings = {
        (BID, "601"): {"id": "L601", "status": "active", "deleted": False},
        (BID, "602"): {"id": "L602", "status": "active", "deleted": False},
    }
    events, _, _, marks, _ = _derive(
        current, prior_index, prior_listings, coverage={"colliers": True}
    )
    assert _types(events) == {"disappeared": 1}
    e = events[0]
    assert e["listing_id"] == "L602"
    assert e["source_value"] == "enumeration_gone"
    assert (BID, "602") in marks   # cre_source_index soft_deleted mark (monitor table only)


def test_disappeared_suppressed_when_coverage_fails():
    present = _g("601", status=None)
    gone = _g("602", status=None)
    current = {(BID, "601"): present}
    prior_index = {
        (BID, "601"): _index_entry(present),
        (BID, "602"): _index_entry(gone),
    }
    prior_listings = {
        (BID, "601"): {"id": "L601", "status": "active", "deleted": False},
        (BID, "602"): {"id": "L602", "status": "active", "deleted": False},
    }
    events, _, _, marks, _ = _derive(
        current, prior_index, prior_listings, coverage={"colliers": False}
    )
    assert _types(events) == {}
    assert marks == []


def test_disappeared_does_not_refire_when_already_soft_deleted():
    present = _g("601", status=None)
    gone = _g("602", status=None)
    current = {(BID, "601"): present}
    prior_index = {
        (BID, "601"): _index_entry(present),
        (BID, "602"): _index_entry(gone, soft_deleted=True),  # already recorded gone
    }
    prior_listings = {
        (BID, "601"): {"id": "L601", "status": "active", "deleted": False},
        (BID, "602"): {"id": "L602", "status": "active", "deleted": False},
    }
    events, _, _, marks, _ = _derive(current, prior_index, prior_listings)
    assert _types(events) == {}


def test_reappeared_when_soft_deleted_id_returns():
    g = _g("700", status=None)
    prior_index = {(BID, "700"): _index_entry(g, soft_deleted=True)}
    prior_listings = {(BID, "700"): {"id": "L700", "status": "active", "deleted": False}}
    events, _, _, _, _ = _derive({(BID, "700"): g}, prior_index, prior_listings)
    assert _types(events) == {"reappeared": 1}
    assert events[0]["listing_id"] == "L700"


def test_reappeared_not_fired_on_cre_listings_deleted_alone():
    """Cross-run idempotency guard: when the monitor's own snapshot shows the row
    PRESENT (soft_deleted=False) but cre_listings.deleted_at is set, REAPPEARED must
    NOT fire. The observe-only monitor cannot clear cre_listings.deleted_at, so
    keying off it would re-emit 'reappeared' on every run and flood the ledger.
    Reappearance fires only on the monitor-owned soft_deleted gone->present flip."""
    g = _g("710", status=None)
    prior_index = {(BID, "710"): _index_entry(g, soft_deleted=False)}
    prior_listings = {(BID, "710"): {"id": "L710", "status": "active", "deleted": True}}
    events, _, _, _, _ = _derive({(BID, "710"): g}, prior_index, prior_listings)
    assert "reappeared" not in _types(events), (
        f"reappeared must not fire on cre_listings.deleted_at alone; got {_types(events)}"
    )


# ---------------------------------------------------------------------------
# within-run dedupe + transform invariants
# ---------------------------------------------------------------------------


def test_within_run_event_dedupe():
    dupe = m._event("L1", BID, "colliers", "status_change",
                    field="status", old_value="active", new_value="sold")
    deduped = m._dedupe_events([dupe, dict(dupe)], RUN)
    assert len(deduped) == 1


def test_group_status_terminal_wins_across_flat_listings():
    # Two flat colliers listings for the same property: one Available (None), one
    # Sold (terminal). Terminal must win. norm_status is called on each flat
    # listing, never on a merged dual dict (dual-shape rule, design 12.5).
    flat = [
        {"sourceKey": "colliers", "status": "Available", "url": "https://x/1"},
        {"sourceKey": "colliers", "status": "Sold", "url": "https://x/1"},
    ]
    status, raw = m.group_status(flat)
    assert status == "sold"
    assert raw == "Sold"


def test_fingerprint_is_stable_and_32_hex():
    fp1 = m.compute_fingerprint("sold", 500000, "$500,000", None, None, None)
    fp2 = m.compute_fingerprint("sold", 500000, "$500,000", None, None, None)
    assert fp1 == fp2
    assert len(fp1) == 32
    assert all(c in "0123456789abcdef" for c in fp1)


def test_parse_source_lastmod_full_precision_not_truncated():
    # A date-only source renders at midnight (full timestamp, not harmfully
    # truncated); an intra-day timestamp keeps its time and offset.
    assert m.parse_source_lastmod("2026-03-18") == "2026-03-18T00:00:00"
    assert m.parse_source_lastmod("2026-03-18T14:23:05Z") == "2026-03-18T14:23:05+00:00"
    assert m.parse_source_lastmod("2026-03-18T14:23:05.500") == "2026-03-18T14:23:05.500000"
    assert m.parse_source_lastmod("not a date") is None
    assert m.parse_source_lastmod(None) is None


def test_build_write_sql_pins_standard_conforming_strings():
    # Scraped event free-text (sale_price_text, source_url, new_value, ...) is
    # inlined into INSERT literals via _sql_text -> sql_lit (quote-doubling).
    # That escaping is only injection-safe under standard_conforming_strings=on,
    # so the monitor's write transaction must pin the GUC itself, before the
    # first literal-bearing INSERT, rather than trust the server default.
    g = _g("810", status="sold", sale_price_usd=900000)
    sql = m.build_write_sql([g], [], {}, {}, [], RUN, "2026-06-13T00:00:00Z",
                            "monitor pin test", ["colliers"])
    assert "SET LOCAL standard_conforming_strings = on;" in sql
    set_idx = sql.index("standard_conforming_strings")
    assert sql.index("BEGIN;") < set_idx < sql.index("INSERT INTO credeals.cre_scrape_jobs")


def test_observe_only_generated_sql_has_no_listing_status_or_deleted_write():
    # End-to-end structural check on the write SQL: the only cre_listings write
    # is the neutral-column UPDATE; status / deleted_at are never assigned.
    g = _g("800", status="sold", sale_price_usd=900000)
    sql = m.build_write_sql([g], [], {}, {}, [], RUN, "2026-06-13T00:00:00Z",
                            "monitor observe-only test", ["colliers"])
    update_blocks = [b for b in sql.split("\n\n") if "UPDATE credeals.cre_listings" in b]
    assert len(update_blocks) == 1
    # 'status' / 'deleted_at' may appear only in a comment line, never in an
    # assignment, anywhere in the generated write SQL.
    for line in sql.split("\n"):
        if line.lstrip().startswith("--"):
            continue
        assert "deleted_at" not in line, line
        assert "cre_listings l\nSET status" not in sql
        if "UPDATE credeals.cre_listings" in line or line.strip().startswith("SET "):
            assert "status" not in line, line
