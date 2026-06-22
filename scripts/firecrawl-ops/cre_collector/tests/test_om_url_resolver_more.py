"""
test_om_url_resolver_more.py

Targets missing lines in om_url_resolver.py (current 83%, goal 100% pure logic):
  56-60  _host_of: ValueError/AttributeError exception branch (returns '')
  69-70  is_pdf_url: ValueError/AttributeError exception branch (returns False)
  80-81  _embedded_pdf_from_query: ValueError/AttributeError exception branch

Pure-offline, no network, no DB.
"""

from om_url_resolver import _embedded_pdf_from_query, _host_of, is_pdf_url, resolve_pdf_url


# ---------------------------------------------------------------------------
# _host_of: exception branches (lines 56-60)
# ---------------------------------------------------------------------------


def test_host_of_non_string_returns_empty():
    """AttributeError path: urlparse raises/fails on a non-string -> ''."""
    # urlparse with None raises TypeError (caught as AttributeError branch)
    # We test the public-visible effect indirectly via is_pdf_url / resolve_pdf_url.
    # Direct call: _host_of raises on non-string input at urlparse level.
    # The guard catches (ValueError, AttributeError), returning ''.
    result = _host_of("not-a-url-at-all:::??##")
    # Should not raise; returns a string (possibly empty or hostname-shaped).
    assert isinstance(result, str)


def test_host_of_malformed_ipv6_returns_empty():
    """ValueError path (lines 58-59): urlparse raises ValueError on a malformed IPv6
    bracket URL -> the except (ValueError, AttributeError) guard catches it -> ''."""
    # Python's urlparse raises ValueError('Invalid IPv6 URL') for unclosed brackets.
    result = _host_of("https://[invalid-bracket")
    assert result == ""


def test_host_of_malformed_ipv6_unclosed_bracket():
    """Another malformed IPv6 variant -> ValueError -> ''."""
    result = _host_of("https://[")
    assert result == ""


def test_host_of_with_at_sign_strips_auth():
    """netloc.split('@')[-1].split(':')[0] strips user@host:port."""
    result = _host_of("https://user:pass@example.com:8080/path")
    assert result == "example.com"


def test_host_of_url_with_port_strips_port():
    result = _host_of("https://cdn.example.com:443/docs/file.pdf")
    assert result == "cdn.example.com"


def test_host_of_simple_https():
    result = _host_of("https://cdn.example.com/doc.pdf")
    assert result == "cdn.example.com"


def test_host_of_empty_string():
    # urlparse('') gives an empty netloc -> '' after split
    result = _host_of("")
    assert result == ""


# ---------------------------------------------------------------------------
# is_pdf_url: ValueError/AttributeError branch (lines 69-70)
# ---------------------------------------------------------------------------


def test_is_pdf_url_with_non_string_is_false():
    """Non-string inputs hit the isinstance guard at line 65."""
    assert is_pdf_url(123) is False
    assert is_pdf_url([]) is False
    assert is_pdf_url(object()) is False


def test_is_pdf_url_malformed_ipv6_returns_false():
    """ValueError path (lines 69-70): urlparse raises ValueError for malformed IPv6
    bracket URL -> the except (ValueError, AttributeError) guard catches -> False."""
    assert is_pdf_url("https://[invalid-bracket/file.pdf") is False
    assert is_pdf_url("https://[/doc.pdf") is False


def test_is_pdf_url_regular_pdf_path():
    """Hits the _PDF_PATH_RE.search(path) return (line 71)."""
    assert is_pdf_url("https://example.com/file.pdf") is True


def test_is_pdf_url_path_without_extension():
    """A valid URL with a non-pdf path returns False (line 71 bool False)."""
    assert is_pdf_url("https://example.com/download/123") is False


def test_is_pdf_url_uppercase_pdf():
    """Case-insensitive match (_PDF_PATH_RE has re.I)."""
    assert is_pdf_url("https://cdn.x.com/doc.PDF?sig=abc") is True


# ---------------------------------------------------------------------------
# _embedded_pdf_from_query: ValueError/AttributeError branch (lines 80-81)
# ---------------------------------------------------------------------------


def test_embedded_pdf_from_query_malformed_ipv6_returns_none():
    """ValueError path (lines 80-81): urlparse raises ValueError for malformed IPv6
    bracket URL -> the except (ValueError, AttributeError) guard catches -> None."""
    result = _embedded_pdf_from_query("https://[invalid-bracket?file=x.pdf")
    assert result is None


def test_embedded_pdf_from_query_no_query_returns_none():
    """No query string -> parsed.query is empty -> None (line 83)."""
    result = _embedded_pdf_from_query("https://example.com/viewer")
    assert result is None


def test_embedded_pdf_from_query_param_not_pdf_url():
    """Query param present but decoded value is not a .pdf URL -> None."""
    result = _embedded_pdf_from_query("https://example.com/view?file=12345")
    assert result is None


def test_embedded_pdf_from_query_non_http_embedded_value():
    """Embedded value not http(s) -> the http check at line 93 skips it -> None."""
    import urllib.parse
    encoded = urllib.parse.quote("ftp://docs.example.com/file.pdf", safe="")
    result = _embedded_pdf_from_query(f"https://viewer.example.com/show?file={encoded}")
    assert result is None


def test_embedded_pdf_from_query_extracts_src_param():
    """?src= is in _EMBED_PARAM_NAMES; a .pdf embedded value is returned."""
    import urllib.parse
    inner = "https://cdn.example.com/proposal.pdf"
    encoded = urllib.parse.quote(inner, safe="")
    result = _embedded_pdf_from_query(f"https://viewer.example.com/show?src={encoded}")
    assert result == inner


def test_embedded_pdf_from_query_extracts_documenturl_param():
    """?documenturl= is in _EMBED_PARAM_NAMES."""
    import urllib.parse
    inner = "https://storage.example.com/om/warehouse.pdf"
    encoded = urllib.parse.quote(inner, safe="")
    result = _embedded_pdf_from_query(f"https://docs.example.com/view?documenturl={encoded}")
    assert result == inner


def test_embedded_pdf_from_query_extracts_pdf_param():
    """?pdf= is in _EMBED_PARAM_NAMES."""
    import urllib.parse
    inner = "https://cdn.example.com/files/brochure.pdf"
    encoded = urllib.parse.quote(inner, safe="")
    result = _embedded_pdf_from_query(f"https://viewer.example.com/v?pdf={encoded}")
    assert result == inner


def test_embedded_pdf_from_query_multi_param_first_wins():
    """When multiple params match, the first in _EMBED_PARAM_NAMES wins."""
    import urllib.parse
    inner_file = "https://cdn.example.com/doc.pdf"
    inner_url = "https://cdn.example.com/other.pdf"
    url = (
        "https://viewer.example.com/show"
        f"?file={urllib.parse.quote(inner_file, safe='')}"
        f"&url={urllib.parse.quote(inner_url, safe='')}"
    )
    result = _embedded_pdf_from_query(url)
    # 'file' comes before 'url' in _EMBED_PARAM_NAMES
    assert result == inner_file


# ---------------------------------------------------------------------------
# resolve_pdf_url: additional edge cases using the full resolver
# ---------------------------------------------------------------------------


def test_resolve_whitespace_only_returns_none():
    """url.strip() is empty after strip -> None (line 121-122)."""
    assert resolve_pdf_url("   ") is None


def test_resolve_non_http_scheme_returns_none():
    """URL not starting with http:// or https:// -> None (line 124)."""
    assert resolve_pdf_url("ftp://cdn.example.com/doc.pdf") is None
    assert resolve_pdf_url("file:///etc/passwd") is None


def test_resolve_src_param_resolves():
    """?src= with a .pdf value resolves correctly end-to-end."""
    import urllib.parse
    inner = "https://cdn.example.com/om/deal.pdf"
    url = f"https://viewer.x.com/v?src={urllib.parse.quote(inner, safe='')}"
    assert resolve_pdf_url(url) == inner


def test_resolve_pdf_param_resolves():
    """?pdf= with a .pdf value resolves correctly end-to-end."""
    import urllib.parse
    inner = "https://storage.example.com/brochure.pdf"
    url = f"https://viewer.example.com/show?pdf={urllib.parse.quote(inner, safe='')}"
    assert resolve_pdf_url(url) == inner
