"""test_cre_geo_backfill.py: unit tests for cre_geo_backfill.py.

Coverage targets (pure / unit-testable functions only):
  - _copy_str
  - _read_rows_sql
  - derive_row
  - build_sql  (SQL invariants: COALESCE-keep, existence guard, no status/deleted_at)
  - _summarize
  - fetch_rows (monkeypatched iter_copy_json_rows)
  - main dry-run path (monkeypatched load_db_url + find_psql + fetch_rows)

No network, no live DB, no psql connection.

I/O boundary intentionally NOT covered:
  - main() --apply path (shells out to psql via subprocess; pure I/O boundary)
  - fetch_rows when iter_copy_json_rows calls real psql
  - ZipCbsaCrosswalk and derive_geo internals (covered by test_cre_geo.py /
    test_geo_derive.py)
"""

import os
import sys

import pytest

# conftest.py already puts cre_collector/ on sys.path.
import cre_geo_backfill
from cre_geo_backfill import (
    _STAGE_COLS,
    _TABLE,
    _copy_str,
    _read_rows_sql,
    _summarize,
    build_sql,
    derive_row,
)

# Mini fixture path (shared with test_cre_geo.py / test_geo_derive.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_MINI_CSV = os.path.join(_HERE, "fixtures", "geo", "mini_crosswalk.csv")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def crosswalk():
    from cre_geo import ZipCbsaCrosswalk
    return ZipCbsaCrosswalk(csv_path=_MINI_CSV)


# ---------------------------------------------------------------------------
# _copy_str
# ---------------------------------------------------------------------------


def test_copy_str_none_returns_backslash_n():
    assert _copy_str(None) == "\\N"


def test_copy_str_plain_string():
    assert _copy_str("hello") == "hello"


def test_copy_str_tab_escaped():
    assert _copy_str("a\tb") == "a\\tb"


def test_copy_str_newline_escaped():
    assert _copy_str("a\nb") == "a\\nb"


def test_copy_str_carriage_return_escaped():
    assert _copy_str("a\rb") == "a\\rb"


def test_copy_str_backslash_doubled():
    assert _copy_str("a\\b") == "a\\\\b"


def test_copy_str_integer_coerced():
    """Non-string values are coerced to str before encoding."""
    assert _copy_str(42) == "42"


def test_copy_str_uuid_passthrough():
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    assert _copy_str(uuid) == uuid


# ---------------------------------------------------------------------------
# _read_rows_sql
# ---------------------------------------------------------------------------


def test_read_rows_sql_targets_correct_table():
    sql = _read_rows_sql()
    assert "credeals.cre_listings" in sql


def test_read_rows_sql_skips_soft_deleted():
    sql = _read_rows_sql()
    assert "deleted_at IS NULL" in sql


def test_read_rows_sql_targets_null_geo_columns():
    """The WHERE clause must include the three derived geo columns."""
    sql = _read_rows_sql()
    assert "cbsa_code IS NULL" in sql
    assert "cbsa_name IS NULL" in sql
    assert "geo_source IS NULL" in sql


def test_read_rows_sql_no_limit_by_default():
    sql = _read_rows_sql(limit=0)
    assert "LIMIT" not in sql


def test_read_rows_sql_limit_applied():
    sql = _read_rows_sql(limit=100)
    assert "LIMIT 100" in sql


def test_read_rows_sql_aliases_zip_as_postal_code():
    """DB column 'zip' must be aliased to 'postal_code' for derive_geo."""
    sql = _read_rows_sql()
    assert "'postal_code'" in sql or "postal_code" in sql


def test_read_rows_sql_aliases_lat_lng():
    """DB columns lat/lng must be aliased to latitude/longitude for derive_geo."""
    sql = _read_rows_sql()
    assert "latitude" in sql
    assert "longitude" in sql


# ---------------------------------------------------------------------------
# derive_row
# ---------------------------------------------------------------------------


def test_derive_row_returns_id(crosswalk):
    raw = {
        "id": "abc-123",
        "county": None,
        "market": None,
        "submarket": None,
        "postal_code": "75201",
        "latitude": None,
        "longitude": None,
        "cbsa_code": None,
        "cbsa_name": None,
        "geo_source": None,
    }
    result = derive_row(raw, crosswalk)
    assert result["id"] == "abc-123"


def test_derive_row_zip_hit_produces_geo_source(crosswalk):
    raw = {
        "id": "row-1",
        "county": None,
        "market": None,
        "submarket": None,
        "postal_code": "75201",
        "latitude": None,
        "longitude": None,
        "cbsa_code": None,
        "cbsa_name": None,
        "geo_source": None,
    }
    result = derive_row(raw, crosswalk)
    assert result["geo_source"] == "crosswalk_zip"
    assert result["cbsa_code"] == "19100"
    assert "Dallas" in (result["county"] or "")


def test_derive_row_source_county_keeps_source_label(crosswalk):
    raw = {
        "id": "row-2",
        "county": "Fulton County, GA",
        "market": None,
        "submarket": None,
        "postal_code": None,
        "latitude": None,
        "longitude": None,
        "cbsa_code": None,
        "cbsa_name": None,
        "geo_source": None,
    }
    result = derive_row(raw, crosswalk)
    assert result["geo_source"] == "source"
    assert result["county"] == "Fulton County, GA"


def test_derive_row_no_geo_returns_none_geo_source(crosswalk):
    raw = {
        "id": "row-3",
        "county": None,
        "market": None,
        "submarket": None,
        "postal_code": None,
        "latitude": None,
        "longitude": None,
        "cbsa_code": None,
        "cbsa_name": None,
        "geo_source": None,
    }
    result = derive_row(raw, crosswalk)
    assert result["geo_source"] is None


def test_derive_row_keys_are_correct(crosswalk):
    raw = {
        "id": "row-4",
        "county": None,
        "market": None,
        "submarket": None,
        "postal_code": "60601",
        "latitude": None,
        "longitude": None,
        "cbsa_code": None,
        "cbsa_name": None,
        "geo_source": None,
    }
    result = derive_row(raw, crosswalk)
    assert set(result.keys()) == {"id", "county", "cbsa_code", "cbsa_name", "geo_source"}


def test_derive_row_latlng_fallback(crosswalk):
    """Unknown ZIP with valid lat/lng falls through to crosswalk_latlng."""
    raw = {
        "id": "row-5",
        "county": None,
        "market": None,
        "submarket": None,
        "postal_code": "00001",   # not in fixture
        "latitude": "32.79",
        "longitude": "-96.80",
        "cbsa_code": None,
        "cbsa_name": None,
        "geo_source": None,
    }
    result = derive_row(raw, crosswalk)
    assert result["geo_source"] == "crosswalk_latlng"
    assert "Dallas" in (result["county"] or "")


# ---------------------------------------------------------------------------
# build_sql - structure and invariants
# ---------------------------------------------------------------------------


def _make_row(uid="aaaaaaaa-0000-0000-0000-000000000001",
              county="Dallas County, TX",
              cbsa_code="19100",
              cbsa_name="Dallas-Fort Worth-Arlington, TX",
              geo_source="crosswalk_zip"):
    return {
        "id": uid,
        "county": county,
        "cbsa_code": cbsa_code,
        "cbsa_name": cbsa_name,
        "geo_source": geo_source,
    }


def test_build_sql_returns_string():
    sql = build_sql([_make_row()])
    assert isinstance(sql, str)


def test_build_sql_contains_begin_and_commit():
    sql = build_sql([_make_row()])
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


def test_build_sql_creates_temp_staging_table():
    sql = build_sql([_make_row()])
    assert "CREATE TEMP TABLE _geo_stage" in sql
    assert "ON COMMIT DROP" in sql


def test_build_sql_copy_from_stdin():
    sql = build_sql([_make_row()])
    assert "COPY _geo_stage" in sql
    assert "FROM stdin" in sql


def test_build_sql_copy_end_marker():
    sql = build_sql([_make_row()])
    assert "\\." in sql


def test_build_sql_all_stage_cols_in_copy():
    sql = build_sql([_make_row()])
    for col in _STAGE_COLS:
        assert col in sql


def test_build_sql_targets_correct_table():
    sql = build_sql([_make_row()])
    assert _TABLE in sql


def test_build_sql_uses_coalesce_keep():
    """The UPDATE must use COALESCE(l.col, s.col) form for all four geo columns."""
    sql = build_sql([_make_row()])
    assert "COALESCE(l.county, s.county)" in sql
    assert "COALESCE(l.cbsa_code, s.cbsa_code)" in sql
    assert "COALESCE(l.cbsa_name, s.cbsa_name)" in sql
    assert "COALESCE(l.geo_source, s.geo_source)" in sql


def test_build_sql_never_assigns_status():
    """The geo backfill SQL must NEVER assign status or deleted_at."""
    sql = build_sql([_make_row()])
    non_comment_lines = [
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    non_comment = "\n".join(non_comment_lines)
    # The geo backfill writes only geo columns; it must never assign status.
    # Direct guard (no or-short-circuit that would pass vacuously).
    assert "status =" not in non_comment
    assert "status=" not in non_comment


def test_build_sql_never_assigns_deleted_at():
    sql = build_sql([_make_row()])
    non_comment_lines = [
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    non_comment = "\n".join(non_comment_lines)
    assert "deleted_at" not in non_comment


def test_build_sql_existence_guard_for_migration_012():
    """The DO block must check that cbsa_code column exists before updating."""
    sql = build_sql([_make_row()])
    assert "pg_attribute" in sql
    assert "cbsa_code" in sql
    assert "DO $$" in sql


def test_build_sql_null_value_renders_backslash_n():
    """A row with null county should render \\N in the COPY data block."""
    row = _make_row(county=None)
    sql = build_sql([row])
    # Find the COPY data section (between COPY ... FROM stdin; and \\.)
    copy_start = sql.index("FROM stdin;") + len("FROM stdin;")
    copy_end = sql.index("\\.", copy_start)
    copy_block = sql[copy_start:copy_end]
    assert "\\N" in copy_block


def test_build_sql_empty_rows_still_valid():
    """build_sql([]) should not raise and still produce BEGIN/COMMIT."""
    sql = build_sql([])
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


def test_build_sql_multiple_rows_all_in_copy_block():
    rows = [
        _make_row(uid="aaaaaaaa-0000-0000-0000-000000000001"),
        _make_row(uid="bbbbbbbb-0000-0000-0000-000000000002"),
        _make_row(uid="cccccccc-0000-0000-0000-000000000003"),
    ]
    sql = build_sql(rows)
    # Each UUID appears at least once (in the COPY data block)
    assert "aaaaaaaa-0000-0000-0000-000000000001" in sql
    assert "bbbbbbbb-0000-0000-0000-000000000002" in sql
    assert "cccccccc-0000-0000-0000-000000000003" in sql
    # Three distinct data rows should be present in the COPY block
    copy_start = sql.index("FROM stdin;") + len("FROM stdin;")
    copy_end = sql.index("\\.", copy_start)
    copy_block = sql[copy_start:copy_end]
    data_lines = [l for l in copy_block.splitlines() if l.strip()]
    assert len(data_lines) == 3


def test_build_sql_on_error_stop_set():
    sql = build_sql([_make_row()])
    assert "ON_ERROR_STOP" in sql


def test_build_sql_standard_conforming_strings():
    sql = build_sql([_make_row()])
    assert "standard_conforming_strings" in sql


def test_build_sql_coalesce_never_writes_new_when_existing_nonnull():
    """The WHERE guard ensures we only update rows where target cols are still NULL."""
    sql = build_sql([_make_row()])
    # The WHERE clause should check that at least one target column is NULL
    assert "l.county IS NULL" in sql or "l.cbsa_code IS NULL" in sql


# ---------------------------------------------------------------------------
# _summarize
# ---------------------------------------------------------------------------


def test_summarize_empty_list():
    s = _summarize([])
    assert s["total_staged"] == 0
    assert s["no_geo_hit"] == 0
    assert s["will_update"] == 0
    assert s["by_geo_source"] == {}


def test_summarize_all_hits():
    rows = [
        {"geo_source": "crosswalk_zip"},
        {"geo_source": "crosswalk_zip"},
        {"geo_source": "crosswalk_latlng"},
        {"geo_source": "source"},
    ]
    s = _summarize(rows)
    assert s["total_staged"] == 4
    assert s["no_geo_hit"] == 0
    assert s["will_update"] == 4
    assert s["by_geo_source"]["crosswalk_zip"] == 2
    assert s["by_geo_source"]["crosswalk_latlng"] == 1
    assert s["by_geo_source"]["source"] == 1


def test_summarize_all_misses():
    rows = [{"geo_source": None}, {"geo_source": None}]
    s = _summarize(rows)
    assert s["no_geo_hit"] == 2
    assert s["will_update"] == 0


def test_summarize_mixed():
    rows = [
        {"geo_source": "crosswalk_zip"},
        {"geo_source": None},
        {"geo_source": "source"},
        {"geo_source": None},
    ]
    s = _summarize(rows)
    assert s["total_staged"] == 4
    assert s["no_geo_hit"] == 2
    assert s["will_update"] == 2
    assert s["by_geo_source"] == {"crosswalk_zip": 1, "source": 1}


def test_summarize_will_update_equals_total_minus_null():
    rows = [{"geo_source": "crosswalk_zip"}] * 5 + [{"geo_source": None}] * 3
    s = _summarize(rows)
    assert s["will_update"] == s["total_staged"] - s["no_geo_hit"]


# ---------------------------------------------------------------------------
# fetch_rows (monkeypatched iter_copy_json_rows)
# ---------------------------------------------------------------------------


def test_fetch_rows_yields_valid_dicts(monkeypatch):
    """fetch_rows should yield only dicts with a truthy 'id' key."""
    fake_rows = [
        {"id": "uuid-1", "postal_code": "75201", "county": None},
        {"id": "uuid-2", "postal_code": "60601", "county": None},
    ]
    monkeypatch.setattr(cre_geo_backfill, "iter_copy_json_rows",
                        lambda psql, db_url, sql, label="geo_backfill": iter(fake_rows))
    result = list(cre_geo_backfill.fetch_rows("postgres://SENTINEL", "psql", limit=0))
    assert len(result) == 2
    assert all(r["id"] for r in result)


def test_fetch_rows_skips_non_dict(monkeypatch):
    """fetch_rows should skip any items that are not dicts or lack 'id'."""
    fake_rows = [
        "not-a-dict",
        None,
        {"id": None, "postal_code": "75201"},   # id is falsy
        {"id": "valid-id", "postal_code": "77002"},
    ]
    monkeypatch.setattr(cre_geo_backfill, "iter_copy_json_rows",
                        lambda psql, db_url, sql, label="geo_backfill": iter(fake_rows))
    result = list(cre_geo_backfill.fetch_rows("postgres://SENTINEL", "psql", limit=0))
    assert len(result) == 1
    assert result[0]["id"] == "valid-id"


def test_fetch_rows_passes_limit_in_sql(monkeypatch):
    """The SQL passed to iter_copy_json_rows should include LIMIT when limit>0."""
    captured = {}

    def fake_iter(psql, db_url, sql, label="geo_backfill"):
        captured["sql"] = sql
        return iter([])

    monkeypatch.setattr(cre_geo_backfill, "iter_copy_json_rows", fake_iter)
    list(cre_geo_backfill.fetch_rows("postgres://SENTINEL", "psql", limit=50))
    assert "LIMIT 50" in captured["sql"]


# ---------------------------------------------------------------------------
# main() - dry-run path (monkeypatched)
# ---------------------------------------------------------------------------


def _patch_main_dry_run(monkeypatch):
    """Patch all DB I/O so main() --dry-run never connects."""
    monkeypatch.setattr(cre_geo_backfill, "load_db_url",
                        lambda env_file: ("postgres://SENTINEL", "/fake/.env.local"))
    monkeypatch.setattr(cre_geo_backfill, "find_psql", lambda: "psql")
    # fetch_rows returns a couple of synthetic rows
    def fake_fetch(db_url, psql, limit=0):
        return [
            {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "county": None, "market": None, "submarket": None,
                "postal_code": "75201", "latitude": None, "longitude": None,
                "cbsa_code": None, "cbsa_name": None, "geo_source": None,
            },
            {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "county": None, "market": None, "submarket": None,
                "postal_code": "99999", "latitude": None, "longitude": None,
                "cbsa_code": None, "cbsa_name": None, "geo_source": None,
            },
        ]
    monkeypatch.setattr(cre_geo_backfill, "fetch_rows", fake_fetch)


def test_main_dry_run_does_not_call_subprocess(monkeypatch, capsys):
    """--dry-run must never call subprocess.run (no DB write)."""
    _patch_main_dry_run(monkeypatch)
    subprocess_calls = []
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **kw: subprocess_calls.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["cre_geo_backfill.py", "--csv", _MINI_CSV])
    cre_geo_backfill.main()
    # subprocess.run should NOT have been called in dry-run mode
    assert subprocess_calls == []


def test_main_dry_run_does_not_print_sentinel(monkeypatch, capsys):
    _patch_main_dry_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_geo_backfill.py", "--csv", _MINI_CSV])
    cre_geo_backfill.main()
    out = capsys.readouterr().out
    assert "SENTINEL" not in out


def test_main_dry_run_keep_sql_writes_file(monkeypatch, tmp_path, capsys):
    """--keep-sql should write the generated SQL to the given path."""
    _patch_main_dry_run(monkeypatch)
    sql_path = tmp_path / "geo.sql"
    monkeypatch.setattr(
        sys, "argv",
        ["cre_geo_backfill.py", "--csv", _MINI_CSV, "--keep-sql", str(sql_path)],
    )
    cre_geo_backfill.main()
    assert sql_path.exists()
    content = sql_path.read_text()
    assert "BEGIN;" in content
    assert "COMMIT;" in content


def test_main_dry_run_summary_printed(monkeypatch, capsys):
    """The dry-run path should print a derivation summary."""
    _patch_main_dry_run(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["cre_geo_backfill.py", "--csv", _MINI_CSV])
    cre_geo_backfill.main()
    out = capsys.readouterr().out
    assert "total staged" in out.lower() or "staged" in out.lower()


def test_main_empty_crosswalk_prints_warning(monkeypatch, capsys):
    """When the crosswalk is empty (no CSV), a warning is printed to stderr."""
    monkeypatch.setattr(cre_geo_backfill, "load_db_url",
                        lambda env_file: ("postgres://SENTINEL", "/fake/.env.local"))
    monkeypatch.setattr(cre_geo_backfill, "find_psql", lambda: "psql")
    monkeypatch.setattr(cre_geo_backfill, "fetch_rows", lambda db_url, psql, limit=0: [])
    monkeypatch.setattr(sys, "argv", [
        "cre_geo_backfill.py",
        "--csv", "/nonexistent/crosswalk.csv",  # empty crosswalk
    ])
    cre_geo_backfill.main()
    err = capsys.readouterr().err
    assert "WARNING" in err or "empty" in err.lower()


def test_main_no_rows_exits_early(monkeypatch, capsys):
    """When fetch_rows returns nothing, main prints the 'nothing to backfill' message and returns."""
    monkeypatch.setattr(cre_geo_backfill, "load_db_url",
                        lambda env_file: ("postgres://SENTINEL", "/fake/.env.local"))
    monkeypatch.setattr(cre_geo_backfill, "find_psql", lambda: "psql")
    monkeypatch.setattr(cre_geo_backfill, "fetch_rows", lambda db_url, psql, limit=0: [])
    monkeypatch.setattr(sys, "argv", ["cre_geo_backfill.py", "--csv", _MINI_CSV])
    cre_geo_backfill.main()
    out = capsys.readouterr().out
    assert "nothing to backfill" in out.lower() or "0 row" in out.lower()
