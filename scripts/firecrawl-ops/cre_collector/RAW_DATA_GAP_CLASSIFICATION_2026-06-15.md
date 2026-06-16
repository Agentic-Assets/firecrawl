# raw_data gap classification (2026-06-15)

Source: read-only Supabase audit (project `fhqycqubkkrdgzswccwd`, schema
`credeals`), 12 per-brokerage probes + document-corpus audit. This is the
source-of-truth spec for the Phase-2 data-lift build (free scalar backfill,
OM/PDF parse tier, new columns, geo-derivation + DQ guards).

Three gap classes:
- **Class 1 (free-to-map):** value is already in `raw_data`, column is empty.
  One-time SQL backfill of existing rows + adapter forward-mapping. No scrape.
- **Class 2 (needs detail/OM):** not in `raw_data`; on the detail page or in
  the brochure/OM PDF. Handled by the harvest/enrich path and the OM-parse tier.
- **Class 3 (unpublished):** broker does not disclose (POA, sale price on lease).
  Unrecoverable.

The forward path (new/re-enriched rows) flows adapter -> JSON artifact ->
`cre_ingest.py` field->column map. The backfill path (existing 87k rows) reads
`raw_data` directly in Python and writes columns; it does NOT go through the
adapter, so it is self-contained and is the biggest immediate coverage jump.

`canonical_url` is universal: column 0% today, `raw_data->>'url'` present
92-100% on every source. One rename, board-wide ~95%. Do it everywhere.

Every dual-mode (sale_or_lease) source stores the real listing under
`raw_data->'primary'` AND `raw_data->'secondary_pass'` with NO usable top-level
keys (~6-8% of rows on colliers-main, lee, svn, avison-young, transwestern).
Any mapping/backfill MUST `COALESCE(raw_data->'primary', raw_data->'secondary_pass', raw_data)`
or it silently drops those rows.

## Per-source Class-1 recoverable map (column is ~0% today unless noted)

### Marcus & Millichap (3,124 rows) — nested object `raw_data->'marcusSpecifications'`
Single highest-value unmapped object (investment-sales feed; values are
pre-formatted strings, parse/strip before cast).
- `cap_rate` <- `'Cap Rate'` (or top-level `capRatePct`), ~70.8%
- `occupancy_rate` <- `'Occupancy'` ('87.5%' -> 0.875), ~21.7% (from 0)
- `size_sf` <- `'Rentable SF'` (fallback `'Gross SF'`), ~81%
- `sale_price_per_sf` <- `'Price/Gross SF'` ('$272.07'), ~53.3%
- `lot_size_sf` <- `'Lot Size'` ('3.83 acres' x43560), ~38.4%
- `units` <- `'Number of Units'`, ~28.8%
- `lease_rate_type` <- `'Lease Type'` ('Triple Net (NNN)'), ~26.7%
- `lease_rate_min`/`max` <- `'Rent Per Square Feet'` ('$23.40'), ~24.8% (in-place tenant rent, not asking)
New-column candidates (no home today): `'Tenant Name'` (26.3%), `'Guarantor'`
(26.7%), `'Years Remaining On Lease'` (24.5%), `'GRM'` (20.3%), `'Price/Unit'`
(21.4%), `'Price/Acre'` (7.1%), `'RevPAR'`/`'Price/Room'`/`'Number of Rooms'`
(hotels), `'Buildable Square Feet'` (land). Broker `license` string in
`contactsDetailed[]` ('License(s): IL: 475.188007'). `gatedDocuments[]` = OM/Deal
Room (gated:true) -> OM-parse signal. `activityId` stable source id.

### Avison Young (2,201 rows) — nested object `raw_data->'rawSharpLaunch'`
- `available_sf` / `min_divisible_sf` <- `availabilities_min_surface_sqft` (mirror top-level `availableMinSqft`), ~46.1%
- `max_divisible_sf` <- `availabilities_max_surface_sqft` (mirror `availableMaxSqft`), ~46.1%
- `lease_rate_min`/`max` <- `availabilities_min_rent` / `availabilities_max_rent` (cleaner than leaseRateText), ~22.9%
- `submarket` <- `submarket`, ~20.7%
- `year_built` <- top-level `yearBuilt`, ~23.3%
- `cap_rate` <- top-level `capRatePct` (mirror `cap_rate`), ~1.5%
- `sale_price_per_sf` <- top-level `saleUnitPrice` (mirror `sale_unit_price`), ~6.1%
- `units` <- `units`, ~5.6%
- `lease_rate_type` <- parse `leaseRateText` suffix ('/SF/YR' -> psf_yr); `detailJsonLd->offers->businessFunction`
DQ guard: anomalous rates like `$5000/SF/YR` (likely monthly/total mislabeled) -> sanity-cap. New columns: building class via `assetType`/`rawSubtypes` ('office.medical'); `on_market_at`/`off_market_at` lifecycle; broker `contactsDetailed[]`.

### Newmark (4,371 rows) — nested object `raw_data->'rawNewmarkHit'` (Algolia index)
- `county` <- top-level `county` (mirror `rawNewmarkHit.county`), ~99.9% (from 0)
- `submarket` <- top-level `submarket`, ~47.4%
- `market` <- `rawNewmarkHit.market`, ~47.2%
- `units` <- `rawNewmarkHit.number_of_units`, ~22.2%
- `description` <- `headline` (col already mapped; verify), ~99.9%
- `sale_price_usd` <- `rawNewmarkHit.sale_price` ('$8,585,673.00'); strip $/commas, reject 'Subject to Offer', ~12.7% parseable
New columns: **status badge** `rawNewmarkHit.status` 100% ('For Sale'/'For Lease'/'Under Contract') -> feeds status activation WITHOUT scraping; `property_subtype` 100% ('Warehouse/Distribution'); `contactsDetailed[]` + `newmarkBrokerProvenance`. needsScrape: lease rate, cap_rate, noi, occupancy, year_built, zoning, floors, parking, term.

### JLL (11,675 rows) — nested object `raw_data->'jllDetail'` (85.5%) + `jllInvestorDetail` (8%)
- `submarket` <- `jllDetail.submarket`, ~85.1% (from 0)
- `building_class` (new col) <- `jllDetail.buildingClass` ('A'/'B'/'C'), ~85.4%
- `highlights` <- `jllDetail.highlights[].title` UNION `jllInvestorDetail.highlights` (HTML), ~34%
- `amenities` <- `jllDetail.amenities` (["Dock","Fenced Lot",...]), ~4.5%
- `updated_date` <- `jllInvestorDetail.dateModified` (only ~8%, investor subset; 92% non-investor have NO date -> capped, not broadly recoverable)
New columns: status/stage `jllInvestorDetail.stageName`/`isUnderContract`/top-level `status` (8%); `dealType`; `locationDescription` (Suburbs/Urban, 32%); full broker roster `contactsDetailed` (93.2%: name/email/phone/title/office/license/profileUrl). `brochures` + `jllInvestorDetail.documentsCA` -> OM source. needsScrape: price, lease rate, cap_rate, noi, occupancy, units, floors, parking, year_built, zoning, market, county, term (only in free-text description/highlights).

### Transwestern (2,021 rows) — `raw_data->'transwesternFacts'` + `raw_data->'availability'[]`
NOTE: my prior media build already lifts some Transwestern facts; reconcile, do not double-map.
- `floors` <- `transwesternFacts.Stories`, ~82.8% (from 0)
- `year_built` <- `transwesternFacts.'Year Built'`, ~72.4%
- `min_divisible_sf`/`max_divisible_sf` <- min/max over `availability[].size` (comma-stripped), ~64.2%
- `available_sf` <- sum over `availability[].size` where type NOT ILIKE '%sale%', ~61%
- `lease_rate_min`/`max` <- min/max over `availability[].rate` where lease and rate<1000 psf, ~15%
- `lease_rate_type` <- net token in `availability[].raw[]` (FSG/NNN/MG/IG/'Absolute Net'); token index VARIES, match against vocabulary, do not hardcode index, ~21.3%
- `lot_size_sf` <- `transwesternFacts.'Land Area (ac)'` x43560 — UNIT INCONSISTENT (sample '29,185' looks like SF); validate before convert
New columns: building class `transwesternFacts.Class` (70.1%); clear height (3.3%); docks/grade doors/power/rail/yard/crane (industrial); `Parcel`/APN (2.9%); `Year Renovated`/`Typical Floor Size`/`Elevators`. needsScrape: cap_rate, noi, occupancy, units, parking ratio, term, market/submarket/county. `rawTranswesternFeed.Price` is 0 placeholder -> NOT a price source.

### CBRE (20,864 rows) — flat `cbre` feed (92%) + `cbre-dealflow` subset (8.8%)
- `canonical_url` <- `url`, 92%
- `lease_rate_min`/`max`/`type` <- parse `leaseRateText` ('3.59 USD/...'), ~18.2%
- `highlights` <- `headline`, ~63.5% (col 28.2%)
Already mapped (NOT gaps; do not redo): description 63.3%, size_sf 74.9%, updated_date 91.2%, sale_price 11.4%, lot_size 5.0% (from lotSizeAcres). New columns on dealflow subset: `contactsDetailed` w/ phone+`cbreContactId`; `status` ('Available'); `cbreDealflowDetail.projectType` ('Value Add'). needsScrape: cap_rate, noi, occupancy, units, floors, parking, divisible_sf, term, year_built, zoning, market/submarket/county, amenities (only sporadic in free-text description).

### Cushman & Wakefield (11,318 rows) — flat list-API feed
- `canonical_url` <- `url`, 100%
- `lease_rate_min` <- parse `leaseRateText` ('$30.60 (Annual) USD'), ~32.6%
- `lease_rate_type` <- '(Annual)'/per-SF token, ~27.3%
- `lease_rate_max` <- '$a - $b' range high, ~3.4%
Already mapped: description 53.2%, updated_date 95.6%, size_sf 92.3%, year_built 74.2%, lot_size 16.1%, sale_price 9.9%. `detailScrape` holds ONLY scrape metadata (rawHtmlLength/markdownLength) — the detail HTML was fetched but never parsed; cap_rate/NOI/occupancy/spaces/units/parking/clear-height/zoning/divisibility/term live in that un-parsed HTML (Class-2, harvest/enrich target). New cols: `contactsDetailed` (name/phone/company/vcard/profile), `is_investment_property` (9.5%), sublease badge `headline`/`attribute1`, `listingStatus` ('Available').

### SVN (5,287 rows) — flat Buildout feed
- `canonical_url` <- `url`, 95.5%
- `lease_rate_min`/`max` <- parse `leaseRateText` ranges ('$35 - 45 SF/yr (NNN)'), ~36.1%
- `lease_rate_type` <- trailing parenthetical (NNN/NN/MG/Gross/Ground), ~32.3%
- `lot_size_sf` <- `sizeText` rows ending 'Acres' x43560 (route acreage to lot not bldg), ~22.5%
New cols: `underContract` bool (5.6%) -> status; broker ids. needsScrape: description, year_built, zoning, cap_rate, noi, occupancy, units, floors, parking, term, market/submarket, amenities, highlights, address2 -> brochures (69.9%) are the OM path.

### Lee & Associates (9,223 rows) — flat Buildout feed
- `canonical_url` <- `COALESCE(url, primary->>'url')`, 100%
- `lease_rate_type` <- parse `leaseRateText` ('$19 SF/yr ($10.00/SF NNN)'; basis SF/yr vs SF/month; NNN/MG/Gross; dual shell-vs-office rates), ~35%
- `lease_rate_max` <- range high ('$1.59 - 1.70 SF/month'), ~4.8%
DQ guard: `salePriceUsd` CONFLATES absolute price and per-SF rate ('$6.00/SF' stored as salePriceUsd:6; '$0' placeholders) -> guard by checking `salePriceText` for '/ SF' before trusting. needsScrape (no nested data): description, cap_rate, noi, occupancy, units, floors, parking, term, year_built, zoning, market/submarket/county, amenities, highlights, address2 -> brochures (82.6%) OM path.

### Colliers (17,001 rows) — `colliers-main` (sitemap) + `colliers` SalesTracker subset
- `canonical_url` <- `COALESCE(url, primary->>'url', secondary_pass->>'url')`, 100%
- `lease_rate_type` <- parse `leaseRateText` (NNN/Gross/MG/FSG + /SF /yr /mo); only ~6.4% carry an explicit token (low yield)
Already mapped: description 96.5%, updated_date 93.1%, size_sf 27.7%, lot_size 23.9%, sale_price 24.1%, lease_rate_min 20.6%, year_built 2.6% (source-capped). New cols: status `colliersMain.propertyStatus`/SalesTracker `status` (14.7%); broker `contactsDetailed` w/ phone (97%) + license (1.1%); SalesTracker `brochureUrl`+`agreementUrl` (gated). needsScrape: cap_rate, noi, occupancy, units, floors, parking, term, zoning, market/submarket/county, amenities, highlights, address2.

### NAI Global (241 rows) — infabode feed, `raw_data->'publicPost'`
- `canonical_url` <- `sourceWebsiteUrl` (== `publicPost.urlOriginal`), 100%
- `sale_price_usd` <- `publicPost.price` WHERE `transactionMode='sale'`, ~53.1%
- `lease_rate_min`/`max` <- `publicPost.price` WHERE `transactionMode='lease'` (per-SF annual), ~16.6%
- `highlights` <- `tags[]` ('BuildingClassB','Parking'), 100%
- `min_divisible_sf`/`max_divisible_sf` <- `publicPost.sizeRangeL`/`sizeRangeH` (non-zero), ~0.8%
DQ guard: provider returns currency='POUND' on USD listings; `salePriceText`/`leaseRateText` carry literal 'POUND ' prefix but values are USD -> ignore label, treat as USD. `publicPost.listingStatus` is contaminated (lease rows carry ['FOR_SALE_ON_MARKET']) -> unreliable. New cols: building class from `tags` (BuildingClassA/B/C); listing office `listingOffice`/`sourceOrganization.name` ('NAI Excel'). needsScrape: cap_rate, noi, occupancy, units, floors, parking, year_built, zoning, term, submarket/county.

### Savills (2 rows) — `raw_data->'rawSavillsProperty'` (full detail API preserved)
Only 2 defensible Chicago retail lease rows; sale structurally capped. Mostly
Class-3 (Price='Price on request'). Recoverable: `canonical_url`, `highlights`
<- `WebFeatureList`, `available_sf` <- `AvailableSize.SqFt`. Low priority.

## Document corpus audit (OM/PDF parse tier)

- 70,414 document rows, ALL stored under a single `doc_type='brochure'`. The
  widened `om`/`financials`/`rent_roll`/`flyer` taxonomy is NOT applied yet ->
  classification step needed.
- 31,883 are direct `.pdf` URLs covering 28,493 distinct listings -> parseable
  NOW via local `/v2/parse` + Docling (zero cloud cost). CBRE (17,245 PDFs /
  16,559 listings) + JLL (9,199 / 8,700) = ~89% of parseable. Then Avison Young
  (2,571), Transwestern (2,864).
- ~37,700 more brochures (Cushman 18,343, Colliers 7,773, Lee 7,681, SVN 3,922)
  have viewer-wrapped / non-`.pdf` URLs -> a URL-shape resolver unlocks them.
- Target columns are near-empty today: noi 0%, occupancy 0%, cap_rate 2.3%,
  year_built 12.5%; unit_mix/rent_roll not first-class columns. Estimated
  12,000-20,000 listings gain >=1 underwriting field on a one-time pass;
  5,000-9,000 gain the full cap_rate+NOI+occupancy triple.
- Sequence: CBRE+JLL PDFs first, then AY/Transwestern, then the URL-shape fix
  for C&W/Colliers/Lee/SVN. Wire an ongoing parse step into the enrich worker so
  new PDFs from the monitor are parsed additively.

## New-column candidates (data already in raw_data, no home today)

- `building_class` (A/B/C) — JLL 85%, Transwestern 70%, NAI tags, AY subtypes.
- listing status badge — Newmark `status` 100%, plus `underContract` flags on
  CBRE-dealflow/Cushman/Colliers/SVN/Lee. Feeds status activation WITHOUT scrape
  (route to the existing OPT-IN activation gate; do not auto-activate).
- broker `license` (+ normalized phone/title) — in `contactsDetailed[]` across
  M&M/JLL/Colliers/CBRE (90-97%). Extends `cre_listing_contacts`.
- M&M net-lease tenant credit: tenant_name, guarantor, lease_years_remaining.
- M&M valuation multiples: grm, price_per_unit, price_per_acre, num_rooms, revpar.
- Transwestern industrial specs: clear_height_ft, dock_doors, drive_in_doors,
  power, rail, parcel/apn.
- `property_subtype` — Newmark/CBRE/Colliers/AY rich subtype strings.

## Free geo-derivation (no scrape)

`county`/`market`(CBSA)/`submarket` derivable from the 98%-filled lat/lng and
94.5%-filled zip via an offline ZIP -> county + CBSA crosswalk (static data
file). Newmark already provides county/market/submarket verbatim.

## Data-quality guards to fold in

1. NAI POUND->USD currency label (treat publicPost.price as USD).
2. Lee `salePriceUsd` per-SF conflation (guard on `salePriceText` '/ SF').
3. Avison Young anomalous `$5000/SF/YR` lease-rate sanity cap.
4. Dual-mode `primary`/`secondary_pass` COALESCE on colliers-main/lee/svn/
   avison-young/transwestern (else ~6-8% of rows drop).
5. Transwestern `Land Area (ac)` unit inconsistency (validate SF vs acres).
6. Newmark `sale_price` 'Subject to Offer' / non-numeric rejection.

## Invariants (unchanged from the media build)

Additive-only. `to_regclass`-guarded INSERTs. COALESCE-keep (sparse pass never
clobbers). detailError-excluded child refresh. No-narrow transaction_type.
Status activation stays OPT-IN default-off. mark-missing untouched. Monitor
enumeration byte-identical. Every new write path needs unit + pytest coverage
and a dry-run before any live apply. Everything gated; nothing live without
explicit go-ahead.
