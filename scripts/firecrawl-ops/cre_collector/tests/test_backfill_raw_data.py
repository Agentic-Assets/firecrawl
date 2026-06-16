"""
test_backfill_raw_data.py

Locks the pure column-derivation in cre_backfill_raw_data.py: the one-time,
additive, idempotent Class-1 scalar backfill that reads stored cre_listings
raw_data and writes the now-mappable columns WITHOUT scraping.

Covers per source (per the gap doc per-source recoverable map):
  * derived columns from the nested raw_data objects (marcusSpecifications,
    rawSharpLaunch, rawNewmarkHit, jllDetail, transwesternFacts, availability[],
    publicPost, rawSavillsProperty, ...),
  * the dual-mode COALESCE(primary, secondary_pass, top-level) read,
  * the universal canonical_url <- raw_data->>'url',
  * the 6 data-quality guards (NAI POUND->USD, Lee per-SF conflation, AY
    $5000/SF/YR cap, dual-mode COALESCE, Transwestern Land Area unit validation,
    Newmark 'Subject to Offer' rejection),
  * COALESCE-keep generated SQL (never blanks a populated value), and
  * column-existence / to_regclass guards (a pre-sql/012 run is a no-op).

Pure Python, no DB, no network. The module imports cre_ingest only for the
shared loader/normalizers (not exercised against a live DB here).
"""

import json
import os

import cre_backfill_raw_data as bf

# ---------------------------------------------------------------------------
# Fixture loading. Each source agent contributes ONE scrubbed raw_data sample
# blob. For sources whose adapter listing object IS the raw_data (marcus, avison,
# newmark, jll, cushman, buildout, nai, savills), the fixture object itself is
# the blob. For transwestern/cbre/colliers the actual raw_data is under a
# "raw_data" key. raw_of() handles both shapes (mirrors cre_ingest storing
# `"raw_data": listing`).
# ---------------------------------------------------------------------------

_FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "raw_data")


def _load(name):
    with open(os.path.join(_FX, name)) as f:
        return json.load(f)


def raw_of(obj):
    """The raw_data blob for a fixture object (handles the two fixture shapes)."""
    return obj.get("raw_data", obj)


def _by_external_id(items, source_key):
    """{external_id-or-id: (columns, extra)} for a fixture list, keyed for lookup."""
    out = {}
    for o in items:
        key = o.get("external_id") or o.get("id") or raw_of(o).get("id")
        cols, extra = bf.derive_columns(source_key, raw_of(o))
        out[key] = (cols, extra)
    return out


# ===========================================================================
# Universal canonical_url (every source)
# ===========================================================================


def test_canonical_url_is_universal_across_sources():
    """raw_data->>'url' -> canonical_url for every source (gap doc: 92-100%)."""
    cases = [
        ("marcus-millichap.json", "marcus-millichap"),
        ("newmark.json", "newmark"),
        ("jll.json", "jll"),
        ("cbre.json", "cbre"),
        ("cushman-wakefield.json", "cushman-wakefield"),
        ("colliers.json", "colliers"),
        ("transwestern.json", "transwestern"),
    ]
    for fixture, default_src in cases:
        for o in _load(fixture):
            raw = raw_of(o)
            src = raw.get("sourceKey") or o.get("sourceKey") or default_src
            cols, _extra = bf.derive_columns(src, raw)
            assert cols.get("canonical_url") == raw["url"], f"{fixture}:{raw.get('id')}"
            assert cols["canonical_url"].startswith("http")


def test_canonical_url_http_guarded():
    cols, _ = bf.derive_columns("cbre", {"url": "not-a-url", "sourceKey": "cbre"})
    assert "canonical_url" not in cols
    cols, _ = bf.derive_columns("cbre", {"url": "javascript:alert(1)", "sourceKey": "cbre"})
    assert "canonical_url" not in cols


def test_canonical_url_nai_from_source_website_url():
    """NAI has no top-level 'url'; canonical_url <- sourceWebsiteUrl
    (== publicPost.urlOriginal), gap doc."""
    for o in _load("nai-global.json"):
        cols, _ = bf.derive_columns("nai-global", raw_of(o))
        assert cols["canonical_url"] == raw_of(o)["sourceWebsiteUrl"]
        assert cols["canonical_url"].startswith("http")


def test_canonical_url_avison_from_external_url():
    """AY has no top-level 'url'; canonical_url <- rawSharpLaunch.external_url
    (the avisonyoung.us canonical URL, not the SharpLaunch subdomain)."""
    for o in _load("avison-young.json"):
        cols, _ = bf.derive_columns("avison-young", raw_of(o))
        external = raw_of(o)["rawSharpLaunch"]["external_url"]
        assert cols["canonical_url"] == external
        assert "avisonyoung.us" in cols["canonical_url"]


# ===========================================================================
# Marcus & Millichap: marcusSpecifications nested object
# ===========================================================================


def test_marcus_specifications_scalar_lift():
    by = _by_external_id(_load("marcus-millichap.json"), "marcus-millichap")
    # Net-lease medical row.
    cols, _ = by["163445"]
    assert cols["cap_rate"] == 0.086                       # '8.60%' -> fraction
    assert cols["size_sf"] == 10815.0                      # 'Rentable SF'
    assert cols["sale_price_per_sf"] == 272.07             # 'Price/Gross SF' '$272.07'
    assert cols["lease_rate_type"] == "nnn"                # 'Triple Net (NNN)'
    assert cols["lease_rate_min"] == 23.40                 # 'Rent Per Square Feet' '$23.40'
    assert cols["tenant_name"] == "JenCare Senior Medical Center"
    assert cols["guarantor"] == "Subsidiary of a Corporation"
    assert cols["lease_years_remaining"] == 1.3


def test_marcus_multifamily_multiples_and_occupancy():
    by = _by_external_id(_load("marcus-millichap.json"), "marcus-millichap")
    cols, _ = by["177871"]
    assert cols["occupancy_rate"] == 0.875                 # '87.5%' -> 0.875 (from 0)
    assert cols["units"] == 28                             # 'Number of Units'
    assert cols["grm"] == 6.06                             # 'GRM'
    assert cols["price_per_unit"] == 56964.0               # 'Price/Unit' '$56,964'
    assert cols["cap_rate"] == 0.0628


def test_marcus_lot_size_acres_to_sf():
    by = _by_external_id(_load("marcus-millichap.json"), "marcus-millichap")
    cols, _ = by["128758"]
    # 'Lot Size' '3.83 acres' x43560 = 166834.8 (the frozen cre_parse factor).
    assert cols["lot_size_sf"] == 3.83 * bf.SQFT_PER_ACRE
    assert cols["year_built"] == 1979                      # 'Year Built'


# ===========================================================================
# Avison Young: rawSharpLaunch + the $5000/SF/YR sanity cap (DQ guard 3)
# ===========================================================================


def test_avison_availability_and_submarket():
    by = _by_external_id(_load("avison-young.json"), "avison-young")
    cols, _ = by["17952"]                                  # range availability
    assert cols["min_divisible_sf"] == 52435.0
    assert cols["max_divisible_sf"] == 127231.0
    assert cols["lease_rate_min"] == 4.95
    assert cols["lease_rate_max"] == 4.95
    assert cols["submarket"] == "Earth City"


def test_avison_anomalous_lease_rate_is_capped():
    """DQ guard 3: AY $7500/SF/YR-style anomaly is rejected by the >500 cap, so
    no lease_rate is staged (would otherwise pollute the board)."""
    by = _by_external_id(_load("avison-young.json"), "avison-young")
    cols, _ = by["18150"]                                  # availabilities_*_rent = 7500
    assert "lease_rate_min" not in cols
    assert "lease_rate_max" not in cols
    assert cols["year_built"] == 1982
    assert cols["units"] == 1


def test_avison_subtype_does_not_infer_building_class():
    """'office.medical'-style subtype carries property_subtype but NEVER a
    building_class (class is never inferred from a subtype)."""
    by = _by_external_id(_load("avison-young.json"), "avison-young")
    cols, _ = by["17808"]
    assert cols["property_subtype"] == "office.office_building"
    assert "building_class" not in cols


# ===========================================================================
# Newmark: rawNewmarkHit + 'Subject to Offer' rejection (DQ guard 6)
# ===========================================================================


def test_newmark_source_verbatim_geo_and_price():
    by = _by_external_id(_load("newmark.json"), "newmark")
    cols, _ = by["1751-yeager-ave-la-verne-sale"]
    assert cols["county"] == "Los Angeles"
    assert cols["submarket"] == "LA East"
    assert cols["market"] == "Los Angeles"
    assert cols["property_subtype"] == "Warehouse/Distribution"
    assert cols["sale_price_usd"] == 8585673.0             # '$8,585,673.00' parsed


def test_newmark_subject_to_offer_rejected():
    """DQ guard 6: sale_price 'Subject to Offer' / non-numeric -> no sale price."""
    by = _by_external_id(_load("newmark.json"), "newmark")
    cols, _ = by["2210-melson-ave-jacksonville-sale-1642678"]
    assert "sale_price_usd" not in cols
    assert cols["units"] == 4                              # number_of_units still lifts
    assert cols["county"] == "Duval"


def test_newmark_county_trimmed():
    by = _by_external_id(_load("newmark.json"), "newmark")
    cols, _ = by["100-west-lexington-street-baltimore-lease"]
    # ' Baltimore City' (leading space in the source) is trimmed.
    assert cols["county"] == "Baltimore City"


# ===========================================================================
# JLL: jllDetail.buildingClass / submarket / extra_facts
# ===========================================================================


def test_jll_building_class_and_submarket():
    for o in _load("jll.json"):
        src = o.get("sourceKey") or raw_of(o).get("sourceKey") or "jll"
        cols, extra = bf.derive_columns(src, raw_of(o))
        if src == "jll":
            assert cols["building_class"] == "B"
            assert cols["submarket"] == "Not Tracked Indiana"
            assert extra.get("location_description") == "Suburbs"
        elif src == "jll-investor":
            # investor row folds onto the jll deriver; dealType -> extra_facts.
            assert extra.get("deal_type") == "Property Sale"


# ===========================================================================
# Transwestern: facts + availability[] + Land Area unit guard (DQ guard 5)
# ===========================================================================


def test_transwestern_industrial_specs():
    by = _by_external_id(_load("transwestern.json"), "transwestern")
    cols, extra = by["1025-w-national-avenue"]
    assert cols["building_class"] == "B"
    assert cols["floors"] == 1                             # 'Stories'
    assert cols["year_built"] == 1984
    assert cols["clear_height_ft"] == 16.0
    assert cols["dock_doors"] == 1
    assert cols["drive_in_doors"] == 1                     # 'Grade Level Doors'
    assert cols["power_service"] == "1200a"
    assert cols["rail_served"] is False                    # 'Rail': 'No'


def test_transwestern_lease_availability_aggregation():
    by = _by_external_id(_load("transwestern.json"), "transwestern")
    cols, _ = by["455-kehoe-boulevard"]                    # two NNN lease rows
    assert cols["min_divisible_sf"] == 3036.0
    assert cols["max_divisible_sf"] == 3122.0
    assert cols["available_sf"] == 3036.0 + 3122.0         # sum over non-sale rows
    assert cols["lease_rate_min"] == 11.75
    assert cols["lease_rate_max"] == 11.75


def test_transwestern_land_area_acres_converts():
    """DQ guard 5: a small 'Land Area (ac)' value (29.2) is acres -> x43560."""
    by = _by_external_id(_load("transwestern.json"), "transwestern")
    cols, _ = by["seq-us-67-fm-2280"]
    assert cols["lot_size_sf"] == 29.2 * bf.SQFT_PER_ACRE
    assert cols["apn"] == "1"                              # 'Parcel'


def test_transwestern_land_area_sf_not_double_converted():
    """DQ guard 5: a large 'Land Area (ac)' value (29,185 looks like SF) is NOT
    x43560'd (would yield an absurd 1.2-billion-SF lot)."""
    by = _by_external_id(_load("transwestern.json"), "transwestern")
    cols, _ = by["2390-n-druid-hills-rd-ne"]
    assert cols["lot_size_sf"] == 29185.0                  # kept as-is, not x43560


# ===========================================================================
# CBRE / Cushman: leaseRateText parse + monthly annualization + (Annual) guard
# ===========================================================================


def test_cbre_monthly_lease_rate_annualized():
    for o in _load("cbre.json"):
        raw = raw_of(o)
        src = raw.get("sourceKey") or "cbre"
        cols, _ = bf.derive_columns(src, raw)
        if raw.get("id") == "US-SMPL-196821":
            assert cols["lease_rate_min"] == 43.08         # '3.59 USD/SF/MO' x12


def test_cushman_annual_token_rejected_but_per_sf_kept():
    by = {}
    for o in _load("cushman-wakefield.json"):
        cols, extra = bf.derive_columns("cushman-wakefield", raw_of(o))
        by[o["external_id"]] = (cols, extra)
    # '$30.00 (Annual) USD' -> the (Annual)-without-per-SF negative signal rejects it.
    annual_cols, _ = by["83b79d9ff18043739a6f950658f24697"]
    assert "lease_rate_min" not in annual_cols
    # '4.50/SF USD' -> trusted per-SF value.
    psf_cols, psf_extra = by["fc8ab88c3bbe4cb893135aa41e3dbdec"]
    assert psf_cols["lease_rate_min"] == 4.5
    assert psf_extra.get("is_investment_property") is True


# ===========================================================================
# Buildout (SVN + Lee): per-SF sale conflation (DQ guard 2) + acreage routing
# ===========================================================================


def test_lee_per_sf_sale_conflation_guard():
    """DQ guard 2: Lee salePriceUsd 12 with salePriceText '$12.00 /SF' is a per-SF
    rate, NOT an absolute price -> suppress sale_price_usd, route to per-SF."""
    cols = None
    for o in _load("buildout.json"):
        if o.get("id") == "1214325":                       # Lee per-SF sale
            cols, _ = bf.derive_columns("lee-associates", raw_of(o))
    assert cols is not None
    assert "sale_price_usd" not in cols                    # absolute price suppressed
    assert cols["sale_price_per_sf"] == 12.0               # routed to per-SF


def test_svn_acreage_routed_to_lot_and_subject_to_offer_rejected():
    by = {}
    for o in _load("buildout.json"):
        if o.get("_source") == "svn":
            by[o["id"]] = bf.derive_columns("svn", raw_of(o))
    # '9.15 Acres' sizeText -> lot_size_sf, not size_sf.
    cols, _ = by["2025495"]
    assert cols["lot_size_sf"] == 9.15 * bf.SQFT_PER_ACRE
    assert cols["sale_price_usd"] == 1500000.0
    # 'Subject To Offer' -> no sale price.
    cols2, _ = by["2019481"]
    assert "sale_price_usd" not in cols2


def test_svn_nnn_lease_rate():
    by = {}
    for o in _load("buildout.json"):
        if o.get("_source") == "svn":
            by[o["id"]] = bf.derive_columns("svn", raw_of(o))
    cols, _ = by["1933292"]                                # '$35 SF/yr (NNN)'
    assert cols["lease_rate_min"] == 35.0
    assert cols["lease_rate_type"] == "nnn"


# ===========================================================================
# NAI Global: POUND->USD currency-label guard (DQ guard 1) + tag class
# ===========================================================================


def test_nai_pound_label_treated_as_usd():
    by = {}
    for o in _load("nai-global.json"):
        cols, extra = bf.derive_columns("nai-global", raw_of(o))
        by[raw_of(o)["publicPost"]["id"]] = (cols, extra)
    # Sale: currency='POUND' but the value is USD (DQ guard 1).
    sale_cols, sale_extra = by[1601603]
    assert sale_cols["sale_price_usd"] == 1595000.0
    assert sale_cols["building_class"] == "B"              # 'BuildingClassB' tag
    assert sale_extra.get("listing_office") == "NAI Capital Commercial"
    # Lease: per-SF annual price + sizeRange divisibility.
    lease_cols, _ = by[1600383]
    assert lease_cols["lease_rate_min"] == 27.0
    assert lease_cols["min_divisible_sf"] == 1500.0
    assert lease_cols["max_divisible_sf"] == 12705.0


def test_nai_no_class_tag_yields_no_building_class():
    by = {}
    for o in _load("nai-global.json"):
        cols, _ = bf.derive_columns("nai-global", raw_of(o))
        by[raw_of(o)["publicPost"]["id"]] = cols
    cols = by[1602322]                                     # 'RetailAsset' tag only
    assert "building_class" not in cols
    assert cols["sale_price_usd"] == 6230416.0


# ===========================================================================
# Savills: AvailableSize.SqFt (non-zero) only
# ===========================================================================


def test_savills_available_sf_nonzero_only():
    by = {}
    for o in _load("savills.json"):
        cols, _ = bf.derive_columns("savills", raw_of(o))
        by[o["external_id"]] = cols
    assert by["5025923B-1E46-42E5-81C4-6F4316A8B02D"]["available_sf"] == 5139.0
    # SqFt = 0 must be suppressed (no available_sf staged).
    assert "available_sf" not in by["E4014DBE-336F-4AC8-BA9B-6ED1B48F73FE"]


# ===========================================================================
# Dual-mode COALESCE(primary, secondary_pass, top-level) (DQ guard 4)
# ===========================================================================


def test_dual_mode_primary_pass_is_read():
    """A dual sale+lease payload wrapped {primary, secondary_pass} must still
    derive from the nested pass (else ~6-8% of dual-mode rows drop)."""
    raw = {
        "primary": {
            "sourceKey": "newmark",
            "url": "https://www.nmrk.com/properties/dual-primary",
            "county": "Cook",
            "rawNewmarkHit": {"market": "Chicago", "submarket": "West Loop"},
        },
        "secondary_pass": {
            "sourceKey": "newmark",
            "url": "https://www.nmrk.com/properties/dual-secondary",
        },
    }
    cols, _ = bf.derive_columns("newmark", raw)
    assert cols["county"] == "Cook"
    assert cols["market"] == "Chicago"
    assert cols["submarket"] == "West Loop"
    # canonical_url prefers the primary pass url.
    assert cols["canonical_url"] == "https://www.nmrk.com/properties/dual-primary"


def test_dual_mode_falls_back_to_secondary_then_flat():
    """When the nested object lives only on secondary_pass, it is still found."""
    raw = {
        "primary": {"sourceKey": "marcus-millichap"},
        "secondary_pass": {
            "sourceKey": "marcus-millichap",
            "url": "https://www.marcusmillichap.com/properties/sec",
            "marcusSpecifications": {"Cap Rate": "7.00%", "Tenant Name": "Walgreens"},
        },
    }
    cols, _ = bf.derive_columns("marcus-millichap", raw)
    assert cols["cap_rate"] == 0.07
    assert cols["tenant_name"] == "Walgreens"
    assert cols["canonical_url"] == "https://www.marcusmillichap.com/properties/sec"


def test_dual_get_precedence_primary_wins():
    raw = {"primary": {"url": "p"}, "secondary_pass": {"url": "s"}, "url": "flat"}
    assert bf.dual_get(raw, "url") == "p"
    raw2 = {"secondary_pass": {"url": "s"}, "url": "flat"}
    assert bf.dual_get(raw2, "url") == "s"
    raw3 = {"url": "flat"}
    assert bf.dual_get(raw3, "url") == "flat"


# ===========================================================================
# Robustness: garbage inputs never throw
# ===========================================================================


def test_derive_columns_never_throws_on_garbage():
    assert bf.derive_columns("marcus-millichap", None) == ({}, {})
    assert bf.derive_columns("marcus-millichap", {}) == ({}, {})
    assert bf.derive_columns("transwestern", {"transwesternFacts": "not-a-dict"}) == ({}, {})
    assert bf.derive_columns("avison-young", {"rawSharpLaunch": 123}) == ({}, {})
    # An unknown source key yields only canonical_url (no deriver).
    cols, extra = bf.derive_columns("unknown-source", {"url": "https://x.example/y"})
    assert cols == {"canonical_url": "https://x.example/y"}
    assert extra == {}


def test_derive_columns_drops_non_target_and_empty():
    cols, extra = bf.derive_columns("marcus-millichap", {
        "url": "https://www.marcusmillichap.com/p/1",
        "marcusSpecifications": {"Tenant Name": "   "},  # whitespace -> dropped
    })
    assert "tenant_name" not in cols
    assert "canonical_url" in cols


# ===========================================================================
# Generated SQL: COALESCE-keep, scoped, guarded, never touches status
# ===========================================================================


def _staged(slug, fixture, source_key=None):
    rows = []
    for i, o in enumerate(_load(fixture)):
        raw = raw_of(o)
        sk = source_key or raw.get("sourceKey") or o.get("sourceKey") or slug
        rows.append((f"00000000-0000-0000-0000-0000000000{i:02d}", sk, raw))
    return bf.stage_rows_for_slug(rows)


def test_build_sql_is_coalesce_keep_never_blanks():
    sql = bf.build_sql("marcus-millichap", _staged("marcus-millichap", "marcus-millichap.json"))
    # Every scalar write is COALESCE(staged, current): a derived NULL keeps the
    # existing value, never blanks it.
    assert "cap_rate = COALESCE(s.cap_rate, t.cap_rate)" in sql
    assert "tenant_name = COALESCE(s.tenant_name, t.tenant_name)" in sql
    assert "canonical_url = COALESCE(s.canonical_url, t.canonical_url)" in sql


def test_build_sql_improvable_only_predicate():
    """The UPDATE only touches rows where a staged value would FILL a NULL
    (improvable-only), so a populated column is never re-touched."""
    sql = bf.build_sql("marcus-millichap", _staged("marcus-millichap", "marcus-millichap.json"))
    assert "(t.cap_rate IS NULL AND s.cap_rate IS NOT NULL)" in sql
    assert "(t.tenant_name IS NULL AND s.tenant_name IS NOT NULL)" in sql


def test_build_sql_is_scoped_to_brokerage_slug():
    sql = bf.build_sql("newmark", _staged("newmark", "newmark.json"))
    assert "JOIN credeals.cre_brokerages b ON b.slug = 'newmark'" in sql
    assert "t.brokerage_id = b.id" in sql
    assert "t.deleted_at IS NULL" in sql


def test_build_sql_column_existence_guarded_pre_migration_noop():
    """A pre-sql/012 DB (missing the institutional columns) runs nothing: the DO
    block checks every target column exists on cre_listings first, else RETURNs."""
    sql = bf.build_sql("marcus-millichap", _staged("marcus-millichap", "marcus-millichap.json"))
    assert "DO $$" in sql
    assert "information_schema.columns" in sql
    assert "to_regclass('credeals.cre_listings')" in sql
    assert "backfill skipped" in sql
    # The guard array lists the staged target columns.
    assert "'cap_rate'" in sql and "'tenant_name'" in sql


def test_build_sql_never_touches_status_or_deleted_at_or_txn_type():
    """Backfill is scalar-only: it never writes status, deleted_at, or
    transaction_type (status badges route to the OPT-IN gate, not here)."""
    sql = bf.build_sql("colliers", _staged("colliers", "colliers.json"))
    assert "status = " not in sql
    assert "deleted_at = " not in sql
    assert "transaction_type = " not in sql
    # The only deleted_at reference is the read-side scope guard, never an assignment.
    assert "SET" in sql  # sanity: an UPDATE was generated
    assert "DELETE FROM" not in sql


def test_build_sql_extra_facts_is_guarded_jsonb_merge():
    """extra_facts merges COALESCE(extra_facts,'{}') || staged, guarded so an
    empty staged blob is a no-op (never replaces the prior blob)."""
    # transwestern stages extra_facts (tw_* keys).
    sql = bf.build_sql("transwestern", _staged("transwestern", "transwestern.json"))
    assert "extra_facts = CASE WHEN s.extra_facts IS NOT NULL" in sql
    assert "|| s.extra_facts" in sql
    assert "ELSE t.extra_facts END" in sql


def test_build_sql_no_extra_facts_when_none_staged():
    """A run that derives no extra_facts (marcus) does not reference extra_facts
    in the SET / improvable predicate (so a pre-012 DB without the column is not
    falsely blocked)."""
    staged = _staged("marcus-millichap", "marcus-millichap.json")
    assert all(not r["extra"] for r in staged)  # marcus fixture stages no extra
    sql = bf.build_sql("marcus-millichap", staged)
    assert "|| s.extra_facts" not in sql
    # extra_facts not in the required-column guard array either.
    assert "'extra_facts'" not in sql


def test_build_sql_standard_conforming_strings_pinned():
    """Mirrors the SECURITY_REVIEW pin: COPY-based staging must pin
    standard_conforming_strings on so a backslash in staged text is literal."""
    sql = bf.build_sql("newmark", _staged("newmark", "newmark.json"))
    assert "SET LOCAL standard_conforming_strings = on;" in sql
    assert "\\set ON_ERROR_STOP on" in sql


# ===========================================================================
# Idempotency: re-deriving the same blob yields the same staged columns
# ===========================================================================


def test_derivation_is_deterministic_and_idempotent():
    for o in _load("transwestern.json"):
        raw = raw_of(o)
        a = bf.derive_columns("transwestern", raw)
        b = bf.derive_columns("transwestern", raw)
        assert a == b
