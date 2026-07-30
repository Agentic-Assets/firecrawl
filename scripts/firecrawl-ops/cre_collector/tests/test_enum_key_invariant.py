"""
test_enum_key_invariant.py

Encodes the enumeration-key invariant for the CRE monitor layer
(design doc section 12.3 / 14.4).

Invariant: for every monitored source, the key that a feed-only --monitor
adapter would emit as its enumeration id equals the external_id that
cre_ingest.to_row() writes into cre_listings.  When this test is green, the
monitor diffs will align with cre_source_index / cre_listings without any
id-translation layer.

If you flip marcus-millichap to key on ActivityId instead of DealId you MUST
break the test_marcus_keys_on_deal_id case (that is the intended red signal).
"""

import hashlib
import re
from datetime import datetime, timezone

import pytest

from cre_ingest import (
    BUILDOUT_SOURCE_KEYS,
    SOURCE_TO_BROKERAGE,
    canonical_cushman_identity_url,
    to_row,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCRAPED_AT = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc).isoformat()
_NO_BROKERS: dict = {}


def _row(listing: dict) -> dict | None:
    """Call to_row with the minimal required extra args."""
    return to_row(listing, _NO_BROKERS, _SCRAPED_AT)


def _prefix(source_key: str) -> str:
    return SOURCE_TO_BROKERAGE[source_key][1]


def _sha1_url(url: str) -> str:
    """Replicates the no-id fallback in to_row: sha1(url)[:16]."""
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def monitor_enum_key(source_key: str, record: dict) -> str:
    """
    Replicate what a feed-only --monitor adapter would emit as its
    enumeration id for a given source + raw record.

    Rules mirror to_row() exactly (this is the contract being tested):
      1. Buildout source keys: extract propertyId from URL, strip -sale/-lease.
      2. Otherwise: use record["id"] if present and non-empty.
      3. Fallback: "url:" + sha1(url)[:16].

    The caller is responsible for prepending _prefix(source_key).
    """
    url = record.get("url", "")

    if source_key == "cushman-wakefield":
        return "url:v1:" + hashlib.sha256(
            canonical_cushman_identity_url(url).encode()
        ).hexdigest()[:32]

    if source_key in BUILDOUT_SOURCE_KEYS:
        m = re.search(r"[?&]propertyId=([^&#]+)", url)
        if m:
            return re.sub(r"-(sale|lease)$", "", m.group(1))

    raw_id = record.get("id")
    if raw_id is not None and str(raw_id).strip():
        return str(raw_id).strip()

    return "url:" + _sha1_url(url)


def _assert_invariant(source_key: str, listing: dict) -> None:
    """Assert prefix + monitor_enum_key == to_row(listing)["external_id"]."""
    prefix = _prefix(source_key)
    row = _row(listing)
    assert row is not None, f"to_row returned None for {source_key} listing"
    expected_monitor_key = prefix + monitor_enum_key(source_key, listing)
    assert row["external_id"] == expected_monitor_key, (
        f"[{source_key}] external_id mismatch:\n"
        f"  to_row()         -> {row['external_id']!r}\n"
        f"  monitor_enum_key -> {expected_monitor_key!r}"
    )


# ---------------------------------------------------------------------------
# Base listing factory (minimal fields to pass to_row url guard)
# ---------------------------------------------------------------------------

def _base(source_key: str, url: str = "https://example.com/listing/1") -> dict:
    return {"sourceKey": source_key, "url": url, "transactionMode": "sale"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMarcusMillichap:
    """
    Marcus & Millichap's collector stores DealId as listing["id"].
    The external_id must derive from DealId, not ActivityId.

    NOTE: If you change the collector/ingestor to key on ActivityId, this test
    turns red: that is the intended sentinel.
    """

    def test_marcus_keys_on_deal_id(self):
        listing = {
            **_base("marcus-millichap", "https://www.marcusmillichap.com/property/D1"),
            "id": "D1",          # DealId mapped to "id" by the collector
            "ActivityId": "A1",  # ActivityId is NOT the key; monitor must ignore it
        }
        _assert_invariant("marcus-millichap", listing)
        row = _row(listing)
        # Explicit check: external_id must contain DealId, not ActivityId
        assert row["external_id"] == "D1", (
            f"external_id should be 'D1' (DealId); got {row['external_id']!r}. "
            "Flipping to ActivityId must turn this test red."
        )

    def test_marcus_activity_id_would_differ(self):
        """Confirm DealId != ActivityId so the sentinel is meaningful."""
        listing = {
            **_base("marcus-millichap", "https://www.marcusmillichap.com/property/D1"),
            "id": "D1",
            "ActivityId": "A1",
        }
        row = _row(listing)
        # If someone accidentally keyed on ActivityId it would produce "A1" not "D1"
        assert row["external_id"] != "A1", (
            "external_id must not equal ActivityId ('A1')"
        )


class TestBuildout:
    """
    Shared Buildout sources use the Buildout inventory API.
    A dual-mode property appears twice with -sale / -lease propertyId suffixes.
    The ingestor strips the suffix so both passes merge to the same external_id.
    """

    @pytest.mark.parametrize("suffix", ["sale", "lease"])
    def test_svn_strips_suffix(self, suffix):
        url = f"https://www.svn.com/listings/?propertyId=1614726-{suffix}"
        listing = {**_base("svn", url)}
        # Both suffixes must produce the same external_id
        _assert_invariant("svn", listing)
        row = _row(listing)
        assert row["external_id"] == "1614726", (
            f"svn -{suffix} did not strip to '1614726'; got {row['external_id']!r}"
        )

    @pytest.mark.parametrize("suffix", ["sale", "lease"])
    def test_lee_associates_strips_suffix(self, suffix):
        url = f"https://www.lee-associates.com/listings/?propertyId=1614726-{suffix}"
        listing = {**_base("lee-associates", url)}
        _assert_invariant("lee-associates", listing)
        row = _row(listing)
        assert row["external_id"] == "1614726", (
            f"lee-associates -{suffix} did not strip to '1614726'; got {row['external_id']!r}"
        )

    def test_svn_sale_and_lease_produce_same_key(self):
        """Core merge invariant: both passes collapse to a single external_id."""
        url_sale = "https://www.svn.com/listings/?propertyId=1614726-sale"
        url_lease = "https://www.svn.com/listings/?propertyId=1614726-lease"
        row_sale = _row({**_base("svn", url_sale)})
        row_lease = _row({**_base("svn", url_lease)})
        assert row_sale["external_id"] == row_lease["external_id"], (
            "sale and lease passes for the same buildout property must share external_id"
        )

    @pytest.mark.parametrize("suffix", ["sale", "lease"])
    def test_franklin_street_strips_suffix(self, suffix):
        url = f"https://www.franklinst.com/properties/?propertyId=777-{suffix}"
        listing = {**_base("franklin-street", url)}
        _assert_invariant("franklin-street", listing)
        row = _row(listing)
        assert row["external_id"] == "777", (
            f"franklin-street -{suffix} did not strip to '777'; got {row['external_id']!r}"
        )

    @pytest.mark.parametrize(
        "source_key",
        sorted(BUILDOUT_SOURCE_KEYS - {"svn", "lee-associates", "franklin-street"}),
    )
    def test_registered_buildout_sources_collapse_sale_and_lease_identity(
        self, source_key
    ):
        rows = []
        for suffix in ("sale", "lease"):
            url = f"https://broker.example/listings/?propertyId=777-{suffix}"
            listing = {**_base(source_key, url)}
            _assert_invariant(source_key, listing)
            rows.append(_row(listing))

        assert rows[0]["external_id"] == rows[1]["external_id"] == "777"


class TestColliersMain:
    """
    colliers-main entries fold into the 'colliers' brokerage with prefix 'main:'.
    """

    def test_colliers_main_prefix(self):
        listing = {
            **_base("colliers-main", "https://www.colliers.com/en/properties/for-sale/usa1159737"),
            "id": "usa1159737",
        }
        _assert_invariant("colliers-main", listing)
        row = _row(listing)
        assert row["external_id"] == "main:usa1159737", (
            f"colliers-main id not prefixed correctly; got {row['external_id']!r}"
        )
        # brokerage slug must be 'colliers' (folded)
        assert row["slug"] == "colliers"


class TestCbreDealflow:
    """
    cbre-dealflow entries fold into the 'cbre' brokerage with prefix 'dealflow:'.
    """

    def test_dealflow_prefix(self):
        listing = {
            **_base("cbre-dealflow", "https://www.cbre.com/properties/DF123"),
            "id": "DF123",
        }
        _assert_invariant("cbre-dealflow", listing)
        row = _row(listing)
        assert row["external_id"] == "dealflow:DF123", (
            f"cbre-dealflow not prefixed correctly; got {row['external_id']!r}"
        )
        assert row["slug"] == "cbre"


class TestJllInvestor:
    """
    jll-investor entries fold into the 'jll' brokerage with prefix 'investor:'.
    """

    def test_investor_prefix(self):
        listing = {
            **_base("jll-investor", "https://www.jllinvestors.com/properties/INV456"),
            "id": "INV456",
        }
        _assert_invariant("jll-investor", listing)
        row = _row(listing)
        assert row["external_id"] == "investor:INV456", (
            f"jll-investor not prefixed correctly; got {row['external_id']!r}"
        )
        assert row["slug"] == "jll"


class TestNoIdFallback:
    """
    When a listing has no id field, both to_row and the monitor must derive
    'url:<sha1(url)[:16]>' identically.  Tests one source per prefix family.
    """

    @pytest.mark.parametrize("source_key, prefix", [
        ("cbre", ""),
        ("cbre-dealflow", "dealflow:"),
        ("jll-investor", "investor:"),
        ("colliers-main", "main:"),
        ("marcus-millichap", ""),
        ("svn", ""),           # no propertyId in URL, no id -> sha1 fallback
        ("lee-associates", ""),
    ])
    def test_no_id_sha1_fallback(self, source_key, prefix):
        url = f"https://example.com/listing/no-id-{source_key}"
        listing = {**_base(source_key, url)}
        # Ensure no "id" key leaks in
        listing.pop("id", None)
        _assert_invariant(source_key, listing)

        row = _row(listing)
        expected = prefix + "url:" + _sha1_url(url)
        assert row["external_id"] == expected, (
            f"[{source_key}] no-id fallback mismatch; got {row['external_id']!r}"
        )

    def test_fallback_is_deterministic(self):
        """Same URL always produces the same hash-based external_id."""
        url = "https://example.com/listing/stable-url"
        listing = {**_base("cbre", url)}
        row1 = _row(listing)
        row2 = _row(listing)
        assert row1["external_id"] == row2["external_id"]

    def test_different_urls_produce_different_keys(self):
        url_a = "https://example.com/listing/alpha"
        url_b = "https://example.com/listing/beta"
        row_a = _row({**_base("cbre", url_a)})
        row_b = _row({**_base("cbre", url_b)})
        assert row_a["external_id"] != row_b["external_id"]


class TestSourceToBrokerageCoverage:
    """
    Sanity: every SOURCE_TO_BROKERAGE key is recognized by to_row and produces
    a non-None row (given a minimal valid listing).  Catches accidental deletions
    or renames in the mapping.
    """

    @pytest.mark.parametrize("source_key", sorted(SOURCE_TO_BROKERAGE.keys()))
    def test_each_source_key_recognized(self, source_key):
        host = (
            "www.cushmanwakefield.com"
            if source_key == "cushman-wakefield"
            else "example.com"
        )
        listing = {
            "sourceKey": source_key,
            "url": f"https://{host}/listing/probe-{source_key}",
            "id": "PROBE1",
            "transactionMode": "sale",
        }
        row = _row(listing)
        assert row is not None, f"to_row returned None for source_key={source_key!r}"
        prefix = _prefix(source_key)
        expected = prefix + monitor_enum_key(source_key, listing)
        assert row["external_id"] == expected
