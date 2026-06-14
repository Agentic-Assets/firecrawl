"""
test_ingest_status_activation.py

Phase-2 status activation in the production ingest upsert (T3.1, 2026-06-13).

Covers the additive wiring of status / source_lastmod / canonical_key into
to_row() + build_sql():

  - to_row() now stages a native-signal status (None when no opinion), a
    full-precision source_lastmod, and the advisory canonical_key.
  - merge_rows() resolves status across the sale+lease passes with
    drop-terminal-wins (sold/leased/off_market beats under_contract/pending).
  - build_sql() inserts COALESCE(status,'active'), keeps existing status
    sticky on update (resetting only resurrected rows to 'active'), and runs a
    separate targeted UPDATE that only ever upgrades a row to a real signal
    (Choice a COALESCE: a no-signal pass can never downgrade).

Pure transform, no network / DB. Design: cre-phase2-board-impact-2026-06-13.md.
"""

from datetime import datetime, timezone

from cre_ingest import build_sql, merge_rows, to_row

_SCRAPED_AT = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _row(listing):
    return to_row(listing, {}, _SCRAPED_AT)


def _cushman(status_text, *, url="https://www.cushmanwakefield.com/p/1", _id="1",
             tx="sale", last="2026-03-18"):
    return {
        "sourceKey": "cushman-wakefield",
        "url": url,
        "id": _id,
        "transactionMode": tx,
        "listingStatus": status_text,
        "lastUpdated": last,
        "street": "100 Main St",
        "state": "TX",
    }


# --- to_row field population ------------------------------------------------

def test_to_row_populates_native_status():
    row = _row(_cushman("Under Contract"))
    assert row["status"] == "under_contract"


def test_to_row_status_none_when_no_signal():
    # cbre is a disappearance-only source (empty STATUS_SOURCE_PATHS) and the
    # card carries no terminal text, so norm_status must yield None, never active.
    row = _row({
        "sourceKey": "cbre",
        "url": "https://www.cbre.com/p/abc",
        "id": "K1",
        "transactionMode": "sale",
    })
    assert row["status"] is None


def test_to_row_source_lastmod_full_precision():
    row = _row(_cushman("Sold", last="2026-03-18T14:23:05Z"))
    assert row["source_lastmod"] == "2026-03-18T14:23:05+00:00"


def test_to_row_canonical_key_address_state():
    row = _row(_cushman("Sold"))
    assert row["canonical_key"] == "100 main st|tx"


# --- merge_rows status resolution ------------------------------------------

def test_merge_drop_terminal_beats_transitional():
    a = _row(_cushman("Under Contract", tx="sale"))
    b = _row(_cushman("Sold", tx="lease"))
    assert merge_rows(a, b)["status"] == "sold"


def test_merge_drop_terminal_when_first_pass_terminal():
    a = _row(_cushman("Sold", tx="sale"))
    b = _row(_cushman("Under Contract", tx="lease"))
    assert merge_rows(a, b)["status"] == "sold"


def test_merge_fills_status_from_other_pass_when_none():
    a = _row({
        "sourceKey": "cbre",
        "url": "https://www.cbre.com/p/abc",
        "id": "K1",
        "transactionMode": "sale",
    })
    b = _row(_cushman("Pending", tx="lease"))
    assert a["status"] is None
    assert merge_rows(a, b)["status"] == "pending"


# --- build_sql template (Choice a COALESCE) --------------------------------

def _sql():
    return build_sql([], [], _SCRAPED_AT, set())


def test_insert_defaults_status_active_via_coalesce():
    assert "COALESCE(status, 'active')" in _sql()


def test_update_keeps_status_sticky_resetting_only_resurrected():
    sql = _sql()
    assert "status            = CASE WHEN t.deleted_at IS NOT NULL THEN 'active' ELSE t.status END" in sql
    # The old unconditional downgrade must be gone.
    assert "status            = 'active'," not in sql


def test_targeted_upgrade_only_moves_to_real_signal():
    sql = _sql()
    assert "SET status = s.status" in sql
    assert "s.status IS NOT NULL" in sql
    assert "t.status IS DISTINCT FROM s.status" in sql


def test_neutral_columns_coalesced_in_upsert():
    sql = _sql()
    assert "COALESCE(EXCLUDED.source_lastmod, t.source_lastmod)" in sql
    assert "COALESCE(EXCLUDED.canonical_key, t.canonical_key)" in sql
    # staged + inserted
    assert "status text, source_lastmod timestamptz, canonical_key text" in sql
