"""
test_norm_status_canonical_and_guards.py

Synthetic-only complement to test_norm_status_shapes.py (which validates
norm_status against real out/ artifacts and skips when they are absent).

This file needs NO artifacts, so it is the portable CI signal for the parts the
artifact-based suite does not cover:

  1. _canonical_key (untested by the shapes suite): geo / geoless degrade /
     no-address / bool-lat guard / latitude fallback / street fallback / case+
     whitespace normalization.
  2. Adversarial word-boundary guards: STATUS_RULES must not fire on substrings
     ("pending" in "spending", "sold" in "Soldiers", "leased" in "released").
  3. url-slug text fallback (positive) and scoped-field-only enforcement
     (description / notes are NEVER scanned).
  4. Source-classification completeness in BOTH directions, so a newly added
     collector source cannot silently fall through to an undefined status tier.

Design: cre-intelligence-system-design.md sections 6 and 12.5.
"""

from cre_ingest import (
    SOURCE_TO_BROKERAGE,
    STATUS_SOURCE_PATHS,
    _canonical_key,
    norm_status,
)

ALLOWED = {"sold", "under_contract", "pending", "leased", "off_market"}


# ---------------------------------------------------------------------------
# 1. _canonical_key (advisory re-listing key)
# ---------------------------------------------------------------------------

def test_canonical_key_with_geo_rounds_to_4dp():
    key = _canonical_key({"address": " 123 Main St ", "state": "TX", "lat": 30.12345})
    assert key == f"123 main st|tx|{round(30.12345, 4)}"


def test_canonical_key_geoless_degrades_to_addr_state():
    assert _canonical_key({"address": "123 Main St", "state": "TX"}) == "123 main st|tx"


def test_canonical_key_latitude_alias_is_honored():
    assert _canonical_key(
        {"address": "9 Elm", "state": "ny", "latitude": 40.5}
    ) == f"9 elm|ny|{round(40.5, 4)}"


def test_canonical_key_street_alias_when_no_address():
    assert _canonical_key({"street": "5 Oak Ave", "state": "CA"}) == "5 oak ave|ca"


def test_canonical_key_no_address_is_none():
    assert _canonical_key({"state": "TX", "lat": 30.1}) is None
    assert _canonical_key({"address": "   ", "state": "TX"}) is None


def test_canonical_key_bool_lat_is_ignored():
    """A bool lat (True == 1 numerically) must NOT be appended as geo."""
    assert _canonical_key({"address": "1 A St", "state": "tx", "lat": True}) == "1 a st|tx"


def test_canonical_key_missing_state_yields_empty_state_segment():
    assert _canonical_key({"address": "7 Pine"}) == "7 pine|"


def test_canonical_key_non_dict_is_none():
    for bad in (None, "x", 42, ["a"]):
        assert _canonical_key(bad) is None


# ---------------------------------------------------------------------------
# 2. Adversarial word-boundary guards (no false positives on substrings)
# ---------------------------------------------------------------------------

def test_word_boundary_no_substring_false_positives():
    """STATUS_RULES are word-boundary anchored; substrings must NOT match."""
    negatives = [
        "Spending Spree Plaza",       # 'pending' inside 'spending'
        "Soldiers Field Road Retail",  # 'sold' inside 'Soldiers'
        "Released Office Tower",       # 'leased' inside 'released'
        "Independence Plaza",          # no terminal token
        "Suspended animation studio",  # 'pending' not present at boundary
    ]
    for title in negatives:
        assert norm_status({"sourceKey": "cbre", "title": title}) is None, title


def test_word_boundary_true_positives_still_fire():
    """The same rules must still fire on real word-boundary matches."""
    assert norm_status({"sourceKey": "cbre", "title": "Now Sold"}) == "sold"
    assert norm_status({"sourceKey": "cbre", "title": "Sale Pending"}) == "pending"
    assert norm_status({"sourceKey": "cbre", "title": "Fully Leased NNN"}) == "leased"
    assert norm_status({"sourceKey": "cbre", "title": "Under Contract - 5th St"}) == "under_contract"
    assert norm_status({"sourceKey": "cbre", "title": "Withdrawn from market"}) == "off_market"


# ---------------------------------------------------------------------------
# 3. Text fallback: url-slug positive, scoped-fields-only enforcement
# ---------------------------------------------------------------------------

def test_url_slug_text_fallback_positive():
    assert norm_status(
        {"sourceKey": "cbre", "url": "https://example.com/listings/123-main-st-sold"}
    ) == "sold"


def test_text_fallback_scans_only_scoped_fields():
    """Whole-blob scanning is forbidden: terminal words in unscoped fields must NOT fire."""
    assert norm_status({"sourceKey": "cbre", "description": "this property sold last year"}) is None
    assert norm_status({"sourceKey": "cbre", "notes": "leased to a national tenant"}) is None
    assert norm_status({"sourceKey": "cbre", "summary": "under contract since 2019"}) is None


def test_never_returns_active_broad():
    """Reinforce the core invariant across assorted shapes."""
    cases = [
        {"sourceKey": "svn", "status": "Available"},
        {"sourceKey": "colliers", "status": "Active"},
        {"sourceKey": "cushman-wakefield", "listingStatus": "active"},
        {"sourceKey": "cbre", "title": "Prime active retail pad"},
        {},
    ]
    for c in cases:
        assert norm_status(c) != "active", c


def test_emitted_vocabulary_subset_of_allowed():
    samples = [
        {"sourceKey": "colliers", "status": "Sold"},
        {"sourceKey": "svn", "underContract": True},
        {"sourceKey": "svn", "closed": True},
        {"sourceKey": "cbre", "title": "Sale Pending - 123 Main"},
        {"sourceKey": "cbre", "title": "Now Leased"},
        {"sourceKey": "cbre", "title": "Withdrawn from market"},
    ]
    for s in samples:
        st = norm_status(s)
        assert st is None or st in ALLOWED, (s, st)


# ---------------------------------------------------------------------------
# 4. Source-classification completeness (both directions)
# ---------------------------------------------------------------------------

def test_every_collector_source_is_classified():
    """Every SOURCE_TO_BROKERAGE source has an explicit STATUS_SOURCE_PATHS entry.

    A new source added without an entry would default to [] silently; this test
    forces an intentional decision (status-bearing list, or [] for the
    disappearance-only tier) when a source is added.
    """
    missing = set(SOURCE_TO_BROKERAGE) - set(STATUS_SOURCE_PATHS)
    assert not missing, (
        "sources missing an explicit STATUS_SOURCE_PATHS classification "
        f"(add a path list, or [] for disappearance-only): {sorted(missing)}"
    )


def test_no_orphan_status_paths():
    """No STATUS_SOURCE_PATHS key references a source absent from the brokerage map."""
    orphan = set(STATUS_SOURCE_PATHS) - set(SOURCE_TO_BROKERAGE)
    assert not orphan, f"STATUS_SOURCE_PATHS references unknown sources: {sorted(orphan)}"
