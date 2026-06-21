"""
test_cre_ingest_history.py

Pure-transform coverage for the parts of cre_ingest.py reachable only by:
  1. calling build_sql() DIRECTLY with a non-empty mark_missing_slugs set in
     BOTH guard modes (the existing mark-missing tests shell out via subprocess,
     so build_sql's mark-missing / archive branches are not counted by the
     parent-process coverage; an in-process call covers lines 1689-1798), and
  2. exercising the status-helper internals (_dig / _status_from_signal /
     _explicit_status_from_pass / _text_status_from_pass / norm_status) and the
     COPY-CSV read-back decoder (_raise_csv_field_limit / parse_copy_csv_json),
     which the artifact-shape suite does not reach.

No network, no live DB: build_sql emits a SQL string; the read-back decoder is
fed synthetic CSV. Assertions grep generated SQL non-comment text for the
invariants (never re-implement the builder).

Invariant under test (tests/CLAUDE.md): the ingest SQL only ever assigns
status / deleted_at inside the guarded mark-missing block (and the documented
revival/activation CASEs), never as a blind column write.
"""

from datetime import datetime, timezone

import pytest

import cre_ingest as ci

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()


# ===========================================================================
# build_sql with mark_missing_slugs, guarded form (history_guard=True)
# Covers the _retired capture, disappeared event, and 009/011/013 archive
# guards (line 1688-1796) in-process.
# ===========================================================================


def _guarded_mm_sql():
    return ci.build_sql([], [], _SCRAPED_AT, {"svn"}, history_guard=True)


def test_mm_creates_retired_capture_before_update():
    sql = _guarded_mm_sql()
    assert "CREATE TEMP TABLE _retired" in sql
    # _retired must be captured before the soft-delete UPDATE overwrites status.
    assert sql.index("CREATE TEMP TABLE _retired") < sql.index(
        "UPDATE credeals.cre_listings l\nSET deleted_at = now()"
    )


def test_mm_soft_delete_sets_inactive_and_deleted_at():
    sql = _guarded_mm_sql()
    assert "SET deleted_at = now(), status = 'inactive', updated_at = now()" in sql


def test_mm_emits_disappeared_event_with_prior_status():
    sql = _guarded_mm_sql()
    assert "INSERT INTO credeals.cre_listing_events" in sql
    assert "'disappeared', 'status', r.prior_status, 'inactive'" in sql
    assert "'mark_missing'" in sql


def test_mm_slug_literal_is_quote_escaped():
    sql = ci.build_sql([], [], _SCRAPED_AT, {"o'brien"}, history_guard=True)
    # sorted-and-escaped via the inline replace("'", "''").
    assert "'o''brien'" in sql


def test_mm_multiple_slugs_sorted_in_literal_list():
    sql = ci.build_sql([], [], _SCRAPED_AT, {"svn", "cbre", "jll"}, history_guard=True)
    block = sql[sql.index("CREATE TEMP TABLE _retired"):]
    i_cbre, i_jll, i_svn = block.index("'cbre'"), block.index("'jll'"), block.index("'svn'")
    assert i_cbre < i_jll < i_svn  # sorted()


def test_mm_guarded_contacts_documents_archives_have_regclass_guard():
    sql = _guarded_mm_sql()
    assert "to_regclass('credeals.cre_listing_contacts_archive')" in sql
    assert "to_regclass('credeals.cre_listing_documents_archive')" in sql
    assert "INSERT INTO credeals.cre_listing_contacts_archive" in sql
    assert "INSERT INTO credeals.cre_listing_documents_archive" in sql


def test_mm_media_links_om_archives_always_guarded():
    sql = _guarded_mm_sql()
    # 011/013 archives are always to_regclass-guarded (may be unapplied).
    assert "to_regclass('credeals.cre_listing_media_archive')" in sql
    assert "to_regclass('credeals.cre_listing_links_archive')" in sql
    assert "to_regclass('credeals.cre_listing_om_facts_archive')" in sql


def test_mm_images_never_archived():
    sql = _guarded_mm_sql()
    assert "images_archive" not in sql


# ===========================================================================
# build_sql with mark_missing_slugs, dry-run form (history_guard=False)
# Covers the unguarded contacts/documents archive emission (line 1797-1804).
# ===========================================================================


def _dry_mm_sql():
    return ci.build_sql([], [], _SCRAPED_AT, {"svn"}, history_guard=False)


def test_dry_mm_contacts_documents_archives_unguarded():
    sql = _dry_mm_sql()
    assert "INSERT INTO credeals.cre_listing_contacts_archive" in sql
    assert "INSERT INTO credeals.cre_listing_documents_archive" in sql
    # The 009 contacts/documents archives lose the to_regclass guard in dry-run.
    assert "to_regclass('credeals.cre_listing_contacts_archive')" not in sql
    assert "to_regclass('credeals.cre_listing_documents_archive')" not in sql


def test_dry_mm_media_links_om_archives_still_guarded():
    sql = _dry_mm_sql()
    # 011/013 archives keep their guard even in dry-run (they may be unapplied).
    assert "to_regclass('credeals.cre_listing_media_archive')" in sql
    assert "to_regclass('credeals.cre_listing_links_archive')" in sql
    assert "to_regclass('credeals.cre_listing_om_facts_archive')" in sql


def test_no_mark_missing_block_when_slugs_empty():
    sql = ci.build_sql([], [], _SCRAPED_AT, set(), history_guard=True)
    assert "CREATE TEMP TABLE _retired" not in sql
    assert "INSERT INTO credeals.cre_listing_contacts_archive" not in sql


# ===========================================================================
# build_sql safety invariant: no blind status / deleted_at writes.
# status/deleted_at must only be assigned inside the documented CASEs and the
# guarded mark-missing block (mirrors the tests/CLAUDE.md observe-only rail for
# the monitor/gate builders, applied here to the ingest mark-missing path).
# ===========================================================================


def test_no_blind_status_assignment_without_mark_missing():
    sql = ci.build_sql([], [], _SCRAPED_AT, set(), history_guard=True)
    # The only status writes are the revival CASE (ON CONFLICT), the targeted
    # activation UPDATE (SET status = s.status), and -- under mark-missing only --
    # the soft-delete. With no mark-missing block, the soft-delete write form
    # (SET deleted_at = now(), status = 'inactive') must be absent. Note the
    # revival CASE still *compares* `t.status = 'inactive'`; we assert on the
    # assignment form, not the comparison.
    assert "status = 'inactive', updated_at = now()" not in sql
    # deleted_at is only NULL'd (resurrect) outside mark-missing, never now()'d.
    assert "deleted_at = now()" not in sql


def test_deleted_at_now_only_present_with_mark_missing():
    assert "deleted_at = now()" in _guarded_mm_sql()


# ===========================================================================
# build_sql COPY emission for staged rows (line 1206-1209, 1216-1217)
# A single staged row drives the _stage and _jobmeta COPY bodies.
# ===========================================================================


def test_build_sql_emits_copy_body_for_staged_rows():
    row = ci.to_row(
        {"sourceKey": "cbre", "url": "https://cbre.com/p", "id": "row1", "city": "Dallas"},
        {}, _SCRAPED_AT,
    )
    row.pop("_modes", None)
    job_meta = [{"slug": "cbre", "discovered": 1, "saved": 1, "errors": 0, "notes": None}]
    sql = ci.build_sql([row], job_meta, _SCRAPED_AT, set(), history_guard=True)
    assert "COPY _stage" in sql
    assert "row1" in sql          # external_id appears in the COPY data line
    assert "Dallas" in sql        # a scalar column value appears
    assert "COPY _jobmeta" in sql


# ===========================================================================
# Status helper internals (line 532-607) not reached by the canonical suite.
# ===========================================================================


def test_dig_resolves_and_stops_on_non_dict_hop():
    assert ci._dig({"a": {"b": {"c": 7}}}, "a.b.c") == 7
    assert ci._dig({"a": {"b": 1}}, "a.b.c") is None   # 1 is not a dict
    assert ci._dig({"a": 5}, "a.b") is None             # mid-path non-dict
    assert ci._dig("notadict", "a") is None


def test_status_from_signal_none_bool_and_coercion():
    assert ci._status_from_signal(None) is None
    assert ci._status_from_signal(True) is None        # bool short-circuit
    assert ci._status_from_signal("   ") is None        # empty after strip
    assert ci._status_from_signal("Now Sold") == "sold"


def test_explicit_status_from_pass_non_dict_and_bool_paths():
    assert ci._explicit_status_from_pass(None, ["status"]) is None
    # boolean True at a path not in _STATUS_BOOL_PATHS -> no token -> no status.
    assert ci.norm_status({"sourceKey": "colliers", "status": True}) is None
    # boolean False at a mapped bool path contributes no signal.
    assert ci.norm_status({"sourceKey": "svn", "closed": False}) is None
    # boolean True at a mapped bool path yields the canonical token.
    assert ci.norm_status({"sourceKey": "svn", "underContract": True}) == "under_contract"


def test_text_status_from_pass_non_dict_and_slug():
    assert ci._text_status_from_pass(None) is None
    # url-slug fallback fires when scoped text fields carry nothing.
    assert ci.norm_status({"sourceKey": "cbre",
                           "url": "https://x.com/a/b-leased"}) == "leased"


def test_norm_status_non_dict_input():
    assert ci.norm_status("x") is None
    assert ci.norm_status(None) is None


def test_norm_status_numeric_status_value_coerced_to_none():
    # A numeric status flows through str() in _status_from_signal but matches no
    # STATUS_RULES, so it yields None (never 'active').
    assert ci.norm_status({"sourceKey": "colliers", "status": 12345}) is None


def test_norm_status_dual_pass_terminal_from_secondary_wins():
    listing = {
        "sourceKey": "cushman-wakefield",
        "primary": {"listingStatus": "Available"},
        "secondary_pass": {"listingStatus": "Sold"},
    }
    assert ci.norm_status(listing) == "sold"


# ===========================================================================
# _flip_circuit_breaker min_base parse fallback (line 1121-1125)
# ===========================================================================


def test_flip_breaker_min_base_garbage_falls_back_to_default(monkeypatch):
    # A valid fraction with a non-int CRE_STATUS_FLIP_MIN_BASE falls back to the
    # 200 default (the except ValueError branch), never raising.
    monkeypatch.setenv("CRE_STATUS_FLIP_MAX_FRACTION", "0.5")
    monkeypatch.setenv("CRE_STATUS_FLIP_MIN_BASE", "garbage")
    assert ci._flip_circuit_breaker() == (0.5, 200)


def test_flip_breaker_min_base_floored_at_one(monkeypatch):
    # max(1, min_base) guards a zero/negative min_base.
    monkeypatch.setenv("CRE_STATUS_FLIP_MAX_FRACTION", "0.3")
    monkeypatch.setenv("CRE_STATUS_FLIP_MIN_BASE", "0")
    assert ci._flip_circuit_breaker() == (0.3, 1)


# ===========================================================================
# COPY-CSV read-back decoder (line 1901-1941) -- pure, synthetic CSV only.
# ===========================================================================


def test_raise_csv_field_limit_does_not_raise():
    # Idempotent; simply must not raise on this platform.
    ci._raise_csv_field_limit()


def test_csv_cells_strips_and_skips_blank():
    cells = list(ci._csv_cells('"a"\n\n"  "\n"b"\n'))
    assert cells == ["a", "b"]


def test_parse_copy_csv_json_round_trips_quoted_json():
    # COPY CSV doubles embedded double-quotes; csv.reader unquotes them.
    out = list(ci.parse_copy_csv_json('"{""k"": ""val""}"\n'))
    assert out == [{"k": "val"}]


def test_parse_copy_csv_json_round_trips_backslash_value():
    # A JSON value with a backslash escape must survive intact (the reason the
    # read-back uses CSV, not text, COPY format).
    out = list(ci.parse_copy_csv_json('"{""path"": ""C:\\\\x""}"\n'))
    assert out == [{"path": "C:\\x"}]


def test_parse_copy_csv_json_raises_on_undecodable_row():
    with pytest.raises(ValueError) as exc:
        list(ci.parse_copy_csv_json('"{not valid json"\n', label="probe"))
    assert "probe" in str(exc.value)
    assert "undecodable" in str(exc.value)


def test_parse_copy_csv_json_handles_large_field():
    # A field over the default 131072-byte csv limit must decode after the limit
    # is raised inside the decoder.
    big = '{"k": "' + ("x" * 200000) + '"}'
    csv_line = '"' + big.replace('"', '""') + '"\n'
    out = list(ci.parse_copy_csv_json(csv_line))
    assert out[0]["k"] == "x" * 200000
