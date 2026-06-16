"""Regression tests for the robust COPY read-back path and the geo column fix.

Two live-schema defects (found 2026-06-15 via the dry-runs) motivate these:

  1. `COPY (...) TO STDOUT` default (text) format DOUBLES backslashes, so a
     raw_data value carrying a JSON escape (HTML with \\" , a Windows path) came
     back as invalid JSON and `json.loads` failed. The old reader's bare
     `except JSONDecodeError: continue` then SILENTLY DROPPED the row -- 100% of
     Marcus & Millichap's 3,124 rows (their raw_data embeds escaped-quote HTML)
     vanished with no error. The reader now uses CSV COPY format
     (`cre_ingest.parse_copy_csv_json`) and ABORTS on an undecodable row.

  2. `cre_geo_backfill` read `postal_code / latitude / longitude`, but the live
     `cre_listings` columns are `zip / lat / lng`. The read SQL now aliases the
     real columns to the keys `derive_geo` expects, so ZIP/lat-lng derivation
     actually fires (98% of rows) instead of silently yielding only `county`.

Pure-transform: no DB, no network.
"""
import csv
import io
import json

import pytest

import cre_ingest
import cre_backfill_raw_data as bf
import cre_geo_backfill as geo
import om_classify_existing as oc
import backfill_media_from_raw_data as media


def _csv_cell(text):
    """Encode one value as a single-cell CSV row exactly like
    `COPY (SELECT ...::text) TO STDOUT WITH (FORMAT csv)` emits it."""
    buf = io.StringIO()
    csv.writer(buf).writerow([text])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# parse_copy_csv_json: the CSV round-trip + fail-loud contract
# ---------------------------------------------------------------------------


def test_roundtrips_backslash_bearing_html():
    """The exact shape that broke the text-format reader (M&M PropertyDetail)."""
    obj = {
        "id": "abc",
        "source_key": "marcus-millichap",
        "raw_data": {"PropertyDetail": '<div class="mm-tile" data-activityId="ZAF067">'},
    }
    stdout = _csv_cell(json.dumps(obj))
    out = list(cre_ingest.parse_copy_csv_json(stdout, label="t"))
    assert len(out) == 1
    assert out[0]["raw_data"]["PropertyDetail"] == '<div class="mm-tile" data-activityId="ZAF067">'


def test_roundtrips_embedded_newline_in_value():
    """A quoted CSV field may span physical lines; the reader must not split it."""
    obj = {"id": "x", "raw_data": {"description": "line1\nline2\twith tab"}}
    stdout = _csv_cell(json.dumps(obj))
    out = list(cre_ingest.parse_copy_csv_json(stdout, label="t"))
    assert out[0]["raw_data"]["description"] == "line1\nline2\twith tab"


def test_aborts_on_undecodable_row_never_silently_skips():
    """A malformed row must raise, not be dropped (the bug that hid the M&M loss)."""
    stdout = _csv_cell('{"id": "x", "raw') + _csv_cell('{"id": "ok"}')
    with pytest.raises(ValueError):
        list(cre_ingest.parse_copy_csv_json(stdout, label="t"))


def test_skips_blank_lines():
    assert list(cre_ingest.parse_copy_csv_json("\n\n", label="t")) == []


def test_decodes_field_over_csv_default_limit():
    """A raw_data object larger than the csv module's 131072-byte default field
    limit must still decode (the limit is raised) -- this was the real
    cushman-wakefield failure, not bad data."""
    big = {"id": "big", "raw_data": {"blob": "x" * 200000}}
    stdout = _csv_cell(json.dumps(big))
    out = list(cre_ingest.parse_copy_csv_json(stdout, label="t"))
    assert len(out) == 1
    assert len(out[0]["raw_data"]["blob"]) == 200000


def test_multiple_rows_decode_in_order():
    stdout = _csv_cell('{"id": "1"}') + _csv_cell('{"id": "2"}')
    out = list(cre_ingest.parse_copy_csv_json(stdout, label="t"))
    assert [o["id"] for o in out] == ["1", "2"]


# ---------------------------------------------------------------------------
# Geo read SQL: aliases the REAL columns (zip/lat/lng) to derive_geo's keys
# ---------------------------------------------------------------------------


def test_geo_read_sql_aliases_real_columns():
    sql = geo._read_rows_sql()
    # Reads the columns that actually exist (zip/lat/lng) under the keys
    # derive_geo consumes (postal_code/latitude/longitude).
    assert "'postal_code', zip" in sql
    assert "'latitude', lat" in sql
    assert "'longitude', lng" in sql
    # The pre-fix bug read non-existent columns named postal_code/latitude/longitude.
    assert "', postal_code" not in sql
    assert "', latitude" not in sql
    assert "', longitude" not in sql


# ---------------------------------------------------------------------------
# Every read SQL is now an inner SELECT (the helper wraps COPY ... FORMAT csv)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_sql",
    [
        lambda: bf.read_rows_sql("cbre"),
        lambda: geo._read_rows_sql(),
        oc.read_brochure_rows_sql,
        lambda: media.read_rows_sql({"jll"}),
    ],
    ids=["raw_data", "geo", "classify", "media"],
)
def test_read_sql_is_inner_select_text(make_sql):
    sql = make_sql()
    assert sql.lstrip().startswith("SELECT")
    # The helper adds `COPY (...) TO STDOUT WITH (FORMAT csv)`; the inner SELECT
    # must NOT carry its own COPY/TO STDOUT (text format) wrapper.
    assert "TO STDOUT" not in sql
    assert "COPY" not in sql
    # Cast to text for a clean, backslash-safe CSV round-trip.
    assert "::text" in sql
