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
  - status activation is OPT-IN: apply_status_activation_gate() suppresses the
    source-derived status (default OFF) so an ingest refreshes listing data
    without flipping board state; _status_activation_enabled() reads
    --activate-status / CRE_ACTIVATE_STATUS.

Pure transform, no network / DB. Design: cre-phase2-board-impact-2026-06-13.md.
"""

from datetime import datetime, timezone

from cre_ingest import (
    _flip_circuit_breaker,
    _status_activation_enabled,
    apply_status_activation_gate,
    build_sql,
    merge_rows,
    to_row,
)

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
    # M5 (2026-06-15): revival resets to 'active' only when the prior status was
    # 'inactive' (the mark-missing soft-delete marker), so a real terminal that
    # flickers back into the feed keeps its terminal label.
    assert "status            = CASE WHEN t.deleted_at IS NOT NULL AND t.status = 'inactive'" in sql
    # The old unconditional revival form must be gone.
    assert "status            = CASE WHEN t.deleted_at IS NOT NULL THEN 'active' ELSE t.status END" not in sql
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


# --- SQL-literal injection invariant (security hardening) ---------------------

def test_build_sql_pins_standard_conforming_strings():
    # sql_lit() escapes a single quote by doubling it; that is provably
    # sufficient ONLY when standard_conforming_strings is ON (a backslash inside
    # a '...' literal is then literal, so a scraped value cannot break out via
    # \'). The generated transaction must pin the GUC itself rather than rely on
    # the server/role default, before any literal- or COPY-bearing statement.
    sql = _sql()
    assert "SET LOCAL standard_conforming_strings = on;" in sql
    set_idx = sql.index("standard_conforming_strings")
    assert sql.index("BEGIN;") < set_idx < sql.index("COPY _stage")


# --- terminal-stickiness guard (review LOW: code must match the prose) --------

def test_targeted_upgrade_never_downgrades_terminal_to_transitional():
    sql = _sql()
    # A sold/leased/off_market row is never overwritten by an under_contract /
    # pending re-signal: the targeted UPDATE carries the guard clause.
    assert "NOT (t.status IN ('sold','leased','off_market')" in sql
    assert "AND s.status IN ('under_contract','pending'))" in sql


# --- flip-rate circuit breaker (review HIGH: bound mass-flip blast radius) -----

def test_flip_breaker_helper_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("CRE_STATUS_FLIP_MAX_FRACTION", raising=False)
    assert _flip_circuit_breaker() is None


def test_flip_breaker_helper_rejects_out_of_range_and_garbage(monkeypatch):
    for bad in ("0", "1.5", "-0.2", "abc", ""):
        monkeypatch.setenv("CRE_STATUS_FLIP_MAX_FRACTION", bad)
        assert _flip_circuit_breaker() is None


def test_flip_breaker_helper_parses_fraction_and_min_base(monkeypatch):
    monkeypatch.setenv("CRE_STATUS_FLIP_MAX_FRACTION", "0.5")
    monkeypatch.setenv("CRE_STATUS_FLIP_MIN_BASE", "75")
    assert _flip_circuit_breaker() == (0.5, 75)


def test_flip_preflight_block_always_emitted_and_notice_only_by_default(monkeypatch):
    monkeypatch.delenv("CRE_STATUS_FLIP_MAX_FRACTION", raising=False)
    sql = _sql()
    # Observability block + GUC reads are always present...
    assert "status-flip pre-flight" in sql
    assert "current_setting('cre.flip_max_fraction', true)" in sql
    assert "RAISE NOTICE 'status-flip" in sql
    assert "circuit breaker tripped" in sql
    # ...but with the env unset, no GUC is set, so the breaker can never fire.
    assert "SET LOCAL cre.flip_max_fraction" not in sql


def test_flip_breaker_sets_guc_when_enabled(monkeypatch):
    monkeypatch.setenv("CRE_STATUS_FLIP_MAX_FRACTION", "0.5")
    monkeypatch.setenv("CRE_STATUS_FLIP_MIN_BASE", "200")
    sql = _sql()
    assert "SET LOCAL cre.flip_max_fraction = '0.5';" in sql
    assert "SET LOCAL cre.flip_min_base = '200';" in sql


# --- status activation opt-in gate (default OFF; board-state safety) ----------

def test_status_activation_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CRE_ACTIVATE_STATUS", raising=False)
    assert _status_activation_enabled() is False
    assert _status_activation_enabled(cli_flag=False) is False


def test_status_activation_enabled_by_cli_flag():
    assert _status_activation_enabled(cli_flag=True) is True


def test_status_activation_enabled_by_env(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("CRE_ACTIVATE_STATUS", val)
        assert _status_activation_enabled() is True


def test_status_activation_env_off_values(monkeypatch):
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("CRE_ACTIVATE_STATUS", val)
        assert _status_activation_enabled() is False


def test_gate_suppresses_status_when_disabled():
    rows = [
        _row(_cushman("Under Contract", _id="1", url="https://www.cushmanwakefield.com/p/1")),
        _row(_cushman("Sold", _id="2", url="https://www.cushmanwakefield.com/p/2")),
    ]
    assert [r["status"] for r in rows] == ["under_contract", "sold"]
    suppressed = apply_status_activation_gate(rows, activate_status=False)
    assert suppressed == 2
    assert all(r["status"] is None for r in rows)


def test_gate_preserves_status_when_enabled():
    rows = [_row(_cushman("Under Contract"))]
    suppressed = apply_status_activation_gate(rows, activate_status=True)
    assert suppressed == 0
    assert rows[0]["status"] == "under_contract"


def test_gate_no_signal_rows_untouched_when_disabled():
    rows = [_row({
        "sourceKey": "cbre",
        "url": "https://www.cbre.com/p/abc",
        "id": "K1",
        "transactionMode": "sale",
    })]
    assert rows[0]["status"] is None
    suppressed = apply_status_activation_gate(rows, activate_status=False)
    assert suppressed == 0
    assert rows[0]["status"] is None
