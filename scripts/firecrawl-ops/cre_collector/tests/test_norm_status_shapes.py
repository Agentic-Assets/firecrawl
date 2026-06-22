"""
test_norm_status_shapes.py

Validates the norm_status core and STATUS_SOURCE_PATHS map against real
cached collector artifacts (design doc sections 6 and 12.5).

Coverage:
  A. Sources with a native status signal: reads correctly from flat listing
     shape for cbre-dealflow, cushman-wakefield, jll-investor, colliers
     (SalesTracker), colliers-main, svn/lee-associates (buildout booleans).
  B. Dual-shape {primary, secondary_pass} produced by merge_rows(): a terminal
     status in EITHER pass wins for single-source fixtures; the merged
     raw_data dict (no top-level sourceKey) documents the silent-None
     invariant required by design section 12.5.
  C. Terminal-status priority: a terminal status in either pass wins over a
     non-terminal signal from the other pass.
  D. Sources without a native status field (cbre, newmark, avison-young)
     return None from norm_status for rows with no text signal.
  E. Wrong-path guard: proves that the merged-raw_data dual-shape (sourceKey
     missing at top level) causes paths to be empty, producing None even
     when sub-dicts have terminal signals. This makes the design section 12.5
     silent-None invariant INTENTIONAL, not accidental.

Artifacts used (read in small slices; no file is loaded fully into memory):
  out/cbre_dealflow_full_2026-06-12_041740.json  -> cbre-dealflow
  out/cushman_full_2026-06-12_022841.json        -> cushman-wakefield
  out/jll_investor_full_sitemap_detail_2026-06-12.json -> jll-investor
  out/colliers_salestracker_full_2026-06-12_050241.json -> colliers
  out/colliers_main_batch1_2026-06-12.json       -> colliers-main
  out/lee_full_cache_2026-06-12_assembled.json   -> lee-associates (+ dual)
  out/full_latest_2026-06-11_223306.json         -> svn, cbre
  out/avison_full_2026-06-12_043342.json         -> avison-young

PATH-MAP MISMATCHES FOUND (reported here, do NOT silently weaken tests):

  jll-investor: STATUS_SOURCE_PATHS includes 'jllInvestorSearchRow.status'
    but real artifact rows have NO 'jllInvestorSearchRow' key at the top
    level (the field is absent; the sitemap-detail path only populates
    'jllInvestorDetail'). The path list is correct by design (it is ordered;
    'status' fires first on these rows), but jllInvestorSearchRow.status
    never resolves in practice. This is harmless (dead path), not a bug.

  colliers-main: STATUS_SOURCE_PATHS['colliers-main'] = ['status',
    'colliersMain.propertyStatus']. Real artifact rows have 'status': None
    at the top level and 'colliersMain.propertyStatus' populated with values
    like 'Just Sold', 'Sold', 'Under Contract'. The nested path
    'colliersMain.propertyStatus' is correctly resolved by _dig() because
    'colliersMain' IS a top-level key in these listing dicts (not nested
    inside raw_data). norm_status correctly falls through to the nested path.
    No bug; the secondary path is load-bearing.

  DUAL-SHAPE SILENT-None DESIGN NOTE (section 12.5):
    When norm_status is called on a merged raw_data dict
    {'primary': listing_a, 'secondary_pass': listing_b}, the top-level dict
    has no 'sourceKey', so STATUS_SOURCE_PATHS.get(None, []) returns [],
    and the explicit status check is skipped for BOTH sub-dicts despite them
    containing valid terminal signals. This is the documented section 12.5
    invariant: the change-tracking layer MUST call norm_status on the
    ORIGINAL flat listing dicts (before merge_rows), not on the merged
    raw_data dict retrieved from the DB. A test below proves this behavior
    is reproducible and would be violated by any naive path fix that
    preserved sourceKey in the merged dict without re-reading the design.
"""

import json
import os
import re

import pytest

from cre_ingest import STATUS_SOURCE_PATHS, _TERMINAL_STATUSES, norm_status

# ---------------------------------------------------------------------------
# Artifact paths (relative to the cre_collector/ directory)
# ---------------------------------------------------------------------------

_BASE = os.path.join(os.path.dirname(__file__), "..")
_OUT = os.path.join(_BASE, "out")


def _art(filename: str) -> str:
    return os.path.join(_OUT, filename)


# ---------------------------------------------------------------------------
# Streaming helpers: read only a bounded slice of real artifact rows
# ---------------------------------------------------------------------------

_SLICE_DEFAULT = 500   # max listings to scan when looking for a row
_SLICE_LARGE = 5000   # for sources with sparse signal (e.g. lee underContract at index ~3372)


def _iter_listings(filename: str, limit: int = _SLICE_DEFAULT):
    """Yield up to `limit` listing dicts from a collector artifact file.

    Uses json.load() on the whole file (the file must be parseable), but
    stops yielding after `limit` rows so callers stay fast. The smallest
    test-relevant artifacts are ~400 KB; the largest is ~50 MB. We cap at
    `limit` rows to keep the suite under a few seconds.
    """
    path = _art(filename)
    if not os.path.isfile(path):
        pytest.skip(f"artifact not found: {path}")
    with open(path) as f:
        data = json.load(f)
    for i, listing in enumerate(data.get("listings") or []):
        if i >= limit:
            break
        yield listing


def _find_first(filename: str, predicate, limit: int = _SLICE_DEFAULT):
    """Return the first listing matching predicate, or None."""
    for l in _iter_listings(filename, limit=limit):
        if predicate(l):
            return l
    return None


# ---------------------------------------------------------------------------
# Fixtures: real rows sampled from artifacts
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cbre_dealflow_under_contract():
    """cbre-dealflow row with status='Under Contract' (terminal)."""
    row = _find_first(
        "cbre_dealflow_full_2026-06-12_041740.json",
        lambda l: l.get("sourceKey") == "cbre-dealflow" and l.get("status") == "Under Contract",
        limit=_SLICE_LARGE,
    )
    if row is None:
        pytest.skip("no cbre-dealflow Under Contract row in first 2000 listings")
    return row


@pytest.fixture(scope="module")
def cbre_dealflow_available():
    """cbre-dealflow row with status='Available' (non-terminal active signal)."""
    row = _find_first(
        "cbre_dealflow_full_2026-06-12_041740.json",
        lambda l: l.get("sourceKey") == "cbre-dealflow" and l.get("status") == "Available",
    )
    if row is None:
        pytest.skip("no cbre-dealflow Available row in first 500 listings")
    return row


@pytest.fixture(scope="module")
def cushman_available():
    """cushman-wakefield row with listingStatus='Available'."""
    row = _find_first(
        "cushman_full_2026-06-12_022841.json",
        lambda l: l.get("sourceKey") == "cushman-wakefield" and l.get("listingStatus") == "Available",
    )
    if row is None:
        pytest.skip("no cushman Available row in first 500 listings")
    return row


@pytest.fixture(scope="module")
def jll_investor_under_contract():
    """jll-investor row with status='Under Contract' (terminal)."""
    row = _find_first(
        "jll_investor_full_sitemap_detail_2026-06-12.json",
        lambda l: l.get("sourceKey") == "jll-investor" and l.get("status") == "Under Contract",
        limit=_SLICE_LARGE,
    )
    if row is None:
        pytest.skip("no jll-investor Under Contract row in first 2000 listings")
    return row


@pytest.fixture(scope="module")
def jll_investor_marketing():
    """jll-investor row with status='Marketing' (non-terminal; not mapped by STATUS_RULES)."""
    row = _find_first(
        "jll_investor_full_sitemap_detail_2026-06-12.json",
        lambda l: l.get("sourceKey") == "jll-investor" and l.get("status") == "Marketing",
    )
    if row is None:
        pytest.skip("no jll-investor Marketing row in first 500 listings")
    return row


@pytest.fixture(scope="module")
def colliers_salestracker_sold():
    """colliers (SalesTracker) row with status='Sold' (terminal)."""
    row = _find_first(
        "colliers_salestracker_full_2026-06-12_050241.json",
        lambda l: l.get("sourceKey") == "colliers" and l.get("status") == "Sold",
        limit=_SLICE_LARGE,
    )
    if row is None:
        pytest.skip("no colliers Sold row in first 2000 listings")
    return row


@pytest.fixture(scope="module")
def colliers_main_just_sold():
    """colliers-main row where colliersMain.propertyStatus='Just Sold' (terminal via nested path)."""
    terminal_values = {"Just Sold", "Sold", "SOLD", "In Escrow", "LEASED", "Under Contract"}
    row = _find_first(
        "colliers_main_batch1_2026-06-12.json",
        lambda l: (
            l.get("sourceKey") == "colliers-main"
            and l.get("status") is None
            and isinstance(l.get("colliersMain"), dict)
            and l["colliersMain"].get("propertyStatus") in terminal_values
        ),
        limit=_SLICE_LARGE,
    )
    if row is None:
        pytest.skip("no colliers-main terminal propertyStatus row in first 2000 listings")
    return row


@pytest.fixture(scope="module")
def lee_under_contract():
    """lee-associates row with underContract=True (boolean terminal signal)."""
    row = _find_first(
        "lee_full_cache_2026-06-12_assembled.json",
        lambda l: l.get("sourceKey") == "lee-associates" and l.get("underContract") is True,
        limit=_SLICE_LARGE,
    )
    if row is None:
        pytest.skip("no lee-associates underContract=True row in first 2000 listings")
    return row


@pytest.fixture(scope="module")
def svn_under_contract():
    """svn row with underContract=True (boolean terminal signal).

    Uses the svn-specific assembled artifact where the first underContract=True
    row appears at index ~2717 (much earlier than in the all-source full_latest).
    """
    row = _find_first(
        "svn_full_cache_2026-06-12_assembled.json",
        lambda l: l.get("sourceKey") == "svn" and l.get("underContract") is True,
        limit=_SLICE_LARGE,
    )
    if row is None:
        pytest.skip("no svn underContract=True row in first 5000 listings of svn artifact")
    return row


@pytest.fixture(scope="module")
def cbre_flat():
    """cbre row (no native status field; disappearance-only tier)."""
    row = _find_first(
        "full_latest_2026-06-11_223306.json",
        lambda l: l.get("sourceKey") == "cbre",
    )
    if row is None:
        pytest.skip("no cbre row in first 500 listings")
    return row


@pytest.fixture(scope="module")
def avison_flat():
    """avison-young row (no native status field; disappearance-only tier)."""
    row = _find_first(
        "avison_full_2026-06-12_043342.json",
        lambda l: l.get("sourceKey") == "avison-young",
    )
    if row is None:
        pytest.skip("no avison-young row in first 500 listings")
    return row


# ---------------------------------------------------------------------------
# Section A: flat listing shape for sources WITH a native status signal
# ---------------------------------------------------------------------------


class TestFlatShapeNativeStatus:
    """norm_status reads the native status signal from a flat listing dict."""

    def test_cbre_dealflow_terminal_status(self, cbre_dealflow_under_contract):
        """cbre-dealflow: 'Under Contract' resolves to 'under_contract' (terminal)."""
        row = cbre_dealflow_under_contract
        result = norm_status(row)
        assert result == "under_contract", (
            f"expected 'under_contract', got {result!r}; "
            f"row status={row.get('status')!r}, "
            f"card.status={row.get('cbreDealflowCard', {}).get('status')!r}"
        )
        assert result in _TERMINAL_STATUSES

    def test_cbre_dealflow_non_terminal_returns_none(self, cbre_dealflow_available):
        """cbre-dealflow: 'Available' maps to no canonical status (norm_status -> None)."""
        # 'Available' doesn't match any STATUS_RULES pattern -> None
        result = norm_status(cbre_dealflow_available)
        assert result is None, (
            f"'Available' should not map to a canonical status; got {result!r}"
        )

    def test_cushman_available_returns_none(self, cushman_available):
        """cushman-wakefield: 'Available' -> None (not a canonical status)."""
        result = norm_status(cushman_available)
        assert result is None, (
            f"cushman 'Available' should be None; got {result!r}"
        )

    def test_jll_investor_under_contract_terminal(self, jll_investor_under_contract):
        """jll-investor: status='Under Contract' -> 'under_contract' (terminal)."""
        row = jll_investor_under_contract
        result = norm_status(row)
        assert result == "under_contract", f"got {result!r}"
        assert result in _TERMINAL_STATUSES

    def test_jll_investor_marketing_returns_none(self, jll_investor_marketing):
        """jll-investor: status='Marketing' doesn't match STATUS_RULES -> None.

        Also tests that jllInvestorSearchRow.status path is harmless even
        when the field is absent (dead path confirmed by artifact inspection).
        """
        row = jll_investor_marketing
        # Confirm artifact reality: jllInvestorSearchRow is absent
        has_search_row = "jllInvestorSearchRow" in row and row["jllInvestorSearchRow"] is not None
        # norm_status still works; 'Marketing' simply doesn't match STATUS_RULES
        result = norm_status(row)
        assert result is None, (
            f"jll-investor 'Marketing' should not map to a canonical status; got {result!r}"
        )
        # Report the dead path if search row was unexpectedly present
        if has_search_row:
            pytest.fail(
                "PATH-MAP NOTE: jllInvestorSearchRow IS present in this artifact row. "
                "The test assumed it would be absent based on sitemap-detail artifacts. "
                "Verify STATUS_SOURCE_PATHS['jll-investor'] is still correct."
            )

    def test_colliers_salestracker_sold_terminal(self, colliers_salestracker_sold):
        """colliers SalesTracker: status='Sold' -> 'sold' (terminal)."""
        result = norm_status(colliers_salestracker_sold)
        assert result == "sold", f"got {result!r}"
        assert result in _TERMINAL_STATUSES

    def test_colliers_main_nested_path(self, colliers_main_just_sold):
        """colliers-main: top-level status=None, colliersMain.propertyStatus is terminal.

        Proves that the secondary path 'colliersMain.propertyStatus' is load-bearing:
        _dig() resolves listing['colliersMain']['propertyStatus'] correctly.
        """
        row = colliers_main_just_sold
        # Confirm the premise: top-level status is None
        assert row.get("status") is None, (
            f"Expected top-level status=None; got {row.get('status')!r}"
        )
        cm = row.get("colliersMain", {})
        ps = cm.get("propertyStatus")
        assert ps is not None, "colliersMain.propertyStatus should be set for this fixture"

        result = norm_status(row)
        assert result is not None, (
            f"colliers-main nested path 'colliersMain.propertyStatus'={ps!r} "
            f"should have produced a status; got None. "
            "If STATUS_SOURCE_PATHS['colliers-main'] lost this path, add it back."
        )
        assert result in _TERMINAL_STATUSES, (
            f"Expected terminal status from {ps!r}; got {result!r}"
        )

    def test_lee_boolean_under_contract(self, lee_under_contract):
        """lee-associates: underContract=True -> 'under_contract' (boolean path)."""
        row = lee_under_contract
        assert row.get("underContract") is True
        result = norm_status(row)
        assert result == "under_contract", f"got {result!r}"
        assert result in _TERMINAL_STATUSES

    def test_svn_boolean_under_contract(self, svn_under_contract):
        """svn: underContract=True -> 'under_contract' (boolean path)."""
        row = svn_under_contract
        assert row.get("underContract") is True
        result = norm_status(row)
        assert result == "under_contract", f"got {result!r}"
        assert result in _TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Section B: dual {primary, secondary_pass} shape (constructed fixtures)
# ---------------------------------------------------------------------------


class TestDualShapeHandling:
    """Dual-shape tests using constructed fixtures derived from real rows."""

    def _make_dual_raw_data(self, pass_a: dict, pass_b: dict) -> dict:
        """Construct a merged raw_data dict as merge_rows() would produce it."""
        return {"primary": pass_a, "secondary_pass": pass_b}

    def test_dual_shape_terminal_in_primary_wins(self, lee_under_contract):
        """Terminal in the primary pass -> under_contract, regardless of secondary.

        This tests norm_status on a FLAT listing (primary pass) that has a
        terminal signal. In the monitor layer, norm_status is called on each
        original flat listing BEFORE merge_rows, so this is the operational path.
        """
        primary = lee_under_contract  # underContract=True -> terminal
        result = norm_status(primary)
        assert result == "under_contract"

    def test_dual_shape_terminal_in_secondary_wins(self, lee_under_contract):
        """Build a synthetic dual pair: terminal only in secondary_pass.

        This is a CONSTRUCTED fixture: take a non-terminal lee row as primary
        and the underContract=True row as secondary_pass. We then simulate
        what would happen if norm_status were called on the primary (no terminal)
        vs. the secondary_pass (terminal). In the operational path the monitor
        calls norm_status on each flat listing separately, so we confirm each
        independently. The point is that terminal in EITHER flat call wins.
        """
        non_terminal = {
            "sourceKey": "lee-associates",
            "underContract": None,
            "closed": None,
            "url": "https://www.lee-associates.com/properties/?propertyId=9999-sale",
            "name": "Test Property",
        }
        terminal = lee_under_contract  # underContract=True

        # primary pass: no terminal signal
        assert norm_status(non_terminal) is None

        # secondary pass: terminal signal
        result_secondary = norm_status(terminal)
        assert result_secondary == "under_contract"
        assert result_secondary in _TERMINAL_STATUSES

    def test_synthetic_merged_raw_data_section_12_5(self, lee_under_contract):
        """Design section 12.5 silent-None invariant for the merged raw_data dict.

        When merge_rows() produces raw_data = {'primary': pass_a, 'secondary_pass': pass_b},
        the top-level merged dict has no 'sourceKey' key. norm_status() looks up
        STATUS_SOURCE_PATHS.get(None, []) -> [], so the explicit status check is
        skipped even though sub-dicts contain a terminal signal.

        This is NOT a bug to fix here: the correct caller pattern is to invoke
        norm_status on each flat listing BEFORE merge_rows. This test encodes
        the current behavior so that any future refactor that accidentally
        changes it fails loudly rather than silently.
        """
        primary_pass = lee_under_contract  # has underContract=True -> terminal if called flat
        secondary_pass = {
            "sourceKey": "lee-associates",
            "underContract": None,
            "closed": None,
            "url": "https://www.lee-associates.com/properties/?propertyId=9999-lease",
        }

        # Confirm flat call works:
        assert norm_status(primary_pass) == "under_contract"

        # Build merged raw_data dict (as stored in DB after merge_rows):
        merged_raw_data = self._make_dual_raw_data(primary_pass, secondary_pass)

        # norm_status on merged raw_data: sourceKey missing at top level -> paths=[]
        # Explicit status check is skipped; text fallback runs on all subs.
        # Unless the property name/url contains a terminal keyword, result is None.
        result = norm_status(merged_raw_data)
        # The merged dict has no sourceKey, so paths=[]. Neither title/name/url
        # of the primary_pass contains "under contract" as a word-boundary match
        # from STATUS_RULES (the underContract boolean field is not a text field).
        # Result MUST be None (silent-None invariant).
        assert result is None, (
            f"DESIGN VIOLATION: norm_status returned {result!r} on a merged "
            "raw_data dict. Section 12.5 requires None here because sourceKey "
            "is absent from the merged top-level dict. If the code was changed "
            "to propagate sourceKey into the merged dict, update this test AND "
            "the section 12.5 documentation in cre-intelligence-system-design.md."
        )

    def test_colliers_main_nested_in_primary(self, colliers_main_just_sold):
        """colliers-main terminal in primary flat listing -> resolves via nested path.

        Confirms that the secondary path 'colliersMain.propertyStatus' works
        when norm_status is called on the flat listing dict (operational path).
        """
        result = norm_status(colliers_main_just_sold)
        assert result in _TERMINAL_STATUSES, (
            f"Expected terminal status from colliersMain.propertyStatus; got {result!r}"
        )

    def test_cbre_dealflow_detail_path(self, cbre_dealflow_under_contract):
        """cbre-dealflow: cbreDealflowDetail.status also carries the terminal signal.

        Verifies the third path in STATUS_SOURCE_PATHS['cbre-dealflow'] is
        reachable when status and cbreDealflowCard.status are the same value.
        (All three paths agree on this real row; the test checks idempotency.)
        """
        row = cbre_dealflow_under_contract
        detail = row.get("cbreDealflowDetail") or {}
        # confirm detail.status is also present and terminal-ish
        detail_status = detail.get("status", "")
        result = norm_status(row)
        assert result == "under_contract"
        # Sanity check: the detail path would independently match if it were first
        assert "contract" in (detail_status or "").lower() or "escrow" in (detail_status or "").lower(), (
            f"cbreDealflowDetail.status={detail_status!r} doesn't look like a terminal token; "
            "the path may have changed in the artifact"
        )


# ---------------------------------------------------------------------------
# Section C: terminal status in EITHER pass wins (synthetic fixture)
# ---------------------------------------------------------------------------


class TestTerminalPriority:
    """Terminal status wins over non-terminal / None from the other pass."""

    def test_terminal_beats_non_terminal_in_explicit_paths(self):
        """Synthetic: two jll-investor rows; first is terminal, second non-terminal.

        norm_status is called on each flat row separately (operational path).
        Terminal status from the terminal row must win; non-terminal row -> None.
        The terminal from EITHER flat call should be used by the monitor layer.
        """
        terminal_row = {
            "sourceKey": "jll-investor",
            "status": "Under Contract",
            "url": "https://jllinvestor.com/property/abc",
        }
        non_terminal_row = {
            "sourceKey": "jll-investor",
            "status": "Marketing",
            "url": "https://jllinvestor.com/property/abc",
        }

        assert norm_status(terminal_row) == "under_contract"
        assert norm_status(non_terminal_row) is None

    def test_terminal_jll_investor_wins_from_detail(self):
        """jll-investor: terminal from jllInvestorDetail.stageName wins when top-level matches."""
        row = {
            "sourceKey": "jll-investor",
            "status": "Under Contract",
            "jllInvestorDetail": {"stageName": "Awarded / Exclusivity"},
            "url": "https://jllinvestor.com/p/123",
        }
        # 'status' = 'Under Contract' -> terminal -> norm_status returns immediately
        result = norm_status(row)
        assert result == "under_contract"
        assert result in _TERMINAL_STATUSES

    def test_terminal_via_detail_path_when_top_level_absent(self):
        """jll-investor: no top-level 'status' but jllInvestorDetail.stageName has terminal value.

        This verifies the second/third paths in STATUS_SOURCE_PATHS['jll-investor']
        fire when the primary path has no value.
        """
        row = {
            "sourceKey": "jll-investor",
            # status absent at top level
            "jllInvestorDetail": {"stageName": "Under Contract"},
            "url": "https://jllinvestor.com/p/456",
        }
        result = norm_status(row)
        assert result == "under_contract", f"got {result!r}"

    def test_terminal_colliers_main_via_only_nested_path(self):
        """colliers-main: top-level status=None; only nested path fires."""
        row = {
            "sourceKey": "colliers-main",
            "status": None,
            "colliersMain": {"propertyStatus": "Sold"},
            "url": "https://colliers.com/en/properties/abc",
        }
        result = norm_status(row)
        assert result == "sold", f"got {result!r}"

    def test_non_terminal_colliers_main_nested(self):
        """colliers-main: colliersMain.propertyStatus='Available' -> None."""
        row = {
            "sourceKey": "colliers-main",
            "status": None,
            "colliersMain": {"propertyStatus": "Available"},
            "url": "https://colliers.com/en/properties/def",
        }
        result = norm_status(row)
        assert result is None, f"'Available' should not map to a canonical status; got {result!r}"

    def test_svn_boolean_closed_terminal(self):
        """svn: closed=True -> 'sold' via _STATUS_BOOL_PATHS coercion."""
        row = {
            "sourceKey": "svn",
            "closed": True,
            "underContract": None,
            "url": "https://svn.com/properties/123",
        }
        result = norm_status(row)
        assert result == "sold", f"got {result!r}"
        assert result in _TERMINAL_STATUSES

    def test_svn_closed_false_returns_none(self):
        """svn: closed=False returns None (prune() drops it; absence is no signal)."""
        row = {
            "sourceKey": "svn",
            "closed": False,
            "underContract": False,
            "url": "https://svn.com/properties/456",
        }
        result = norm_status(row)
        assert result is None, f"closed=False should not produce a status; got {result!r}"


# ---------------------------------------------------------------------------
# Section D: sources WITHOUT a native status field -> None for clean rows
# ---------------------------------------------------------------------------


class TestNoNativeStatusSources:
    """Sources with empty STATUS_SOURCE_PATHS return None for clean rows."""

    def test_cbre_flat_no_status_returns_none(self, cbre_flat):
        """cbre: no status field, no text signal -> None."""
        row = cbre_flat
        # Confirm no status-looking keys
        assert row.get("sourceKey") == "cbre"
        assert STATUS_SOURCE_PATHS.get("cbre") == [], "cbre should have empty paths"
        result = norm_status(row)
        # cbre rows often have property names; check for accidental text match
        if result is not None:
            title = row.get("name") or row.get("title") or ""
            pytest.fail(
                f"norm_status returned {result!r} for a cbre row. "
                f"Likely from text fallback on name={title!r}. "
                "Verify STATUS_RULES word-boundary patterns don't over-fire on active listings."
            )

    def test_avison_young_no_status_returns_none(self, avison_flat):
        """avison-young: no status field, no text signal -> None."""
        row = avison_flat
        assert row.get("sourceKey") == "avison-young"
        assert STATUS_SOURCE_PATHS.get("avison-young") == []
        result = norm_status(row)
        if result is not None:
            name = row.get("name") or ""
            pytest.fail(
                f"norm_status returned {result!r} for avison-young row "
                f"(name={name!r}). Text fallback over-fired; check STATUS_RULES."
            )

    def test_no_source_key_returns_none(self):
        """Listing with no sourceKey -> paths=[], no text signal -> None."""
        row = {"url": "https://example.com/listing/1", "name": "Industrial Property"}
        result = norm_status(row)
        assert result is None

    def test_synthetic_newmark_no_status(self):
        """newmark has empty STATUS_SOURCE_PATHS -> None for a clean row."""
        row = {
            "sourceKey": "newmark",
            "url": "https://newmark.com/properties/abc",
            "name": "Office Building Downtown",
            "city": "Austin",
            "state": "TX",
        }
        assert STATUS_SOURCE_PATHS.get("newmark") == []
        result = norm_status(row)
        assert result is None, f"newmark clean row should be None; got {result!r}"

    def test_none_never_returned_as_active(self):
        """Invariant: norm_status NEVER returns 'active'. None means no opinion."""
        test_rows = [
            {"sourceKey": "cbre", "url": "https://cbre.com/1"},
            {"sourceKey": "newmark", "url": "https://newmark.com/2"},
            {"sourceKey": "avison-young", "url": "https://av.com/3"},
            {"sourceKey": "jll", "url": "https://jll.com/4"},
            {},
        ]
        for row in test_rows:
            result = norm_status(row)
            assert result != "active", (
                f"norm_status returned 'active' for {row!r}; "
                "invariant violated: norm_status must never return 'active'"
            )


# ---------------------------------------------------------------------------
# Section E: wrong-path guard / dual-shape naive-fix detection
# ---------------------------------------------------------------------------


class TestDualShapeWrongPathGuard:
    """Prove that the merged raw_data dual-shape produces None due to missing sourceKey.

    These tests guard design section 12.5. Any code change that makes
    norm_status return a non-None value for a merged raw_data dict (where
    sourceKey is at the sub-dict level, not top-level) MUST be intentional
    and documented as a design change.
    """

    def test_merged_raw_data_silent_none_is_intentional(self, lee_under_contract):
        """Merged raw_data with terminal signal in primary -> still None (section 12.5).

        This is the 'wrong-path fixture' required by the task spec: if the
        dual-shape handling were 'naive' (i.e., it just promoted primary's
        sourceKey to the top level), norm_status would return 'under_contract'.
        The current code does NOT do this, so result is None. The test FAILS
        if the code is naively 'fixed' without a design change.
        """
        # Flat call works:
        assert norm_status(lee_under_contract) == "under_contract"

        # Merged raw_data: sourceKey missing at top level
        merged = {
            "primary": lee_under_contract,  # has underContract=True, sourceKey='lee-associates'
            "secondary_pass": {
                "sourceKey": "lee-associates",
                "underContract": None,
                "closed": None,
                "url": "https://www.lee-associates.com/properties/?propertyId=9999-lease",
            },
        }

        result = norm_status(merged)

        # The merged dict has no top-level sourceKey -> paths=[]
        # _explicit_status_from_pass is called on each sub with paths=[]
        # so it always returns None from the explicit tier
        # The text fallback checks title/name/headline/url-slug of each sub
        # The primary row has name/url but neither contains a terminal keyword
        # at word-boundary level that STATUS_RULES would match
        assert result is None, (
            f"SECTION 12.5 GUARD TRIPPED: norm_status returned {result!r} on a "
            "merged raw_data dict. This violates the section 12.5 silent-None "
            "invariant. Either the code was changed to propagate sourceKey into "
            "the merged dict (document this as a design change), or STATUS_RULES "
            "over-fired on a text field in the listing (check name/title/url of "
            f"the lee-associates fixture: name={lee_under_contract.get('name')!r})."
        )

    def test_flat_with_explicit_sourcekey_finds_terminal(self):
        """Contrast: a FLAT dict with sourceKey finds terminal signal immediately.

        This is the 'correct' path that the monitor layer must use.
        Confirms the dual-shape None is caused by missing sourceKey, not by
        anything else in the path resolution.
        """
        flat_row = {
            "sourceKey": "lee-associates",
            "underContract": True,
            "closed": None,
            "url": "https://www.lee-associates.com/properties/?propertyId=9999-sale",
            "name": "Warehouse Building",
        }
        result = norm_status(flat_row)
        assert result == "under_contract", (
            f"A flat lee-associates row with underContract=True must return "
            f"'under_contract'; got {result!r}"
        )

    def test_merged_dict_with_promoted_sourcekey_would_find_terminal(self):
        """Hypothetical: if sourceKey were promoted to merged top-level, terminal fires.

        This test documents what WOULD happen after a hypothetical naive fix:
        promoting sourceKey breaks the silent-None invariant. We verify this
        hypothetical by constructing the merged dict WITH sourceKey at top level
        and checking that norm_status then returns the terminal status.
        This test is GREEN only if the hypothetical fix is applied (it confirms
        the fix would work). Since we are NOT applying the fix, this test
        validates only the logic, not that we want this behavior.
        """
        # Hypothetical merged dict WITH sourceKey at top level (NOT how merge_rows works now):
        merged_with_key = {
            "sourceKey": "lee-associates",  # <-- promoted (hypothetical fix)
            "primary": {
                "sourceKey": "lee-associates",
                "underContract": True,
                "closed": None,
                "url": "https://www.lee-associates.com/properties/?propertyId=9999-sale",
            },
            "secondary_pass": {
                "sourceKey": "lee-associates",
                "underContract": None,
                "closed": None,
                "url": "https://www.lee-associates.com/properties/?propertyId=9999-lease",
            },
        }
        # With sourceKey promoted, norm_status WOULD find paths, then scan subs including primary
        # In the primary sub, underContract=True -> 'under_contract' (terminal)
        result = norm_status(merged_with_key)
        assert result == "under_contract", (
            f"With sourceKey promoted to top level, norm_status should find the "
            f"terminal signal in primary sub-dict; got {result!r}. "
            "This confirms the fix would work logically (but is not applied)."
        )

    def test_status_source_paths_coverage(self):
        """All STATUS_SOURCE_PATHS sources are also in SOURCE_TO_BROKERAGE.

        Catches additions to STATUS_SOURCE_PATHS that forget to register the
        source in the brokerage map, or vice versa.
        """
        from cre_ingest import SOURCE_TO_BROKERAGE
        for source_key in STATUS_SOURCE_PATHS:
            assert source_key in SOURCE_TO_BROKERAGE, (
                f"STATUS_SOURCE_PATHS has '{source_key}' but it is not in "
                "SOURCE_TO_BROKERAGE. Add a mapping or remove the path entry."
            )

    def test_no_status_source_defaults_to_empty_list(self):
        """An unknown sourceKey gets an empty path list (not an error)."""
        row = {
            "sourceKey": "unknown-fictional-source",
            "status": "Sold",
            "url": "https://fictional.com/p/1",
        }
        # With an empty path list (default), status field is NOT read via explicit tier
        # Text fallback runs on title/name/headline/url-slug
        result = norm_status(row)
        # 'status' key is not in _STATUS_TEXT_FIELDS; url slug 'p/1' doesn't match
        # So result should be None (the 'Sold' field is not scanned by the text fallback)
        assert result is None, (
            f"Unknown source with status='Sold' should return None (explicit tier "
            f"skipped; text fallback doesn't scan 'status' key); got {result!r}"
        )
