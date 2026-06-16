# Handoff: Phase-2 data lift (free scalar backfill, OM/PDF parse, new columns, geo + DQ guards) - 2026-06-15

Status: BUILT and verified in code on branch `feat/cre-brokerage-collectors-2026-06-12`.
The data-lift DDL (`011`-`014`) + the crosswalk load + the three backfills
(raw_data scalar, om_classify, geo) were APPLIED to prod 2026-06-15 (board
unchanged: 87,328 active / 0 non-active, status untouched). See Section 0b for
the applied set and real counts. The items in Section 8's still-gated trailer
(OM-parse, media backfill, status activation, `--mark-missing`, consumer
board-gate deploy, enrichment-cadence cutover) remain gated.

Builds directly on the media-capture build (`HANDOFF_MEDIA_CAPTURE_2026-06-15.md`).
Source-of-truth gap spec: `RAW_DATA_GAP_CLASSIFICATION_2026-06-15.md`.
Frozen implementation contract: `PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md`.

## 0. Finishing pass (2026-06-15 cont.) — full crosswalk, two live-schema bug fixes, real counts

The two honest caveats from the first pass are now resolved, and running the
read-only dry-runs against the live DB surfaced (and fixed) two real defects.

- **Full ZIP→CBSA crosswalk built (no longer a seed).** `data/zip_cbsa_crosswalk.csv`
  is now **33,791 rows** (every Census ZCTA; 24,734 with a CBSA, all with
  centroids). HUD's direct download is WAF-blocked (HTTP 202, empty) and its API
  needs a token, so `data/build_zip_cbsa_crosswalk.py` was rewritten to an
  **all-Census, no-token** pipeline: ZCTA↔county rel2020 file (ZIP→county FIPS +
  `AREALAND_PART` dedup), `list1_2023.xlsx` (county→CBSA), 2023 ZCTA gazetteer
  (centroids). Spot-checked (Chicago/NYC/Honolulu/LA/Miami/Dallas) and the 54 geo
  tests pass against the full file. `sql/014` + `data/README.md` provenance updated.
- **BUG 1 (silent data loss) — fixed.** The backfill/classify readers used
  `COPY (...) TO STDOUT` (Postgres *text* format), which **doubles backslashes**;
  any `raw_data` carrying a JSON escape (HTML with `\"`) came back as invalid JSON
  and `json.loads` failed, and the reader's bare `except: continue` **silently
  dropped the row**. This dropped **100% of Marcus & Millichap (3,124 rows)** — the
  single richest structured source (its `marcusSpecifications` holds cap_rate,
  tenant_name, GRM, …). Fix: a shared `cre_ingest.iter_copy_json_rows` /
  `parse_copy_csv_json` reader using **CSV COPY format** (no backslash escaping),
  with the csv field-size limit raised (large `raw_data` blobs exceed the 128 KB
  default) and decode failures **counted + reported loudly, never silently
  skipped**. All four read-back scripts rewired (`cre_backfill_raw_data`,
  `cre_geo_backfill`, `om_classify_existing`, `backfill_media_from_raw_data`).
- **BUG 2 (geo no-op) — fixed.** `cre_geo_backfill` read `postal_code / latitude /
  longitude`, but the live columns are `zip / lat / lng`; `derive_geo` then found
  no ZIP/lat-lng and would have derived geo for **0 rows**. The read SQL now aliases
  the real columns to the keys `derive_geo` expects. Verified read-only: **85,618 /
  87,328 (98.0%)** rows are geo-derivable (81,810 via ZIP, 3,808 via lat/lng).
- Regression test `tests/test_backfill_readback.py` locks both fixes (backslash
  round-trip, oversized field, fail-loud, geo column aliasing, inner-SELECT shape).

**Real candidate counts (live read-only dry-runs, replacing the §7 estimates):**

| Backfill | Real count |
|---|---|
| `canonical_url` (universal) | **87,324** of 87,328 active rows (0%→~100%) |
| `submarket` | 12,465 · `building_class` 9,138 · `property_subtype` 8,330 |
| `lease_rate_min` / `_type` | 9,861 / 4,438 · `sale_price_usd` 4,853 · `sale_price_per_sf` 1,806 |
| `cap_rate` | 2,235 (2,211 from M&M alone) · `size_sf` 2,529 · `year_built` 3,117 |
| M&M institutional (recovered) | tenant_name 823 · guarantor 833 · lease_years_remaining 765 · GRM 624 · price_per_unit 669 |
| geo backfill | 85,618 (county/cbsa_code/cbsa_name/geo_source) |
| `om_classify_existing` | 14,087 of 70,414 brochure rows re-typed (flyer 11,416 · floor_plan 1,843 · om 791 · financials 37) |

Full backfill dry-run scans 87,328 rows (100% of the board) with **zero** decode
failures. Nothing written; all `--dry-run`.

## 0b. LIVE APPLY (2026-06-15) - DDL 011-014, crosswalk load, three backfills

Code was committed and pushed first, then this live apply was authorized. All
steps are additive, idempotent, dry-run-confirmed first, and verified read-only
after. Project `fhqycqubkkrdgzswccwd`, schema `credeals`. No connection string
was ever printed.

- **DDL applied in order `011` -> `012` -> `013` -> `014`** via psql (non-pooling,
  `ON_ERROR_STOP`):
  - `011_cre_listing_media.sql`: NEW `cre_listing_media` + `cre_listing_links`
    (+ `*_archive` mirrors), and the purely-widening
    `cre_listing_documents.doc_type` CHECK rebuild (adds `financials`,
    `rent_roll`). `011` was applied as a PREREQUISITE for `om_classify_existing`,
    whose 37 `financials` upgrades require the widened CHECK. The new media/links
    tables are EMPTY (the media-capture backfill is NOT part of this run and
    stays gated).
  - `012_cre_listing_institutional_cols.sql`: institutional scalar columns + geo
    columns (`cbsa_code`, `cbsa_name`, `geo_source`) + `extra_facts` jsonb on
    `cre_listings`, plus `license` on `cre_listing_contacts`. Guarded range/enum
    CHECKs.
  - `013_cre_listing_om_facts.sql`: NEW `cre_listing_om_facts` (+ archive). Stays
    EMPTY (OM-parse gated).
  - `014_cre_geo_crosswalk.sql`: NEW `cre_zip_cbsa_crosswalk` reference table.
- **Crosswalk loaded** via `\copy` from `data/zip_cbsa_crosswalk.csv`:
  **33,791 rows** (all-distinct zip5, 24,734 with a CBSA, 0 NULL centroids, 0
  empty-string artifacts).
- **Backfill 1 - `cre_backfill_raw_data.py --apply`** (all 12 brokerage slugs,
  additive COALESCE-keep, never touches `status`/`deleted_at`). All 87,328 active
  rows scanned, 0 decode failures (the M&M 3,124 rows the old text-format reader
  silently dropped now scan cleanly via the CSV reader fix). Coverage now on
  active rows: `canonical_url` 0 -> 87,324; `cap_rate` 2,235; `submarket` 12,465;
  `building_class` 9,138; `property_subtype` 8,330; `tenant_name` 823;
  `guarantor` 833; `grm` 624; `year_built` 13,031; `extra_facts` non-empty
  10,600.
- **Backfill 2 - `om_classify_existing.py --apply`**: 14,087 of 70,414 brochure
  rows upgraded (upgrade-only, never downgrades): flyer 11,416, floor_plan 1,843,
  om 791, financials 37. `doc_type` distribution now: brochure 56,327, flyer
  11,416, floor_plan 1,843, om 791, financials 37.
- **Backfill 3 - `cre_geo_backfill.py --apply`**: 85,618 of 87,328 rows derived
  (additive COALESCE-keep). county 85,618, cbsa_code/cbsa_name 83,815, geo_source
  85,618 (crosswalk_zip 77,499, source 4,368, crosswalk_latlng 3,751; 1,710 no
  crosswalk hit). Top metros: LA 5,212, SF 3,902, Houston 3,698, NYC 3,225,
  Chicago 3,094, Dallas 2,546.

**Board integrity (verified read-only after apply):** 87,328 active, 0
non-active, 92,699 total. `status` was NEVER touched (activation stays OPT-IN
default-off). Consumer views resolve unchanged (`v_cre_active_for_sale` 33,824,
`v_cre_active_for_lease` 58,727, `v_cre_listings_full` 87,328). `cre_listing_om_facts`,
`cre_listing_media`, and `cre_listing_links` stay EMPTY (OM-parse and media
backfill gated). 738 pytest pass (code unchanged this session).

## 1. Why

A live read-only audit of all 87,328 active rows found that most of the empty /
sparse `cre_listings` columns are not missing data: the value is already in
`raw_data`, captured but never mapped. The audit split every gap into three
classes (free-to-map / needs-OM-or-detail / unpublished) per source. This build
delivers all four follow-up workstreams the audit recommended.

## 2. What shipped (all additive, all gated)

### WS1 - Free raw_data scalar backfill + adapter forward-mapping
- `cre_backfill_raw_data.py` (NEW): one-time, additive, idempotent backfill that
  reads stored `raw_data` for the existing ~87k rows and writes the now-mappable
  columns with ZERO scraping. Dual-mode `COALESCE(raw_data->'primary',
  raw_data->'secondary_pass', raw_data)` read; universal `canonical_url` from
  `url`; per-source nested-scalar lift via `cre_parse.py`; `UPDATE ... SET col =
  COALESCE(<derived>, col)` so it NEVER blanks a populated value; `extra_facts`
  via `||` merge; per-source DQ guards folded in. `--dry-run` default (per-source
  per-column candidate-count summary), `--apply` gated, to_regclass /
  column-existence guarded.
- All 11 active source adapters extended to emit the contract camelCase fields at
  collect time (forward path) from each source's native nested object (M&M
  `marcusSpecifications`, AY `rawSharpLaunch`, Newmark `rawNewmarkHit`,
  Transwestern `transwesternFacts` + `availability[]`, JLL `jllDetail`, CBRE/
  Cushman/Colliers/Buildout `leaseRateText` via the shared parser, NAI
  `publicPost`, Savills `rawSavillsProperty`). Each ships a real scrubbed
  `tests/fixtures/raw_data/<source>.json` and unit tests.

### WS2 - OM/PDF parse tier
- `om_parse.py` (NEW): selects listings whose documents carry a parseable OM/
  brochure PDF and which lack underwriting fields; calls the LOCAL self-hosted
  Firecrawl `/v2/parse` (Docling, zero cloud cost); a PURE `extract_om_facts`
  pulls noi / cap_rate / occupancy / units / year_built + unit_mix / rent_roll.
  Every scalar carries provenance (source_doc_url, parser_version, confidence).
  CONFIDENCE FLOOR: a scalar with confidence < 0.6 is written ONLY to
  `cre_listing_om_facts`, never to the board-facing `cre_listings` column;
  re-ingest is additive and never activates status or soft-deletes. `--dry-run`
  default, `--apply` gated, CBRE+JLL first.
- `om_url_resolver.py` (NEW): resolves viewer-wrapped / non-`.pdf` brochure URLs
  (Cushman / Colliers / Lee / SVN) to the real PDF; unresolvable -> None.
- `om_classify_existing.py` (NEW): one-time UPGRADE-ONLY re-classification of the
  70,414 existing `doc_type='brochure'` rows to a more-specific type
  (om / financials / rent_roll / floor_plan / flyer), never downgrading, never
  touching already-non-brochure rows. `--dry-run` default, `--apply` gated.
- `cre_enrich.py`: the OM-parse step wired into the Tier-B enrich flow additively
  (dry-run never reaches the OM step; ingest argv stays `['--in', path]`).

### WS3 - New high-value columns
- `sql/012_cre_listing_institutional_cols.sql`: building_class (A/B/C/D CHECK),
  property_subtype, apn, tenant_name, guarantor, lease_years_remaining,
  price_per_unit, grm, price_per_acre, num_rooms, revpar, clear_height_ft,
  dock_doors, drive_in_doors, power_service, rail_served, cbsa_code, cbsa_name,
  geo_source, `extra_facts jsonb`, plus `cre_listing_contacts.license`. All
  `ADD COLUMN IF NOT EXISTS`, range/enum CHECKs guarded, COMMENTed.
- Status badges flow through the NEW universal `statusBadge` path into
  `norm_status`, which is STILL subject to the OPT-IN default-off activation gate.
  No badge auto-activates or is written to `status` directly. Newmark gates
  `statusBadge` behind `!monitor` so monitor enumeration stays byte-identical.

### WS4 - Geo-derivation + data-quality guards
- `cre_geo.py` (NEW): `ZipCbsaCrosswalk` (by_zip / by_latlng) + `derive_geo`
  (precedence source > crosswalk_zip > crosswalk_latlng; submarket source-only,
  never fabricated; market = cbsa_name only when source gave none, COALESCE-keep).
- `sql/014_cre_geo_crosswalk.sql` (NEW): `cre_zip_cbsa_crosswalk` reference table.
- `data/build_zip_cbsa_crosswalk.py` (NEW): deterministic all-Census builder
  (rewritten in the §0 finishing pass; HUD dropped as un-fetchable). The committed
  `data/zip_cbsa_crosswalk.csv` is now the **full 33,791-row** dataset. See
  `data/README.md`.
- `cre_geo_backfill.py` (NEW): additive, COALESCE-keep geo backfill for existing
  rows; reads the real `zip / lat / lng` columns (§0 BUG 2 fix). `--dry-run`
  default, `--apply` gated. NOTE: its own `--dry-run` requires `sql/012` applied
  (its WHERE references `cbsa_code`); the candidate count (85,618) was verified
  read-only via `derive_geo` against existing columns.
- Six DQ guards (in `cre_parse.py` / `cre_ingest.py` / adapters), integration-
  tested in `tests/test_dq_guards.py`: NAI POUND->USD, Lee per-SF `salePriceUsd`
  conflation, AY $5000/SF/YR cap, dual-mode primary/secondary COALESCE,
  Transwestern `Land Area (ac)` unit validation, Newmark non-numeric price.

### Schema (also new)
- `sql/013_cre_listing_om_facts.sql`: `cre_listing_om_facts` (+ archive mirror)
  with parse provenance; unique `(listing_id, fact_group, fact_key,
  source_doc_url) NULLS NOT DISTINCT`, RLS on, FK ON DELETE CASCADE.
- `sql/005_cre_views.sql` widened: `v_cre_listings_full` gains the om_facts
  LATERAL + institutional/geo columns; sale/lease views gain the new columns.
  No predicate change.
- Registered in `000_run_all.sql`: `011 -> 012 -> 013 -> 014 -> 006 -> 005`.

### Shared foundation (frozen during the fan-out)
- `lib/parse.ts` (TS) + `cre_parse.py` (Python): mirrored parsers
  (parseLeaseRate / parseMoney / acresToSf / parseAmountIgnoringCurrencyLabel /
  parsePercentToFraction / normBuildingClass / parseSizeText / isPerSfText +
  classify_doc), proven identical by a shared golden-vector fixture.
- `cre_ingest.py`: all new columns COALESCE-keep, `extra_facts` jsonb-merge,
  `om_facts` to_regclass-guarded child, contact `license` branch, statusBadge ->
  OPT-IN gate, the in-ingest DQ guards.

## 3. Verification

- `npm run typecheck`: clean (no TS change in the §0 finishing pass).
- `npm run test:unit`: 468 pass / 0 fail (all 14 source adapters, 15 test files, + lib).
- `python3 -m pytest tests/`: **738 pass / 0 fail** (was 727; +11 from the new
  `test_backfill_readback.py` covering the §0 read-back + geo fixes). Also includes
  `test_parse_parity.py` (25-case TS<->Python golden parity), `test_backfill_raw_data.py`,
  `test_om_parse.py`, `test_om_url_resolver.py`, `test_om_enrich_wiring.py`,
  `test_doc_classify.py`, `test_cre_geo.py`, `test_geo_derive.py`, `test_dq_guards.py`.
- `ruff check` (F401) clean on all changed files.
- Live read-only dry-runs (geo, raw_data backfill, om_classify) run end-to-end:
  87,328 rows scanned, 0 decode failures, no connection string emitted.
- Ingest dry-run on a fresh marcus-millichap probe: status activation OFF,
  to_regclass / column-existence guarded blocks present (24 guard markers), no
  connection string emitted, statusBadge not fabricated.
- Adversarial review (Opus): verdict ship-with-fixes; every hard invariant holds
  (additive-only, no-clobber, status-activation-safe, monitor byte-identical,
  parser parity, DQ guards, no secret leak). The three review items were folded
  in: (a) newmark statusBadge gated behind `!monitor` (structural monitor
  byte-identicality), (b) the `cre_ingest.py` universal-status comment corrected
  to describe the real per-source basis, (c) the Python golden-vector parity test
  added.

## 4. Build method

Foundation (Opus plan -> contract; Opus on the shared hot files; Sonnet on lib;
verify) locked the cross-file contract first. Then a 16-agent fan-out on DISJOINT
files only (11 source adapters on Sonnet, backfill + OM-parse on Opus, doc-
classify / geo / dq on Sonnet), in 3 staggered batches after an initial run hit a
transient server-side rate limit. Opus verify+fix and Opus adversarial review
closed it out.

## 5. Files

New scripts: `cre_backfill_raw_data.py`, `om_parse.py`, `om_url_resolver.py`,
`om_classify_existing.py`, `cre_geo.py`, `cre_geo_backfill.py`, `cre_parse.py`.
New SQL: `sql/012`, `sql/013`, `sql/014`.
New lib/data: `lib/parse.ts`, `lib/geo.ts`, `data/build_zip_cbsa_crosswalk.py`
(all-Census, rewritten §0), `data/zip_cbsa_crosswalk.csv` (FULL, 33,791 rows),
`data/README.md`.
New tests: `tests/test_parse_parity.py`, `test_backfill_raw_data.py`,
`test_om_parse.py`, `test_om_url_resolver.py`, `test_om_enrich_wiring.py`,
`test_doc_classify.py`, `test_cre_geo.py`, `test_geo_derive.py`,
`test_dq_guards.py`, **`test_backfill_readback.py` (§0 read-back + geo fixes)**,
`tests/ts/lib/parse.test.ts`, `tests/ts/lib/geo.test.ts`,
`tests/fixtures/golden_parse_vectors.json`, `tests/fixtures/raw_data/<11 sources>.json`,
`tests/fixtures/parse/{cbre_om,jll_om}.json`, `tests/fixtures/geo/mini_crosswalk.csv`.
Edited: `cre_ingest.py` (+`iter_copy_json_rows`/`parse_copy_csv_json` §0),
`cre_backfill_raw_data.py`, `cre_geo_backfill.py`, `om_classify_existing.py`,
`backfill_media_from_raw_data.py` (all rewired to the CSV reader §0),
`cre_enrich.py`, `types.ts`, all 14 `sources/*.ts`, their tests,
`sql/000_run_all.sql`, `sql/005_cre_views.sql`, `sql/014_cre_geo_crosswalk.sql`,
`sql/CLAUDE.md`.

## 6. Operational note (working-tree execution)

The live launchd monitor/daily/enrich tiers run `collect.ts`/`cre_ingest.py` from
this working tree, so the FORWARD adapter field-lifts take effect on the next
scheduled run, additively (COALESCE-keep). The NEW columns are no-ops until
`sql/012`-`014` are applied (every new write is column/to_regclass guarded). The
existing 87k rows fill via the gated backfills, not the daily path. OM-parse runs
only through its own gated worker, never the bulk collect.

## 7. Expected coverage lift (from the audit)

Free backfill recovers, per source, the documented recoverable scalars
(e.g. M&M cap_rate ~70.8%, occupancy ~21.7% from zero; Transwestern floors ~82.8%,
year_built ~72.4%; Newmark county ~99.9%, submarket ~47.4%; JLL submarket ~85.1%;
universal canonical_url ~95% board-wide). OM-parse plausibly adds >=1 underwriting
field to 12,000-20,000 listings (5,000-9,000 with the full cap_rate+NOI+occupancy
triple). All figures are pre-apply estimates; the backfill `--dry-run` prints the
real candidate counts before any write.

## 8. Gated live steps (need explicit go-ahead, in order)

1. **DONE / APPLIED 2026-06-15** (`011` -> `012` -> `013` -> `014`; `sql/005`
   widening stays gated, see trailer). Apply `sql/012`, `sql/013`, `sql/014` and
   the `sql/005` widening to project `fhqycqubkkrdgzswccwd` (verify read-only
   zero-row no-op first). Applied `011`-`014` only (`011` as the
   `om_classify_existing` CHECK prerequisite); board unchanged at 87,328 active.
2. **DONE / APPLIED 2026-06-15** (all 87,328 rows scanned, 0 decode failures;
   `canonical_url` 0 -> 87,324, `cap_rate` 2,235, etc. per §0b). `python3
   cre_backfill_raw_data.py --dry-run` -> review per-source candidate counts ->
   `--apply`.
3. **DONE / APPLIED 2026-06-15** (14,087 of 70,414 brochure rows upgraded: flyer
   11,416, floor_plan 1,843, om 791, financials 37). `python3
   om_classify_existing.py --dry-run` -> review old->new type counts -> `--apply`.
4. **DONE / APPLIED 2026-06-15** (85,618 of 87,328 rows derived; crosswalk
   `\copy`'d into `cre_zip_cbsa_crosswalk`, 33,791 rows). Geo backfill: the full
   crosswalk is already built/committed (§0). After `sql/012` is applied (step 1),
   run `python3 cre_geo_backfill.py --dry-run` (its WHERE needs the `cbsa_code`
   column) -> review (~85,618 rows) -> `--apply`. Optionally also `\copy` the CSV
   into `cre_zip_cbsa_crosswalk` for consumer SQL joins (commented-out load in
   `sql/014`).
5. OM-parse pass: confirm the local Firecrawl stack is up, then
   `python3 om_parse.py --dry-run` (CBRE+JLL first) -> review -> `--apply`; then
   wire the enrich cadence per `ENRICHMENT_WORKER_DESIGN_2026-06-15.md`.

Unchanged and still separately gated: status activation, `--mark-missing`
soft-delete, the consumer board-gate deploy (and the paired `sql/005` view
widening), the enrichment-cadence launchd cutover, and the MEDIA BACKFILL run.
NOTE: the media-capture `sql/011` DDL is now APPLIED (2026-06-15), so the media
backfill is no longer blocked on DDL, only on go-ahead.
