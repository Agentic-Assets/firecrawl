"""
test_om_url_resolver.py

Pure / offline contracts for om_url_resolver.py: resolve a viewer-wrapped /
non-.pdf brochure URL to its real .pdf, or None when it is not deterministically
resolvable (NEVER guess). Covers the Cushman / Colliers / Lee / SVN viewer
shapes named in the gap doc and the safety invariant that an unresolvable URL
returns None.
"""

import pytest

from om_url_resolver import is_pdf_url, resolve_pdf_url


# --- is_pdf_url -------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://cdn.example.com/docs/offering.pdf", True),
    ("https://cdn.example.com/docs/offering.PDF", True),
    ("https://cdn.example.com/docs/offering.pdf?token=abc", True),
    ("https://cdn.example.com/docs/offering.pdf#page=2", True),
    ("https://cdn.example.com/docs/offering.html", False),
    ("https://cdn.example.com/viewer?id=123", False),
    ("https://cdn.example.com/offering.pdf.html", False),
    ("", False),
    (None, False),
    ("not a url", False),
])
def test_is_pdf_url(url, expected):
    assert is_pdf_url(url) is expected


# --- already a .pdf -> pass through unchanged (idempotent) ------------------


def test_direct_pdf_passes_through_unchanged():
    url = "https://images.cbre.com/brochures/maple-court-om.pdf"
    assert resolve_pdf_url(url) == url


def test_direct_pdf_with_query_passes_through():
    url = "https://images.cbre.com/brochures/x.pdf?sig=abc123"
    assert resolve_pdf_url(url) == url


def test_resolve_is_idempotent():
    url = "https://x/y.pdf"
    once = resolve_pdf_url(url)
    assert resolve_pdf_url(once) == once == url


# --- viewer carrying the real pdf in a query param -------------------------


def test_pdfjs_file_param_resolves_to_embedded_pdf():
    # pdf.js viewer: ?file=<percent-encoded pdf url> (Cushman / Colliers viewer).
    inner = "https://cdn.colliers.com/docs/offering-memorandum.pdf"
    url = "https://docs.colliers.com/web/viewer.html?file=" + \
        "https%3A%2F%2Fcdn.colliers.com%2Fdocs%2Foffering-memorandum.pdf"
    assert resolve_pdf_url(url) == inner


def test_generic_url_param_resolves():
    inner = "https://assets.cushwake.com/brochure-1234.pdf"
    url = "https://view.cushmanwakefield.com/embed?url=" + \
        "https%3A%2F%2Fassets.cushwake.com%2Fbrochure-1234.pdf"
    assert resolve_pdf_url(url) == inner


def test_document_param_resolves():
    inner = "https://media.lee-associates.com/om/deal-9.pdf"
    url = "https://reader.example.com/v?document=" + \
        "https%3A%2F%2Fmedia.lee-associates.com%2Fom%2Fdeal-9.pdf"
    assert resolve_pdf_url(url) == inner


def test_double_wrapped_viewer_resolves_once_nested():
    inner = "https://cdn.svn.com/docs/final-om.pdf"
    # outer viewer wraps an inner viewer that wraps the pdf.
    nested = "https://reader.x/view?file=" + \
        "https%3A%2F%2Fcdn.svn.com%2Fdocs%2Ffinal-om.pdf"
    from urllib.parse import quote
    url = "https://outer.x/embed?url=" + quote(nested, safe="")
    assert resolve_pdf_url(url) == inner


# --- unresolvable -> None (NEVER guess) ------------------------------------


def test_unresolvable_viewer_returns_none():
    # A viewer with no embedded .pdf and an opaque id: we cannot synthesize a
    # pdf URL, so None (the caller skips it).
    assert resolve_pdf_url("https://docs.cushwake.com/viewer?id=98765") is None


def test_buildout_sharing_link_returns_none():
    # A bare Buildout /sharing/<id> is a real doc but served behind an opaque
    # redirect; we never blindly rewrite it (the live enricher handles it).
    assert resolve_pdf_url("https://buildout.com/sharing/abc123def") is None


def test_buildout_numeric_file_param_returns_none():
    # ?file=<numeric-id> on Buildout is opaque, not a pdf URL.
    assert resolve_pdf_url("https://buildout.com/plugins/x?file=4567890") is None


def test_file_param_pointing_to_html_returns_none():
    # the embedded value is a viewer page, not a .pdf -> not resolvable.
    url = "https://view.x/embed?file=" + \
        "https%3A%2F%2Fcdn.x.com%2Fpage.html"
    assert resolve_pdf_url(url) is None


def test_html_brochure_url_returns_none():
    assert resolve_pdf_url("https://www.cbre.com/properties/123/brochure") is None


@pytest.mark.parametrize("bad", ["", "   ", None, "ftp://x/y.pdf", "not-a-url",
                                 "file:///etc/passwd"])
def test_garbage_inputs_return_none(bad):
    assert resolve_pdf_url(bad) is None


def test_relative_url_returns_none():
    # No scheme/host to anchor a download -> None (never guess a base).
    assert resolve_pdf_url("/docs/offering.pdf") is None
