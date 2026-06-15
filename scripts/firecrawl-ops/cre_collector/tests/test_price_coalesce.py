"""
test_price_coalesce.py

Locks down the L1 price COALESCE-keep fix in cre_ingest.py build_sql().

Pre-fix: sale_price_usd, sale_price_per_sf, lease_rate_min, and lease_rate_max
were overwritten unconditionally with EXCLUDED.* in the DO UPDATE SET block. A
transient parse miss (regex miss, "Call for offer") would null a previously-good
numeric price.

Post-fix: all four price columns use COALESCE(EXCLUDED.x, t.x) so a NULL in
EXCLUDED preserves the prior good value. A real new numeric value still overwrites
because COALESCE picks the first non-NULL.

Pure Python, no DB connection. Asserts against build_sql([], [], scraped_at, set()).
"""

from datetime import datetime, timezone

from cre_ingest import build_sql

_SCRAPED_AT = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _sql():
    return build_sql([], [], _SCRAPED_AT, set())


# ---------------------------------------------------------------------------
# Assert the new COALESCE forms are present
# ---------------------------------------------------------------------------


def test_sale_price_usd_coalesce_present():
    assert "sale_price_usd    = COALESCE(EXCLUDED.sale_price_usd, t.sale_price_usd)" in _sql(), (
        "Expected COALESCE-keep for sale_price_usd in DO UPDATE SET block."
    )


def test_sale_price_per_sf_coalesce_present():
    assert "sale_price_per_sf = COALESCE(EXCLUDED.sale_price_per_sf, t.sale_price_per_sf)" in _sql(), (
        "Expected COALESCE-keep for sale_price_per_sf in DO UPDATE SET block."
    )


def test_lease_rate_min_coalesce_present():
    assert "lease_rate_min    = COALESCE(EXCLUDED.lease_rate_min, t.lease_rate_min)" in _sql(), (
        "Expected COALESCE-keep for lease_rate_min in DO UPDATE SET block."
    )


def test_lease_rate_max_coalesce_present():
    assert "lease_rate_max    = COALESCE(EXCLUDED.lease_rate_max, t.lease_rate_max)" in _sql(), (
        "Expected COALESCE-keep for lease_rate_max in DO UPDATE SET block."
    )


# ---------------------------------------------------------------------------
# Assert the old unconditional overwrite forms are GONE
# ---------------------------------------------------------------------------


def test_sale_price_usd_unconditional_gone():
    sql = _sql()
    # The unconditional form ends the line with the column value and a newline,
    # while the COALESCE form has more content on the same line.
    # Match the exact old substring that cannot appear in the new COALESCE form.
    assert "sale_price_usd    = EXCLUDED.sale_price_usd,\n" not in sql, (
        "Old unconditional sale_price_usd = EXCLUDED form still present. "
        "L1 COALESCE fix was not applied."
    )


def test_sale_price_per_sf_unconditional_gone():
    sql = _sql()
    assert "sale_price_per_sf = EXCLUDED.sale_price_per_sf,\n" not in sql, (
        "Old unconditional sale_price_per_sf = EXCLUDED form still present."
    )


def test_lease_rate_min_unconditional_gone():
    sql = _sql()
    assert "lease_rate_min    = EXCLUDED.lease_rate_min,\n" not in sql, (
        "Old unconditional lease_rate_min = EXCLUDED form still present."
    )


def test_lease_rate_max_unconditional_gone():
    sql = _sql()
    assert "lease_rate_max    = EXCLUDED.lease_rate_max,\n" not in sql, (
        "Old unconditional lease_rate_max = EXCLUDED form still present."
    )


# ---------------------------------------------------------------------------
# Regression: cap_rate still COALESCE-keeps (unchanged neighbor)
# ---------------------------------------------------------------------------


def test_cap_rate_still_coalesce():
    assert "cap_rate          = COALESCE(EXCLUDED.cap_rate, t.cap_rate)" in _sql(), (
        "cap_rate COALESCE-keep was inadvertently removed."
    )
