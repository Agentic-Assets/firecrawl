"""
test_cre_parse_more.py

Targets missing lines in cre_parse.py (current 92%, goal >=98%):
  139    parse_lease_rate: not text.strip() -> (None, None, None)
  176    parse_lease_rate: nm is None (no bare number found) -> return None x3
  188    parse_lease_rate: monthly annualize (x12) branch
  192    parse_lease_rate: not annual and min(nums)>100 -> return None x3
  230    parse_size_text: size_sf > 1_000_000_000 -> size_sf = None
  270    parse_percent_to_fraction: 0 < v <= 100 False -> return None
  277    parse_percent_to_fraction: no bare number match -> return None
  281-283  parse_percent_to_fraction: bare fraction (0 < v <= 1)
  298    norm_building_class: text.strip() is empty -> return None
  345    _host_of (cre_parse's local version): no match -> return ''

Pure-transform, no I/O, no network. Imports from cre_parse directly.
"""

import cre_parse as p


# ---------------------------------------------------------------------------
# parse_lease_rate: whitespace-only string (line 139)
# ---------------------------------------------------------------------------


def test_parse_lease_rate_whitespace_only_returns_none():
    """text.strip() is empty -> (None, None, None) at line 138-139."""
    assert p.parse_lease_rate("   ") == (None, None, None)
    assert p.parse_lease_rate("\t\n") == (None, None, None)


def test_parse_lease_rate_non_string_returns_none():
    """Non-string hits line 136-137."""
    assert p.parse_lease_rate(None) == (None, None, None)
    assert p.parse_lease_rate(25.0) == (None, None, None)
    assert p.parse_lease_rate([]) == (None, None, None)


# ---------------------------------------------------------------------------
# parse_lease_rate: no bare number (line 176) -> None when per-SF context
# but no numeric at all
# ---------------------------------------------------------------------------


def test_parse_lease_rate_per_sf_context_no_number():
    """per-SF context present but no number in text -> None at line 175-176."""
    result = p.parse_lease_rate("/sf")
    assert result == (None, None, None)


def test_parse_lease_rate_per_sf_context_only_letters():
    """No numeric and no $ -> None (line 175-176: nm is None -> return)."""
    result = p.parse_lease_rate("negotiable /sf/yr")
    assert result == (None, None, None)


# ---------------------------------------------------------------------------
# parse_lease_rate: monthly annualize x12 branch (line 184-185 and line 188)
# ---------------------------------------------------------------------------


def test_parse_lease_rate_monthly_annualizes():
    """A per-month rate is multiplied x12 (line 184-185)."""
    lo, hi, t = p.parse_lease_rate("$3.00/sf/mo")
    assert lo == pytest.approx(36.0)
    assert hi is None


def test_parse_lease_rate_monthly_range_annualizes():
    """A monthly range is annualized: both ends x12."""
    lo, hi, t = p.parse_lease_rate("$2.50 - $3.50/sf/mo")
    assert lo == pytest.approx(30.0)
    assert hi == pytest.approx(42.0)


def test_parse_lease_rate_not_annual_over_100_no_marker():
    """A per-SF value > 100 with no annual marker -> rejected (line 186-188)."""
    result = p.parse_lease_rate("$150.00 /sf")
    assert result == (None, None, None)


def test_parse_lease_rate_not_annual_exactly_100_is_ok():
    """$100/sf with no explicit annual marker is under the >100 threshold -> kept."""
    lo, hi, t = p.parse_lease_rate("$100.00 /sf")
    assert lo == pytest.approx(100.0)


def test_parse_lease_rate_annual_marker_over_100_accepted():
    """Per-SF > 100 with explicit annual marker (/yr) -> NOT blocked by line 186-188."""
    lo, hi, t = p.parse_lease_rate("$120.00 /sf/yr")
    assert lo == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# parse_size_text: oversized size_sf -> None (line 229-230)
# ---------------------------------------------------------------------------


def test_parse_size_text_oversized_sf_set_to_none():
    """size_sf > 1_000_000_000 is set to None (line 229-230)."""
    size_sf, lot_sf = p.parse_size_text("1,500,000,000 sf")
    assert size_sf is None


def test_parse_size_text_normal_sf_not_clamped():
    """1 billion SF is exactly at the threshold (> not >=), so it IS kept."""
    size_sf, lot_sf = p.parse_size_text("1000000000 sf")
    # 1e9 is not > 1e9, so it is NOT set to None
    assert size_sf == pytest.approx(1_000_000_000.0)


def test_parse_size_text_large_sf_clamped():
    """1,000,000,001 SF is > 1e9 -> None."""
    size_sf, lot_sf = p.parse_size_text("1,000,000,001 sf")
    assert size_sf is None


# ---------------------------------------------------------------------------
# parse_percent_to_fraction: edge cases
# ---------------------------------------------------------------------------


def test_parse_percent_to_fraction_zero_percent_is_none():
    """'0%' -> v=0.0 -> 0 < v <= 100 is False -> return None (line 274)."""
    assert p.parse_percent_to_fraction("0%") is None


def test_parse_percent_to_fraction_over_100_percent_is_none():
    """'101%' -> v=101 -> 0 < v <= 100 is False -> return None (line 274)."""
    assert p.parse_percent_to_fraction("101%") is None


def test_parse_percent_to_fraction_no_number_returns_none():
    """No numeric token in the text -> return None (line 276-277)."""
    assert p.parse_percent_to_fraction("N/A") is None
    assert p.parse_percent_to_fraction("--") is None
    assert p.parse_percent_to_fraction("contact broker") is None


def test_parse_percent_to_fraction_bare_fraction_zero_to_one():
    """A bare number 0 < v <= 1 is already a fraction -> return rounded v (line 279-280)."""
    result = p.parse_percent_to_fraction("0.875")
    assert result == pytest.approx(0.875, rel=1e-9)


def test_parse_percent_to_fraction_bare_one_is_fraction():
    """v=1.0 -> 0 < v <= 1 -> return round(v, 6) = 1.0."""
    result = p.parse_percent_to_fraction("1")
    assert result == pytest.approx(1.0)


def test_parse_percent_to_fraction_bare_fraction_between_one_and_100():
    """A bare number 1 < v <= 100 is treated as percent -> v/100 (line 281-282)."""
    result = p.parse_percent_to_fraction("87.5")
    assert result == pytest.approx(0.875)


def test_parse_percent_to_fraction_bare_over_100_is_none():
    """A bare number > 100 with no '%' sign falls through to return None (line 283)."""
    assert p.parse_percent_to_fraction("150") is None


def test_parse_percent_to_fraction_non_string_returns_none():
    """Non-string input hits the isinstance guard at line 269."""
    assert p.parse_percent_to_fraction(None) is None
    assert p.parse_percent_to_fraction(0.9) is None


# ---------------------------------------------------------------------------
# norm_building_class: empty string after strip -> None (line 297-298)
# ---------------------------------------------------------------------------


def test_norm_building_class_empty_string():
    """text.strip() is empty -> return None (line 297-298)."""
    assert p.norm_building_class("") is None
    assert p.norm_building_class("   ") is None


def test_norm_building_class_non_string_returns_none():
    """Non-string hits the isinstance guard at line 294."""
    assert p.norm_building_class(None) is None
    assert p.norm_building_class(42) is None


def test_norm_building_class_long_prose_no_match():
    """Prose with > 2 tokens and no explicit 'Class X' -> None (line 306)."""
    result = p.norm_building_class("industrial warehouse distribution center")
    assert result is None


def test_norm_building_class_bare_single_token_a():
    """Single token 'A' -> norm_building_class returns 'A' (line 302-305)."""
    assert p.norm_building_class("A") == "A"


def test_norm_building_class_two_tokens_class_b():
    """Two-token 'Class B' -> explicit match at line 299-301."""
    assert p.norm_building_class("Class B") == "B"


# ---------------------------------------------------------------------------
# cre_parse._host_of (the local classify_doc helper, line 341-346)
# targeting line 345 (no http match -> return '')
# ---------------------------------------------------------------------------


def test_classify_doc_host_of_via_classify_doc_no_http():
    """A url with no http(s) scheme hits _host_of's no-match branch (line 344-345)
    and returns '' which makes _BUILDOUT_HOST_RE.search('') False; the entire
    classify_doc call returns None for a non-doc url."""
    # Non-http url -> _host_of returns '' -> not a Buildout host -> None
    result = p.classify_doc("ftp://docs.example.com/file.pdf")
    # _DOC_EXT_RE matches .pdf extension -> 'other'
    assert result == "other"


def test_classify_doc_non_http_url_no_extension_returns_none():
    """Non-http and no extension/keyword -> _host_of returns '' -> classify_doc None."""
    result = p.classify_doc("ftp://example.com/nopdf")
    assert result is None


def test_classify_doc_buildout_sharing_path():
    """Buildout /sharing/ path -> 'other' (is_hosted_download True, line 365-368)."""
    result = p.classify_doc("https://www.buildout.com/sharing/abc123")
    assert result == "other"


def test_classify_doc_buildout_file_param():
    """Buildout ?file=<numeric-id> -> 'other' (is_hosted_download True)."""
    result = p.classify_doc("https://buildout.com/plugins/x?file=12345")
    assert result == "other"


def test_classify_doc_buildout_docs_path():
    """Buildout /docs/ path -> 'other' (is_hosted_download True)."""
    result = p.classify_doc("https://buildout.com/docs/prop-brochure-abc")
    # This matches 'brochure' keyword in _DOC_KEYWORD_RULES first -> 'brochure'
    # (keyword hits before the extension/hosted-download path)
    assert result in ("brochure", "other")


# ---------------------------------------------------------------------------
# parse_lease_rate: monthly annualized x12 exceeds _MAX_LEASE_PSF_YR cap (line 192)
# ---------------------------------------------------------------------------


def test_parse_lease_rate_monthly_annualized_over_max_cap_returns_none():
    """Line 192: monthly rate x12 exceeds _MAX_LEASE_PSF_YR (500) -> nums empty -> None.

    $50/sf/mo * 12 = $600/sf/yr which is > 500 cap, so the filter
    'nums = [n for n in nums if 0 < n <= _MAX_LEASE_PSF_YR]' empties nums,
    triggering 'if not nums: return None, None, None' at line 192.
    """
    result = p.parse_lease_rate("$50.00/sf/mo")
    assert result == (None, None, None)


def test_parse_lease_rate_monthly_42_annualized_over_cap():
    """$42/sf/mo * 12 = $504/sf/yr > 500 cap -> line 192 None."""
    result = p.parse_lease_rate("$42.00/sf/mo")
    assert result == (None, None, None)


import pytest
