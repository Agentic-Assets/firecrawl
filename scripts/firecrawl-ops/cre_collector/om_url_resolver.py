#!/usr/bin/env python3
"""om_url_resolver.py: resolve a viewer-wrapped / non-.pdf brochure URL to its
real .pdf document URL (Phase-2 data-lift WS2, OM-parse tier).

Per the raw_data gap classification (Section "Document corpus audit"), ~37,700
brochure rows on Cushman / Colliers / Lee / SVN carry viewer-wrapped or
non-`.pdf` URLs (a Buildout viewer iframe, a DocumentCloud reader, a
?file=<id> hosted-download link). The OM-parse tier needs the actual PDF bytes
to feed local `/v2/parse`, so this module maps a known viewer shape to its
underlying .pdf URL.

Design rules (conservative, provenance-first):
  - PURE and offline. No network, no DB, no I/O. Every function is a string ->
    string|None transform the unit tests assert directly.
  - NEVER guess. A URL whose viewer shape is not in the recognized vocabulary
    returns None (the caller skips it rather than fabricating a PDF URL). A wrong
    resolution would feed the OM parser the wrong document and could write a
    wrong NOI / cap_rate onto a board-facing column.
  - Already-.pdf URLs pass through unchanged (resolve_pdf_url is idempotent).
  - Only well-understood, deterministic rewrites are encoded. When a viewer
    embeds the real PDF as a query parameter (`?file=`, `?url=`, `?document=`),
    the embedded value is extracted and percent-decoded; when it is not a .pdf,
    return None.

Public surface:
    resolve_pdf_url(url) -> str | None
    is_pdf_url(url)      -> bool

Python stdlib only.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# A URL whose path ends in `.pdf` (optionally followed by a query/fragment) is
# already a direct document. Mirrors the `.pdf` arm of cre_parse._DOC_EXT_RE but
# is .pdf-only (the OM parser only handles PDFs in this tier).
_PDF_PATH_RE = re.compile(r"\.pdf(?:[?#]|$)", re.I)

# Query-parameter names a viewer commonly uses to carry the wrapped document URL
# (pdf.js `?file=`, generic `?url=` / `?document=` / `?src=`). Lowercased compare.
_EMBED_PARAM_NAMES = ("file", "url", "document", "src", "documenturl", "pdf")

# Buildout serves a brochure as a hosted download at /sharing/<id> or via a
# ?file=<numeric-id> link; neither ends in .pdf but both are the real document.
# We CANNOT synthesize a .pdf URL for a bare numeric ?file=<id> (the id is opaque
# and the real bytes are served behind a redirect), so those return None here and
# are handled by the live Buildout enricher, not by a blind rewrite.
_BUILDOUT_HOST_RE = re.compile(r"(?:^|\.)buildout\.com$", re.I)


def _host_of(url: str) -> str:
    """Lowercased hostname, or '' on a non-http(s) / malformed URL."""
    try:
        netloc = urlparse(url).netloc
    except (ValueError, AttributeError):
        return ""
    return netloc.split("@")[-1].split(":")[0].lower()


def is_pdf_url(url):
    """True when the URL's path is a direct `.pdf` (query/fragment allowed)."""
    if not isinstance(url, str) or not url:
        return False
    try:
        path = urlparse(url).path
    except (ValueError, AttributeError):
        return False
    return bool(_PDF_PATH_RE.search(path))


def _embedded_pdf_from_query(url):
    """If a viewer carries the real document as a query param (?file=<pdf-url>,
    ?url=<pdf-url>, ...), extract and percent-decode it; return it only when the
    decoded value is itself a `.pdf` URL. Else None (never guess)."""
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return None
    if not parsed.query:
        return None
    qs = parse_qs(parsed.query, keep_blank_values=False)
    # Case-insensitive param lookup.
    lowered = {k.lower(): v for k, v in qs.items()}
    for name in _EMBED_PARAM_NAMES:
        vals = lowered.get(name)
        if not vals:
            continue
        candidate = unquote(vals[0]).strip()
        # A nested viewer can double-wrap; one more decode + an http(s) shape.
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        if is_pdf_url(candidate):
            return candidate
        # The embedded value might itself be a viewer carrying ?file=; recurse
        # once so a double-wrapped pdf.js link still resolves. Bounded by the
        # http(s) + .pdf gate above, so this terminates.
        nested = _embedded_pdf_from_query(candidate)
        if nested:
            return nested
    return None


def resolve_pdf_url(url):
    """Resolve a viewer-wrapped / non-.pdf brochure URL to its real .pdf URL.

    Returns the .pdf URL string, or None when the URL is not resolvable to a
    direct PDF by a deterministic rewrite (the caller then skips it; it is NEVER
    guessed). Idempotent: a URL already ending in `.pdf` is returned unchanged.

    Resolution order:
      1. Already a `.pdf` path -> return as-is.
      2. A viewer carrying the real document in a query param
         (?file= / ?url= / ?document= / ?src=) whose decoded value is a `.pdf`
         -> return that embedded URL.
      3. Anything else (a bare Buildout /sharing/<id>, a ?file=<numeric-id>, a
         DocumentCloud reader with no embedded .pdf, an opaque viewer) -> None.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    if is_pdf_url(url):
        return url
    embedded = _embedded_pdf_from_query(url)
    if embedded:
        return embedded
    # A bare Buildout hosted-download (/sharing/<id> or ?file=<numeric-id>) is a
    # real document but its PDF bytes are served behind an opaque redirect we
    # cannot deterministically rewrite. Return None rather than guess; the live
    # Buildout enricher (lib/enrich.ts) handles those.
    return None
