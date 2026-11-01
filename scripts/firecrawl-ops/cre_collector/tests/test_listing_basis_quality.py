"""Listing-basis data-quality rows; synthetic, no DB or network."""

import re

import cre_data_quality_audit as audit
import cre_validate as validate

COLUMNS = tuple(name for name, _ in validate.LISTING_BASIS_CHECKS)


def _minimal_report(**query_rows):
    return {
        "generated_at": "2026-11-01T00:00:00+00:00",
        "env_file": "/fake/.env.local",
        "queries": query_rows,
        "query_timings": {},
        "psql_warnings": [],
    }


def test_family_is_full_profile_only():
    assert "listing_basis_quality" in validate.queries_for_profile()
    assert "listing_basis_quality" not in validate.CHECKPOINT_PREFLIGHT_QUERY_NAMES
    preflight = validate.queries_for_profile(validate.CHECKPOINT_PREFLIGHT_PROFILE)
    assert "listing_basis_quality" not in preflight


def test_checks_are_unique_and_warn_keys_exclude_only_the_info_finding():
    assert len(set(COLUMNS)) == len(COLUMNS)
    assert validate.LISTING_BASIS_INFO_KEY in COLUMNS
    assert audit.LISTING_BASIS_WARN_KEYS == tuple(
        name for name in COLUMNS if name != validate.LISTING_BASIS_INFO_KEY
    )


def _selected_columns(sql: str) -> list[str]:
    return re.findall(r"^  \)::\w+ AS (\w+)", sql, flags=re.M)


def test_both_tools_render_every_check_in_mapping_order():
    validator_sql = validate.QUERIES["listing_basis_quality"]
    audit_sql = audit.QUERIES["listing_basis_quality"].sql
    assert _selected_columns(validator_sql) == list(COLUMNS)
    assert _selected_columns(audit_sql) == list(COLUMNS)
    assert "::bigint AS rows_checked" in audit_sql
    assert "rows_checked" not in validator_sql
    assert ")::text AS" in validator_sql
    assert ")::bigint AS" in audit_sql


def test_both_tools_share_each_condition_verbatim():
    def squash(text: str) -> str:
        return " ".join(text.split())

    for sql in (
        validate.QUERIES["listing_basis_quality"],
        audit.QUERIES["listing_basis_quality"].sql,
    ):
        flat = squash(sql)
        for _, condition in validate.LISTING_BASIS_CHECKS:
            assert squash(condition) in flat


def test_validate_renders_label():
    report = _minimal_report(
        listing_basis_quality=[{"source_key": "svn", "mf_avg_unit_size": "2"}]
    )
    markdown = validate.render_markdown(report)
    assert "## Listing Basis Quality" in markdown


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


def test_every_warn_key_alone_yields_one_warn_finding():
    for key in audit.LISTING_BASIS_WARN_KEYS:
        findings = audit.build_findings({"listing_basis_quality": [_row(**{key: 1})]})
        assert [f["severity"] for f in findings] == ["WARN"], key
        assert f"{key}=1" in findings[0]["detail"]


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
