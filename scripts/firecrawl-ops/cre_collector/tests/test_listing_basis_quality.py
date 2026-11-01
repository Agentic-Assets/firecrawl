"""Listing-basis data-quality rows (AGENTIC-5119); synthetic, no DB or network."""

import cre_data_quality_audit as audit
import cre_validate as validate

COLUMNS = (
    "mf_avg_unit_size",
    "stored_psf_mismatch",
    "stored_psf_without_size",
    "land_stored_psf_without_size",
    "non_land_size_equals_lot",
    "land_size_without_lot",
    "tiny_size_with_price",
    "cap_rate_under_2pct",
    "lease_with_sale_price",
    "price_over_1b_non_portfolio",
    "closed_price_text_active",
)


def _minimal_report(**query_rows):
    return {
        "generated_at": "2026-11-01T00:00:00+00:00",
        "env_file": "/fake/.env.local",
        "queries": query_rows,
        "query_timings": {},
        "psql_warnings": [],
    }


def test_family_is_full_profile_only_and_follows_quality_by_source():
    names = list(validate.QUERIES)
    assert names.index("listing_basis_quality") == names.index("quality_by_source") + 1
    assert "listing_basis_quality" in validate.queries_for_profile()
    assert "listing_basis_quality" not in validate.CHECKPOINT_PREFLIGHT_QUERY_NAMES
    preflight = validate.queries_for_profile(validate.CHECKPOINT_PREFLIGHT_PROFILE)
    assert "listing_basis_quality" not in preflight


def test_validate_sql_names_every_column_as_text_over_active_rows():
    sql = validate.QUERIES["listing_basis_quality"]
    for column in COLUMNS:
        assert f")::text AS {column}" in sql
    assert "l.deleted_at IS NULL AND l.status = 'active'" in sql
    assert "GROUP BY source_key" in sql
    assert "ORDER BY source_key" in sql


def test_validate_renders_label():
    report = _minimal_report(
        listing_basis_quality=[{"source_key": "svn", "mf_avg_unit_size": "2"}]
    )
    markdown = validate.render_markdown(report)
    assert "## Listing Basis Quality" in markdown


def test_audit_query_matches_validate_columns_and_universe():
    sql = audit.QUERIES["listing_basis_quality"].sql
    for column in COLUMNS:
        assert f"AS {column}" in sql
    assert "l.deleted_at IS NULL AND l.status = 'active'" in sql


def _row(**overrides):
    row = {"source_key": "svn", "rows_checked": 10}
    row.update(dict.fromkeys(COLUMNS, 0))
    row.update(overrides)
    return row


def test_audit_findings_warn_on_positive_basis_counts():
    findings = audit.build_findings(
        {"listing_basis_quality": [_row(mf_avg_unit_size=3, cap_rate_under_2pct=1)]}
    )
    warns = [f for f in findings if f["severity"] == "WARN"]
    assert len(warns) == 1
    assert warns[0]["title"] == "svn listing basis flags"
    assert "mf_avg_unit_size=3" in warns[0]["detail"]
    assert "cap_rate_under_2pct=1" in warns[0]["detail"]
    assert "stored_psf_mismatch" not in warns[0]["detail"]


def test_audit_findings_route_closed_price_text_to_lifecycle_as_info():
    findings = audit.build_findings(
        {"listing_basis_quality": [_row(closed_price_text_active=4)]}
    )
    assert [f["severity"] for f in findings] == ["INFO"]
    assert "lifecycle owner" in findings[0]["detail"]


def test_audit_findings_none_when_all_counts_zero():
    findings = audit.build_findings({"listing_basis_quality": [_row()]})
    assert [f["severity"] for f in findings] == ["OK"]
    assert not any("basis" in f["title"] for f in findings)
