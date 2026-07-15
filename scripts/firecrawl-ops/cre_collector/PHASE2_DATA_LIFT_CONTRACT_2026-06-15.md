# Phase-2 Data-Lift Implementation Contract (2026-06-15)

> **Historical implementation contract.** This document is not the current
> source of truth for OM writes. Its WS2 activation and enrich-wiring steps are
> superseded: GetCREdata is the sole production OM writer, and local
> `om_parse.py --apply` exits `78`. Keep the pure parsing material only for
> regression coverage. Follow `START_HERE.md` and the operator runbook for
> current operational instructions.

Lead engineer (Opus) contract for the multi-phase CRE data-lift build. This is
the EXACT spec ~15 downstream agents follow VERBATIM so they can work on DISJOINT
files without conflict. It is the single source of truth for the four
workstreams (WS1 free raw_data scalar backfill, WS2 OM/PDF parse tier, WS3 new
high-value columns, WS4 geo-derivation + DQ guards).

Inputs read to author this contract: `RAW_DATA_GAP_CLASSIFICATION_2026-06-15.md`
(gap source of truth), `CRE_LISTINGS_COLUMN_COVERAGE_2026-06-15.md` (fill rates),
`HANDOFF_MEDIA_CAPTURE_2026-06-15.md` (the just-shipped media build, NOT to be
duplicated or undone), and the live code (`cre_ingest.py`, `types.ts`,
`lib/harvest.ts`, `lib/util.ts`, `lib/scrape.ts`, `lib/enrich.ts`, `cre_enrich.py`,
representative `sources/*.ts`, `sql/000_run_all.sql`, `sql/002`, `sql/005`,
`sql/011`).

## 0. Two-phase build structure

This contract describes a TWO-WORKFLOW build:

- **Workflow 1 (FOUNDATION, sequential, single owner = the lead/integration
  agent).** Lands every shared file the parallel agents depend on: SQL migrations
  `012`/`013`/`014`, `types.ts` field additions, `lib/parse.ts`, `lib/geo.ts`,
  `cre_parse.py`, `cre_geo.py`, and the `cre_ingest.py` forward-map wiring.
  Workflow 1 also writes the SHARED GOLDEN TEST-VECTOR fixture
  (`tests/fixtures/golden_parse_vectors.json`, Section C.5) that both the TS and
  Python parser test authors import. Nothing in Workflow 1 is applied to prod or
  committed; it is staged, dry-run-verified, and reviewed.
- **Workflow 2 (PARALLEL, ~15 agents, DISJOINT files).** Each agent owns exactly
  one `sources/<broker>.ts` (+ its test) OR one new script (+ its test). The
  FOUNDATION files (`cre_ingest.py`, `types.ts`, `lib/*`, `sql/*`,
  `cre_parse.py`, `cre_geo.py`) are FROZEN in Workflow 2: no Workflow-2 agent
  edits them. The file-ownership map is Section F.

Rationale: forward-map + ingest + parser-library contracts MUST agree on field
names and semantics before any adapter agent emits them, so they are built once
in Workflow 1 and consumed read-only in Workflow 2.

## A. NEW SQL

Existing migrations stop at `011`. Three new migrations are added. The
`000_run_all.sql` registration order and rationale are below.

### A.1 Discrete columns vs jsonb extra_facts (decision + justification)

DECISION: **discrete columns for the high-value institutional fields; a single
`extra_facts jsonb` blob on `cre_listings` for the long tail.**

- Discrete (queryable, indexable, board/consumer-facing): `building_class`,
  `property_subtype`, `apn`, `tenant_name`, `guarantor`, `lease_years_remaining`,
  the M&M valuation multiples (`price_per_unit`, `grm`, `price_per_acre`,
  `num_rooms`, `revpar`), and the Transwestern industrial specs
  (`clear_height_ft`, `dock_doors`, `drive_in_doors`, `power_service`,
  `rail_served`). These are fields an EQUIRE agent filters/sorts/underwrites on;
  they earn a column. Cap-rate, NOI, occupancy, units, year_built, and the
  lease/sale price columns ALREADY EXIST and are REUSED (no new column).
- `extra_facts jsonb` (default `'{}'`): the genuine long tail with no consumer
  query need (e.g. `'Typical Floor Size'`, `'Elevators'`, `'Year Renovated'`,
  `'Buildable Square Feet'`, `'crane'`, `'yard'`, `'Number of Rooms'` on
  non-hotel, `assetType` raw subtype strings). One additive jsonb column avoids a
  column explosion for sparse, rarely-queried facts while still capturing them.

Cutline rule for downstream agents: if the gap doc names it as a NEW column
candidate in Section "New-column candidates" AND a consumer would filter on it,
it gets a discrete column listed in A.4; everything else the adapter promotes
goes into `extra_facts` under a stable snake_case key.

### A.2 OM-parsed fields: reuse vs new table (decision)

DECISION:

- **OM scalar underwriting fields REUSE the EXISTING `cre_listings` columns**:
  `noi`, `cap_rate`, `occupancy_rate`, `units`, `year_built`, `gross_revenue`,
  `size_sf`, `lease_rate_*`, `sale_price_*`. They go through the SAME COALESCE-keep
  upsert path so an OM parse never clobbers a fuller prior capture, and a board
  consumer already reads them. No schema change for these.
- **Unit-mix / rent-roll line items and parse PROVENANCE get a NEW table
  `cre_listing_om_facts`** (migration `013`). One row per (listing, fact group,
  fact key, source document, parser version), with the parse provenance columns
  (`source_doc_url`, `parsed_at`, `parser_version`, `confidence`) so every
  OM-derived datum is auditable. A re-parse by the same parser release is
  idempotent, while a later parser release retains its own audit row. Unit-mix/
  rent-roll are arrays-of-objects with no fixed arity; a child table is the
  correct shape, not jsonb on the parent.

Provenance contract: when the OM-parse tier writes a scalar onto `cre_listings`
(e.g. `noi`), it ALSO writes a `cre_listing_om_facts` row recording WHICH doc and
parse produced it (`fact_key='noi'`, `fact_value_num`, `source_doc_url`,
`confidence`), so the column value is traceable to a parsed page. This keeps the
columns clean for consumers while preserving an audit trail.

### A.3 Migration file numbers + 000_run_all.sql order

Three new migrations:

- **`012_cre_listing_institutional_cols.sql`**: additive `ALTER TABLE
  credeals.cre_listings ADD COLUMN IF NOT EXISTS ...` for the WS3 discrete
  columns + `extra_facts jsonb`, and the WS4 geo columns (`market`/`submarket`
  already exist; `county` already exists; this adds `cbsa_code`,
  `cbsa_name`, and `geo_source`). Plus the `cre_listing_contacts.license` column
  (WS3 broker license). Idempotent: every statement is `ADD COLUMN IF NOT EXISTS`
  / a guarded CHECK rebuild mirroring the 002 status-CHECK and 011 doc_type
  templates.
- **`013_cre_listing_om_facts.sql`**: NEW `credeals.cre_listing_om_facts` table
  (+ its `*_archive` mirror following the 009/011 retirement-snapshot pattern),
  RLS on with no public policy, FK `ON DELETE CASCADE`, unique index
  `NULLS NOT DISTINCT`, table COMMENT.
- **`014_cre_geo_crosswalk.sql`**: NEW `credeals.cre_zip_cbsa_crosswalk`
  reference table (the offline ZIP -> county + CBSA dataset, Section E), loaded by
  `\copy` from the committed CSV. Pure reference data; no FK to listings, RLS on.

`000_run_all.sql` registration order (append AFTER `011`, BEFORE `006`, mirroring
the 011 placement rationale that 005 must run last so views see new tables):

```
... 009 ... 010 ... 011_cre_listing_media ...
012_cre_listing_institutional_cols     <- ADD COLUMNs on cre_listings + contacts
013_cre_listing_om_facts               <- new om_facts table (+ archive)
014_cre_geo_crosswalk                  <- new reference table + \copy load
006_cre_contact_urls                   <- (unchanged position; runs after 012 contacts ALTER)
005_cre_views                          <- LAST; widened to expose new columns
```

`\i` lines inserted after the `011` `\i` line and before the `006` `\i` line in
`000_run_all.sql`. NOTE: `006` ALTERs `cre_listing_contacts`; `012` also ALTERs
it (adds `license`). Both use `ADD COLUMN IF NOT EXISTS`, so order between them is
immaterial; keep `012`->`013`->`014`->`006`->`005`.

### A.4 Exact DDL for every new column / table

All `ALTER`s are `ADD COLUMN IF NOT EXISTS` and additive. Every column is
nullable with no default unless stated (a default would make 0% columns "look
populated", violating the coverage-report convention).

**`012_cre_listing_institutional_cols.sql`: `cre_listings` columns:**

| Column | Type | CHECK | COMMENT (verbatim intent) |
|---|---|---|---|
| `building_class` | `text` | `CHECK (building_class IS NULL OR building_class IN ('A','B','C','D'))` | Building class A/B/C/D when the source states it (JLL buildingClass, Transwestern Class, NAI tags, AY subtype). NULL = unstated, never inferred. |
| `property_subtype` | `text` | none (free text, length-capped in ingest to 96) | Source-stated property subtype string (e.g. 'Warehouse/Distribution', 'office.medical'). Finer than property_type; not an enum. |
| `apn` | `text` | none | Assessor parcel number / parcel id when the source exposes it (Transwestern Parcel, OM cover). Free text; not validated. |
| `tenant_name` | `text` | none | Single-tenant net-lease tenant name (M&M 'Tenant Name'). |
| `guarantor` | `text` | none | Lease guarantor / credit entity (M&M 'Guarantor'). |
| `lease_years_remaining` | `numeric` | `CHECK (lease_years_remaining IS NULL OR (lease_years_remaining >= 0 AND lease_years_remaining <= 99))` | Years remaining on the in-place lease (M&M 'Years Remaining On Lease'). |
| `price_per_unit` | `numeric` | `CHECK (price_per_unit IS NULL OR price_per_unit > 0)` | Sale price per unit (M&M 'Price/Unit'). USD. |
| `grm` | `numeric` | `CHECK (grm IS NULL OR (grm > 0 AND grm < 100))` | Gross rent multiplier (M&M 'GRM'). |
| `price_per_acre` | `numeric` | `CHECK (price_per_acre IS NULL OR price_per_acre > 0)` | Sale price per acre (M&M 'Price/Acre'). USD. |
| `num_rooms` | `integer` | `CHECK (num_rooms IS NULL OR num_rooms > 0)` | Hotel room count (M&M 'Number of Rooms'). |
| `revpar` | `numeric` | `CHECK (revpar IS NULL OR revpar > 0)` | Hotel revenue per available room (M&M 'RevPAR'). USD. |
| `clear_height_ft` | `numeric` | `CHECK (clear_height_ft IS NULL OR (clear_height_ft > 0 AND clear_height_ft < 200))` | Industrial clear height in feet (Transwestern). |
| `dock_doors` | `integer` | `CHECK (dock_doors IS NULL OR dock_doors >= 0)` | Dock-high door count (industrial). |
| `drive_in_doors` | `integer` | `CHECK (drive_in_doors IS NULL OR drive_in_doors >= 0)` | Drive-in / grade-level door count (industrial). |
| `power_service` | `text` | none | Electrical service description (e.g. '2000A 480V'). Free text. |
| `rail_served` | `boolean` | none | True when the property is rail-served (industrial). |
| `cbsa_code` | `text` | none | 5-digit CBSA (metro market) code from the offline ZIP->CBSA crosswalk. Geo-derived, not scraped. |
| `cbsa_name` | `text` | none | CBSA (metro market) name from the crosswalk (e.g. 'Dallas-Fort Worth-Arlington, TX'). |
| `geo_source` | `text` | `CHECK (geo_source IS NULL OR geo_source IN ('source','crosswalk_zip','crosswalk_latlng'))` | Provenance of derived county/market/submarket: 'source' (broker gave it verbatim, e.g. Newmark), 'crosswalk_zip', or 'crosswalk_latlng'. |
| `extra_facts` | `jsonb` | none (DEFAULT `'{}'::jsonb`) | Long-tail source facts with no discrete column and no consumer query need. snake_case keys. Additive; never clobbers, merged jsonb on upsert. |

`county`, `market`, `submarket`, `canonical_url` ALREADY EXIST on `cre_listings`
(002:24,40,44,45). WS1/WS4 populate them; no DDL for those four.

**`012`: `cre_listing_contacts` column:**

| Column | Type | CHECK | COMMENT |
|---|---|---|---|
| `license` | `text` | none | Broker real-estate license string as printed (e.g. 'IL: 475.188007'). `title` already exists (002). |

CHECK rebuilds in `012` use the guarded `DO $$ ... pg_get_constraintdef ... DROP
CONSTRAINT IF EXISTS / ADD CONSTRAINT $$` template from 002:187-204 so a re-run on
a populated table does not take ACCESS EXCLUSIVE + a validating scan every time.
Each new CHECK is added only if absent (the `IF NOT EXISTS (SELECT 1 FROM
pg_constraint ...)` template from 002:211-225).

**`013_cre_listing_om_facts.sql`: NEW table:**

```sql
CREATE TABLE IF NOT EXISTS credeals.cre_listing_om_facts (
    id             uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id     uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    fact_group     text        NOT NULL DEFAULT 'scalar'
                               CHECK (fact_group IN ('scalar','unit_mix','rent_roll')),
    fact_key       text        NOT NULL,        -- e.g. 'noi','cap_rate','unit_type','tenant'
    fact_value_text text,
    fact_value_num numeric,
    unit_count     integer,                     -- unit_mix: # of units of this type
    -- provenance (required on every OM-derived row)
    source_doc_url text        NOT NULL,        -- the parsed document URL
    parsed_at      timestamptz NOT NULL DEFAULT now(),
    parser_version text        NOT NULL,        -- e.g. 'om-parse/1'
    confidence     numeric     CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cre_listing_om_facts_listing_idx
    ON credeals.cre_listing_om_facts (listing_id);
CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_om_facts_uq
    ON credeals.cre_listing_om_facts (listing_id, fact_group, fact_key, source_doc_url, parser_version) NULLS NOT DISTINCT;
ALTER TABLE credeals.cre_listing_om_facts ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE credeals.cre_listing_om_facts IS
    'OM/PDF-parsed facts (scalar underwriting + unit_mix + rent_roll) with parse provenance. Scalars also COALESCE-write the matching cre_listings column; this table is the audit trail and the home for non-scalar line items. Service-role only (RLS on, no public policy).';
```

Plus `cre_listing_om_facts_archive` (no FK, retirement snapshot, 009/011
pattern). The mark-missing archive INSERT in `cre_ingest.py` is `to_regclass`-
guarded exactly like the media/links archive (1463-1486).

**`014_cre_geo_crosswalk.sql`: NEW reference table:** see Section E for the
dataset, columns, and `\copy` load.

### A.5 005 view widening

`005_cre_views.sql` (`CREATE OR REPLACE VIEW`, runs last) is widened additively:

- `v_cre_listings_full`: passes through `l.*` already, so the new `cre_listings`
  columns appear automatically. ADD a LATERAL `json_agg` block exposing
  `cre_listing_om_facts` as an `om_facts` json array (mirroring the media/links
  LATERALs at 005:86-98), guarded by being created after `013`.
- `v_cre_active_for_sale`: ADD `l.building_class, l.property_subtype, l.apn,
  l.tenant_name, l.guarantor, l.lease_years_remaining, l.price_per_unit, l.grm,
  l.price_per_acre, l.num_rooms, l.revpar, l.cbsa_code, l.cbsa_name` to the
  explicit column list (sale-relevant institutional fields).
- `v_cre_active_for_lease`: ADD `l.building_class, l.property_subtype,
  l.clear_height_ft, l.dock_doors, l.drive_in_doors, l.power_service,
  l.rail_served, l.cbsa_code, l.cbsa_name` (lease/industrial-relevant fields).
- The on-market predicate `status IN ('active','under_contract','pending')` is
  UNCHANGED. Status-badge work (WS3) routes through the existing OPT-IN activation
  gate; it never widens these predicates and never auto-activates.

`security_invoker=true` is reasserted on every CREATE OR REPLACE (005 already does
this). No view is renamed or dropped (EQUIRE read contract, sql/CLAUDE.md).

## B. COLLECTOR FIELDS (camelCase on the `types.ts` Listing object)

Adapters EMIT these camelCase keys; `cre_ingest.py to_row()` READS them. The
Listing object is `any` at compile time, so these are a documented vocabulary
(like the existing comment block in `types.ts:97-107`), added to that comment.
Forward-map and ingest MUST agree on these exact names.

| camelCase field (adapter emits) | cre_listings column (ingest writes) | Normalizer in `to_row` |
|---|---|---|
| `canonicalUrl` | `canonical_url` | `http_url_or_none` |
| `buildingClass` | `building_class` | `norm_building_class` (NEW) -> A/B/C/D or NULL |
| `propertySubtype` | `property_subtype` | `clean_text(v, 96)` |
| `apn` | `apn` | `clean_text(v, 64)` |
| `tenantName` | `tenant_name` | `clean_text(v, 256)` |
| `guarantor` | `guarantor` | `clean_text(v, 256)` |
| `leaseYearsRemaining` | `lease_years_remaining` | `num_or_none(v, lo=0, hi=99)` |
| `pricePerUnit` | `price_per_unit` | `num_or_none(v, lo=0, hi=1e9)` |
| `grm` | `grm` | `num_or_none(v, lo=0, hi=100)` |
| `pricePerAcre` | `price_per_acre` | `num_or_none(v, lo=0, hi=1e9)` |
| `numRooms` | `num_rooms` | `num_or_none(v, lo=0, hi=1e5)` -> int |
| `revpar` | `revpar` | `num_or_none(v, lo=0, hi=1e5)` |
| `clearHeightFt` | `clear_height_ft` | `num_or_none(v, lo=0, hi=200)` |
| `dockDoors` | `dock_doors` | `num_or_none(v, lo=0, hi=1e4)` -> int |
| `driveInDoors` | `drive_in_doors` | `num_or_none(v, lo=0, hi=1e4)` -> int |
| `powerService` | `power_service` | `clean_text(v, 128)` |
| `railServed` | `rail_served` | bool passthrough (True/False/None) |
| `statusBadge` | (routes to existing status gate) | `norm_status` consumes via STATUS_SOURCE_PATHS; NEVER a direct column write; OPT-IN gate applies |
| `extraFacts` | `extra_facts` | `extra_facts_or_none` (NEW) -> dict of snake_case keys or None |
| `leaseRateType` | `lease_rate_type` | `norm_lease_rate_type` (EXISTS) |
| `leaseRateMin`/`leaseRateMax` | `lease_rate_min`/`max` | `num_or_none` (when adapter pre-parses cleaner than `leaseRateText`) |
| `omFacts` | `cre_listing_om_facts` rows | `om_facts_rows` (NEW, WS2 only; provenance-bearing) |

Existing fields that WS1 newly POPULATES (no rename): `submarket`, `market`,
`county`, `units`, `yearBuilt`, `occupancyRate`, `availableSf`, `minDivisibleSf`,
`maxDivisibleSf`, `capRatePct`, `salePricePerSf`, `noi`, `floors`, `zoning`,
`highlights`, `amenities`, `description`. These keys ALREADY exist in `to_row`;
adapters just need to emit them (forward path) and the backfill script writes the
columns directly (backfill path).

Broker `license` rides the EXISTING `contactsDetailed[]` channel: adapters add a
`license` key to each contact object (M&M already does, marcus-millichap.ts:233);
`to_row` contacts mapping (cre_ingest.py:703-715) gains `"license": c.get("license")`
and the `cre_listing_contacts` INSERT (1302-1313) gains the `license` column.
`title` is already mapped.

## C. PARSER API

The lead/integration agent (Workflow 1) authors `lib/parse.ts`, `lib/geo.ts`,
`cre_parse.py`, `cre_geo.py`. The TS and Python sides are VERIFIABLY IDENTICAL via
the shared golden test-vector table (C.5). Adapters (TS) call `lib/parse.ts`; the
backfill + ingest (Python) call `cre_parse.py`.

### C.1 `lib/parse.ts` (TypeScript)

```ts
// Lease-rate parse: returns annualized $/SF/yr min/max + normalized basis type.
// Mirrors and SUPERSEDES the inline logic in cre_ingest.parse_lease_rates +
// norm_lease_rate_type, exposed as one reusable function for adapters.
export interface LeaseRate {
  min: number | null;   // $/SF/yr, annualized; null when not per-SF-trustable
  max: number | null;   // range high, else null
  type: "nnn" | "modified_gross" | "gross" | "full_service" | null;
}
export function parseLeaseRate(text: string | null): LeaseRate;

// Money: first "$N[,N][.N]" -> number, commas stripped, ignores currency words.
export function parseMoney(text: string | null): number | null;

// Acres -> SF (x 43560). Accepts "3.83 acres" / "3.83 ac" / bare number+unit.
export function acresToSf(text: string | null): number | null;

// Amount where a non-USD currency LABEL is present but the value is really USD
// (NAI 'POUND ' prefix). Strips any leading currency word/symbol, returns the
// numeric. Used ONLY where the gap doc proves the label is wrong.
export function parseAmountIgnoringCurrencyLabel(text: string | null): number | null;

// Percent string -> fraction in (0,1], e.g. "87.5%" -> 0.875, "0.875" -> 0.875.
export function parsePercentToFraction(text: string | null): number | null;

// Building class normalizer: any cased "Class A"/"A"/"office.medical (B)" -> 'A'|'B'|'C'|'D'|null.
export function normBuildingClass(text: string | null): "A" | "B" | "C" | "D" | null;

// Size text -> { sizeSf, lotSf }, routing an "Acres" token to lotSf (x43560).
export function parseSizeText(text: string | null): { sizeSf: number | null; lotSf: number | null };

// Guard: is this free text a PER-SF price (so it must NOT be read as an absolute
// sale price)? Mirrors util.isPerSfPriceText; re-exported here for the Lee guard.
export function isPerSfText(text: string | null): boolean;
```

Semantics (locked, identical to Python):

- `parseLeaseRate`: trust a value ONLY when the text is explicitly per-SF
  (`/sf`, `psf`, `per square foot`, `square f`). Annualize a per-month value
  (`/mo`, `month`) by x12. Reject a value > 500 $/SF/yr (implausible) and the AY
  `$5000/SF/YR` anomaly. For a money RANGE, reject when `max > 100 && min < 100`
  (a suite-size range mis-typed as a money range, the Buildout case). `type` from
  the trailing token: NNN/triple net -> `nnn`; modified gross / mod gross ->
  `modified_gross`; full service / FSG -> `full_service`; bare gross -> `gross`;
  else null. Order: modified_gross, full_service, nnn, gross (specific first).
- `parseMoney`: first `\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)` match, commas stripped.
- `acresToSf`: `([0-9][0-9,]*(?:\.[0-9]+)?)\s*ac(res?)?\b` x 43560.
- `parseAmountIgnoringCurrencyLabel`: strip a leading currency word
  (`POUND|GBP|USD|EUR|\$|£|€`) then `parseMoney` on the remainder; the value is
  treated as USD regardless of the stripped label.
- `normBuildingClass`: match `\bClass\s+([A-D])\b` first, else a bare trailing
  `\b([A-D])\b` ONLY when the input is <= 2 tokens (avoid matching a stray letter
  in prose), else map a known subtype suffix. Returns uppercase A/B/C/D or null.

### C.2 `lib/geo.ts` (TypeScript)

The TS geo lib is for FORWARD-PATH adapter use where an adapter wants to attach a
derived market hint. The authoritative crosswalk LOOKUP at scale runs in Python
(`cre_geo.py`) against the committed CSV, because the backfill of 87k rows and the
ingest both run in Python. `lib/geo.ts` exposes only the pure normalizers (no file
I/O at import time, mirroring the harvest no-argv rule):

```ts
// Normalize a 9-digit or ZIP+4 to a 5-digit ZIP, else null.
export function zip5(raw: string | null): string | null;
// Round lat/lng to a stable key precision (4 dp) for crosswalk matching.
export function geoKey(lat: number | null, lng: number | null): string | null;
```

The actual ZIP->county/CBSA resolution is `cre_geo.py` (C.4). Adapters do NOT do
the crosswalk lookup; they emit `postalCode`/`latitude`/`longitude` (already
required) and the ingest/backfill derives geo. So `lib/geo.ts` stays tiny and
import-safe; this avoids shipping the crosswalk CSV into the Node bundle.

### C.3 `cre_parse.py` (Python mirror: used by `cre_ingest.py` and the backfill)

```python
def parse_lease_rate(text):    # -> (min, max, type)  type in the 4 CHECK tokens or None
def parse_money(text):         # -> float | None
def acres_to_sf(text):         # -> float | None
def parse_amount_ignoring_currency_label(text):  # -> float | None
def parse_percent_to_fraction(text):             # -> float | None  in (0,1]
def norm_building_class(text): # -> 'A'|'B'|'C'|'D'|None
def parse_size_text(text):     # -> (size_sf, lot_sf)
def is_per_sf_text(text):      # -> bool
```

`cre_parse.py` is the SINGLE source of truth for these in Python.
`cre_ingest.py`'s existing `parse_lease_rates`, `parse_money`, `parse_size_text`,
`is_sale_psf_text`, `norm_lease_rate_type`, `norm_occupancy_rate` are REFACTORED
to delegate to `cre_parse.py` (so the monitor and backfill share one
implementation), WITHOUT changing their current observable behavior (verified by
the existing `test_price_*`, `test_norm_status_*` suites still passing). This
refactor is a Workflow-1 task; it touches `cre_ingest.py` (FOUNDATION-owned).

### C.4 `cre_geo.py` (Python: the crosswalk resolver)

```python
class ZipCbsaCrosswalk:
    """Loads the committed offline ZIP->county+CBSA CSV once; O(1) lookups.
    No network. Used by the backfill script AND (optionally) cre_ingest.py."""
    def __init__(self, csv_path=None): ...          # default: data/zip_cbsa_crosswalk.csv
    def by_zip(self, zip5):  # -> {county, cbsa_code, cbsa_name} | None
    def by_latlng(self, lat, lng):  # -> nearest-centroid match within tolerance | None

def derive_geo(listing_or_row, crosswalk):
    """Returns (county, cbsa_code, cbsa_name, submarket, geo_source).
    Precedence: (1) source-verbatim (Newmark county/market/submarket) -> geo_source='source';
    (2) ZIP crosswalk -> 'crosswalk_zip'; (3) lat/lng crosswalk -> 'crosswalk_latlng';
    submarket fallback per Section E. Never overwrites a source-verbatim value."""
```

### C.5 SHARED GOLDEN TEST-VECTOR TABLE

Workflow 1 writes `tests/fixtures/golden_parse_vectors.json` (an array of
`{fn, input, expected}` objects). BOTH `tests/ts/lib/parse.test.ts` and
`tests/test_cre_parse.py` import this one fixture and assert their parser produces
`expected`. This is how the TS and Python parsers are proven identical.

`fn` is one of `parseLeaseRate|parseMoney|acresToSf|parseAmountIgnoringCurrencyLabel|parsePercentToFraction|normBuildingClass|parseSizeText`.
`expected` for `parseLeaseRate` is `{min,max,type}`; for `parseSizeText` is
`{sizeSf,lotSf}`; else a scalar/null.

Minimum 20 rows covering the leaseRateText forms per the gap doc:

| # | fn | input | expected |
|---|---|---|---|
| 1 | parseLeaseRate | `"$23.40"` (M&M Rent Per SF) | `{min:23.40,max:null,type:null}` |
| 2 | parseLeaseRate | `"$30.60 (Annual) USD"` (Cushman) | `{min:30.60,max:null,type:null}` |
| 3 | parseLeaseRate | `"$35 - 45 SF/yr (NNN)"` (SVN range) | `{min:35,max:45,type:"nnn"}` |
| 4 | parseLeaseRate | `"$19 SF/yr ($10.00/SF NNN)"` (Lee dual) | `{min:19,max:null,type:"nnn"}` |
| 5 | parseLeaseRate | `"$1.59 - 1.70 SF/month"` (Lee monthly range) | `{min:19.08,max:20.40,type:null}` |
| 6 | parseLeaseRate | `"3.59 USD/SF/MO"` (CBRE monthly) | `{min:43.08,max:null,type:null}` |
| 7 | parseLeaseRate | `"$5000/SF/YR"` (AY anomaly) | `{min:null,max:null,type:null}` |
| 8 | parseLeaseRate | `"$24.00/SF/YR, FSG"` (Cushman FSG) | `{min:24,max:null,type:"full_service"}` |
| 9 | parseLeaseRate | `"$18.50 SF/yr Modified Gross"` (Colliers MG) | `{min:18.50,max:null,type:"modified_gross"}` |
| 10 | parseLeaseRate | `"$2.50 - 250 SF/month"` (Buildout suite-size mis-range) | `{min:null,max:null,type:null}` |
| 11 | parseLeaseRate | `"Negotiable"` | `{min:null,max:null,type:null}` |
| 12 | parseLeaseRate | `"$22 - $26 PSF Gross"` (AY range gross) | `{min:22,max:26,type:"gross"}` |
| 13 | parseLeaseRate | `"$0.95/SF/MO IG"` (industrial gross monthly) | `{min:11.40,max:null,type:"gross"}` |
| 14 | acresToSf | `"3.83 acres"` | `166774.8` |
| 15 | acresToSf | `"0.5 ac"` | `21780` |
| 16 | parseSizeText | `"12,500 SF on 2.0 Acres"` | `{sizeSf:12500,lotSf:87120}` |
| 17 | parseAmountIgnoringCurrencyLabel | `"POUND 8,585,673.00"` (NAI) | `8585673.00` |
| 18 | parseAmountIgnoringCurrencyLabel | `"$8,585,673"` | `8585673` |
| 19 | parseMoney | `"$272.07"` (M&M Price/Gross SF) | `272.07` |
| 20 | parsePercentToFraction | `"87.5%"` (M&M Occupancy) | `0.875` |
| 21 | parsePercentToFraction | `"0.875"` | `0.875` |
| 22 | normBuildingClass | `"Class A"` (JLL) | `"A"` |
| 23 | normBuildingClass | `"office.medical"` | `null` |
| 24 | isPerSfText (bool) | `"$6.00/SF"` (Lee salePriceUsd conflation) | `true` |
| 25 | parseMoney + isPerSfText | `"$6.00/SF"` salePriceText guard | money=6.00, isPerSf=true -> sale_price suppressed |

Row 25 documents the Lee `salePriceUsd` per-SF conflation guard: when
`isPerSfText(salePriceText)` is true, the absolute `sale_price_usd` is set NULL
and the value is routed to `sale_price_per_sf` (this is the EXISTING
`is_sale_psf_text` branch in `to_row`, 682-684; the backfill replicates it).

Rows 5/6/13 lock the monthly->annual x12 annualization; row 7 locks the AY sanity
cap; row 10 locks the Buildout suite-size-as-money-range rejection. Workflow 1 may
ADD rows but MUST NOT change an existing expected value without updating both test
files in the same change.

## D. DOC_TYPE classification rules

`doc_type` is decided from URL + title by `classifyDoc` in `lib/harvest.ts`
(already shipped, 210-242) on the forward path, and by a mirrored Python
`classify_doc(url, title)` in `cre_parse.py` on the backfill/parse path. The
allowed tokens are the widened set from 011: `om, brochure, flyer, floor_plan,
financials, rent_roll, other`. WS2 does NOT change the TS `classifyDoc` (it is
FOUNDATION/already-shipped); it ADDS the Python mirror used by the OM-parse tier.

Decision order (most specific first, matching `classifyDoc`):

1. `rent_roll` if hay matches `rent[-_ ]?roll`.
2. `financials` if `financ|pro[-_ ]?forma|proforma|\bt-?12\b`.
3. `floor_plan` if `floor[-_ ]?plan|site[-_ ]?plan|floorplan|siteplan`.
4. `om` if `offering|memorandum|(?:^|[/_-])om(?:[/_.-]|$)|teaser|dataroom|data[-_ ]room|deal[-_ ]room`.
5. `flyer` if `flyer`.
6. `brochure` if `brochure|marketing|\bpackage\b|\bdeck\b|\bpib\b`.
7. `other` if a bare document extension or a recognized hosted-download link
   (Buildout `/sharing/` or `?file=<id>`) with no keyword.

**The 70,414 existing `brochure` rows** are handled as follows:

- A one-time, ADDITIVE re-classification pass (part of the WS2 doc-classification
  script, `om_classify_existing.py`) re-runs `classify_doc(url, title)` over every
  existing `cre_listing_documents` row currently typed `brochure` and UPDATEs
  `doc_type` ONLY when the classifier yields a MORE SPECIFIC type
  (om/financials/rent_roll/floor_plan/flyer). It NEVER downgrades a more-specific
  existing type to `brochure`, and NEVER touches a row already typed
  non-`brochure`. A row that still classifies as `brochure` (or only as `other`)
  is LEFT as `brochure` (no demotion to `other`; `brochure` is the safe default
  the rows already carry).
- This UPDATE is its own statement, `to_regclass`-guarded harmless (the table
  exists), `--dry-run` default, `--apply` gated, and emits a per-old/new-type
  count summary before applying.
- The OM-parse tier (WS2) keys document SELECTION off `doc_type IN
  ('om','financials','rent_roll','brochure')` AND a `.pdf`/viewer URL shape, so
  re-classification improves prioritization (CBRE+JLL `om`/`financials` first) but
  is NOT a precondition for parsing.

## E. GEO crosswalk dataset

DATASET: the **HUD-USPS ZIP Code Crosswalk (ZIP-COUNTY)** joined to the **Census
CBSA-to-county delineation**, distilled to one committed CSV. Both are US-gov
public-domain (no license restriction), which satisfies the "free, offline, no
scrape" requirement.

- **What ships:** a single committed file `cre_collector/data/zip_cbsa_crosswalk.csv`
  with columns `zip5, county_fips, county_name, state, cbsa_code, cbsa_name,
  centroid_lat, centroid_lng`. ~41,000 rows (one per US ZIP; ZIPs spanning
  multiple counties keep the highest-residential-ratio county per the HUD
  `RES_RATIO`). Approx size ~3.5 MB uncommitted, ~1.2 MB gzipped. It IS committed
  (it is durable reference data, not a run artifact); add `data/` to the
  collector tree (NOT gitignored) with a `data/README.md` citing the HUD vintage
  (e.g. `HUD ZIP-COUNTY 2026Q1`) and Census delineation vintage and the build
  command that produced it.
- **License:** US Government public domain (HUD USPS Crosswalk Files + Census
  Bureau delineation files). No attribution required; README records provenance
  anyway.
- **How it loads:** `014_cre_geo_crosswalk.sql` creates
  `credeals.cre_zip_cbsa_crosswalk` (same columns, `zip5 text PRIMARY KEY` is
  wrong because multi-county; use `id uuid PK` + UNIQUE `(zip5)` after the
  highest-ratio dedup) and `\copy credeals.cre_zip_cbsa_crosswalk FROM
  'zip_cbsa_crosswalk.csv' WITH (FORMAT csv, HEADER true)`. The backfill /
  `cre_geo.py` load the SAME CSV directly from disk (no DB round-trip needed for
  the 87k backfill), so the table is for consumer/ad-hoc SQL joins and the CSV is
  for the Python path; they are byte-identical (same committed file).
- **A build script** `data/build_zip_cbsa_crosswalk.py` (committed, run offline by
  the geo agent) downloads the two gov sources, joins, dedups by RES_RATIO, and
  emits the CSV deterministically, so the file is reproducible and reviewable.

**Submarket fallback rule** (`cre_geo.py derive_geo`):

1. If the source provides `submarket` verbatim (Newmark, AY `submarket`, JLL
   `jllDetail.submarket`), KEEP it; `geo_source='source'` for the geo bundle.
2. Else `submarket` stays NULL. We do NOT fabricate a submarket from the
   crosswalk (CBSA granularity is metro, not submarket; inventing a submarket
   would be a false-precision claim). `county`, `cbsa_code`, `cbsa_name` ARE
   derived from the crosswalk; `submarket` is source-only.
3. `market` column: set to `cbsa_name` when the source did not provide a `market`
   verbatim; KEEP the source `market` when present (Newmark gives it).

This keeps geo strictly additive and never overwrites a higher-confidence
source-verbatim value (COALESCE-keep), matching the invariant.

## F. FILE-OWNERSHIP MAP (Workflow 2, disjoint)

FOUNDATION files (built in Workflow 1, FROZEN in Workflow 2, edited by NO
Workflow-2 agent): `cre_ingest.py`, `types.ts`, `lib/parse.ts`, `lib/geo.ts`,
`lib/harvest.ts`, `lib/scrape.ts`, `lib/util.ts`, `lib/enrich.ts`,
`cre_parse.py`, `cre_geo.py`, all `sql/*.sql`, `tests/fixtures/golden_parse_vectors.json`.

Each Workflow-2 agent owns exactly the files listed; no two share a file.

| Owner | Files (disjoint) |
|---|---|
| agent-src-marcus | `sources/marcus-millichap.ts`, `tests/ts/sources/marcus-millichap.test.ts` |
| agent-src-avison | `sources/avison-young.ts`, `tests/ts/sources/avison-young.test.ts` |
| agent-src-newmark | `sources/newmark.ts`, `tests/ts/sources/newmark.test.ts` |
| agent-src-transwestern | `sources/transwestern.ts`, `tests/ts/sources/transwestern.test.ts` |
| agent-src-jll | `sources/jll.ts`, `sources/jll-investor.ts`, `tests/ts/sources/jll.test.ts`, `tests/ts/sources/jll-investor.test.ts` |
| agent-src-cbre | `sources/cbre.ts`, `sources/cbre-dealflow.ts`, `tests/ts/sources/cbre.test.ts`, `tests/ts/sources/cbre-dealflow.test.ts` |
| agent-src-cushman | `sources/cushman-wakefield.ts`, `tests/ts/sources/cushman-wakefield.test.ts` |
| agent-src-colliers | `sources/colliers.ts`, `sources/colliers-main.ts`, `tests/ts/sources/colliers.test.ts`, `tests/ts/sources/colliers-main.test.ts` |
| agent-src-buildout | `sources/buildout.ts`, `tests/ts/sources/buildout.test.ts` (svn + lee share this adapter) |
| agent-src-nai | `sources/nai-global.ts`, `tests/ts/sources/nai-global.test.ts` |
| agent-src-savills | `sources/savills.ts`, `tests/ts/sources/savills-commercial.test.ts`, `tests/ts/sources/savills.test.ts` |
| agent-backfill | `cre_backfill_raw_data.py`, `tests/test_backfill_raw_data.py` |
| agent-om-parse | `om_parse.py`, `om_url_resolver.py`, `tests/test_om_parse.py`, `tests/test_om_url_resolver.py` |
| agent-doc-classify | `om_classify_existing.py`, `tests/test_doc_classify.py` |
| agent-geo | `cre_geo_backfill.py`, `data/build_zip_cbsa_crosswalk.py`, `data/zip_cbsa_crosswalk.csv`, `data/README.md`, `tests/test_geo_derive.py` |
| agent-dq-guards | `tests/test_dq_guards.py` (asserts the 6 guards against `cre_parse.py`/`cre_geo.py`; ADD-only test file, touches no FOUNDATION file) |
| agent-enrich-wire | `tests/test_om_enrich_wiring.py` (asserts that `cre_enrich.py` and `lib/enrich.ts` have no OM-parse invocation; this is a retired-writer regression guard) |

Notes:
- The former OM-parse worker wiring is retired. `cre_enrich.py` and
  `lib/enrich.ts` must not invoke `om_parse.py`, and the regression test guards
  that absence so a legacy environment cannot recreate a second writer.
- `collect.ts` is FOUNDATION (Workflow 1) for any new source-key registration; no
  source agent edits it (none of these are new sources, only field additions, so
  `collect.ts` likely needs no change beyond passing new fields through, which it
  already does via the `any` listing object).
- Each source agent's ONLY cross-file dependency is READING `lib/parse.ts` /
  `lib/geo.ts` (frozen) and emitting the Section-B camelCase fields. No source
  agent imports another source file.

## G. TEST PLAN

Every new/edited test file and its key cases. TS tests use Node `node:test` +
`tsx` (`npm run test:unit`); Python uses pytest (`python3 -m pytest tests/`).
Re-run counts to confirm (the suite is parametrized).

**Workflow-1 (FOUNDATION) tests:**

- `tests/ts/lib/parse.test.ts`: loads `golden_parse_vectors.json`; asserts every
  vector for the TS side; plus null/garbage inputs return null/empty, never throw.
- `tests/test_cre_parse.py`: loads the SAME fixture; asserts every vector for the
  Python side. Cross-checks identical to TS. Plus: the existing `parse_lease_rates`
  / `parse_money` / `parse_size_text` delegate to `cre_parse` and still pass the
  current `test_price_coalesce.py` expectations (regression guard on the refactor).
- `tests/ts/lib/geo.test.ts`: `zip5`/`geoKey` normalization edge cases.
- `tests/test_cre_geo.py`: `ZipCbsaCrosswalk` loads a tiny FIXTURE csv (not the
  3.5 MB file); `by_zip` hit/miss; `by_latlng` nearest within/outside tolerance;
  `derive_geo` precedence (source > zip > latlng), submarket source-only, market
  COALESCE-keep, `geo_source` value correctness.
- Edits to existing FOUNDATION tests: `tests/test_media_links_ingest.py` /
  `test_price_coalesce.py` get NEW cases asserting the new columns
  (`building_class`, `extra_facts`, `cbsa_*`, contact `license`) stage + COALESCE-
  keep correctly, and that `om_facts` rows are emitted with provenance. The
  `om_facts` archive-on-mark-missing case extends `test_child_history_archive_on_retirement.py`.

**Workflow-2 (PARALLEL) tests:**

- Each `tests/ts/sources/<broker>.test.ts` gains cases that feed a SAVED
  `raw_data` sample blob (fixture, Section below) through the adapter's pure
  parse/base-listing function and asserts the new Section-B camelCase fields are
  emitted with correct values (e.g. marcus: `tenantName`/`guarantor`/`grm` from
  `marcusSpecifications`; transwestern: `clearHeightFt`/`dockDoors`/`apn` from
  `transwesternFacts`; newmark: `propertySubtype`/`statusBadge`/`county`; avison:
  `buildingClass` from subtype, `canonicalUrl`). Each asserts the `extra_facts`
  long-tail keys land. No network.
- `tests/test_backfill_raw_data.py`: feeds per-source `raw_data` blobs through the
  backfill's pure column-derivation function; asserts the dual-mode
  `COALESCE(primary, secondary_pass, top-level)` lift, the `canonical_url`
  universal rename, and per-source scalar lift (M&M marcusSpecifications, AY
  rawSharpLaunch, Newmark county/submarket, JLL submarket/buildingClass,
  Transwestern facts/availability). Asserts COALESCE-keep semantics in the
  generated UPDATE SQL (never blanks a good value) and `to_regclass`-guarded shape.
- `tests/test_om_parse.py`: feeds a SAVED `/v2/parse` output fixture (CBRE OM +
  JLL OM, Section below) through the pure OM-field extractor; asserts NOI /
  cap_rate / occupancy / units / year_built extraction, unit_mix/rent_roll row
  shaping, and that EVERY emitted scalar carries provenance (source_doc_url,
  parser_version, confidence). Asserts a non-underwriting PDF yields zero scalars
  (no fabrication).
- `tests/test_om_url_resolver.py`: viewer-wrapped/non-`.pdf` URL -> resolved
  `.pdf` URL for Cushman/Colliers/Lee/SVN shapes; an unresolvable URL returns null
  (no guess).
- `tests/test_doc_classify.py`: `classify_doc` Python mirror matches the TS
  `classifyDoc` on a shared set of URL/title pairs; the existing-`brochure`
  re-classification UPDATE only ever upgrades to a more-specific type and never
  downgrades; dry-run count summary shape.
- `tests/test_geo_derive.py`: (agent-geo) end-to-end: a row with only zip -> county
  + cbsa; a row with only lat/lng -> crosswalk_latlng; a Newmark row keeps source
  county/market/submarket; geo_source correctness; submarket never fabricated.
- `tests/test_dq_guards.py`: (agent-dq-guards) asserts all 6 guards via the
  parser/geo libs: (1) NAI POUND->USD, (2) Lee salePriceUsd per-SF conflation,
  (3) AY `$5000/SF/YR` cap, (4) dual-mode primary/secondary_pass COALESCE,
  (5) Transwestern Land Area (ac) SF-vs-acres validation, (6) Newmark sale_price
  'Subject to Offer' rejection.
- `tests/test_om_enrich_wiring.py`: (agent-enrich-wire) asserts that
  `cre_enrich.py` and `lib/enrich.ts` expose no OM-parse path or invocation.

**Fixtures needed:**

- `tests/fixtures/golden_parse_vectors.json` (FOUNDATION; Section C.5).
- `tests/fixtures/raw_data/<source>.json`: one saved real `raw_data` sample blob
  per source (M&M marcusSpecifications + contactsDetailed; AY rawSharpLaunch;
  Newmark rawNewmarkHit; JLL jllDetail + jllInvestorDetail; Transwestern
  transwesternFacts + availability; CBRE flat + dealflow; Cushman; SVN; Lee;
  Colliers main + ST; NAI publicPost; Savills rawSavillsProperty). Scrubbed of any
  non-public data, kept small (1-3 listings each). Each source agent contributes
  ITS fixture; the backfill agent reads them read-only.
- `tests/fixtures/parse/cbre_om.json`, `tests/fixtures/parse/jll_om.json`: saved
  local `/v2/parse` (Docling) outputs for one CBRE and one JLL OM PDF, used by
  `test_om_parse.py` (no live parse in tests).
- `tests/fixtures/geo/mini_crosswalk.csv`: ~20-row crosswalk slice for
  `test_cre_geo.py`/`test_geo_derive.py` (NOT the full 41k file).

## H. INVARIANTS (reaffirmed) + open risks

**Invariants every agent honors (no exceptions):**

- Additive-only. No DROP/rename of a column, table, or view; no narrowing.
- `to_regclass`-guarded INSERTs for any table that may not be applied yet
  (`cre_listing_om_facts`, its archive, `cre_zip_cbsa_crosswalk`), exactly like
  the 009/011 media/links/history guards already in `build_sql`.
- COALESCE-keep on every new scalar column so a sparse pass never clobbers good
  data; `extra_facts` merges (jsonb `||`) rather than replaces, with a NULL/empty
  guard so an empty pass keeps the prior blob.
- detailError-excluded child refresh: `cre_listing_om_facts` writes ride the same
  `_child_refresh` set (excludes `jsonb_path_exists(raw_data,'$.**.detailError')`).
- No-narrow `transaction_type` (the existing CASE in the upsert is untouched).
- Status badges (WS3) route to the EXISTING OPT-IN activation gate
  (`apply_status_activation_gate` / `_status_activation_enabled`); they are NEVER
  written directly to `cre_listings.status` and NEVER auto-activate. A new source
  status signal is added to `STATUS_SOURCE_PATHS`, nothing more.
- mark-missing untouched (its predicate and floor are unchanged; the only addition
  is the `cre_listing_om_facts_archive` snapshot, guarded).
- Monitor enumeration byte-identical: any new field promotion on a source's full
  path is gated behind `!monitor` (mirroring newmark.ts:250), so the monitor
  artifact `cre_monitor.py` reads never changes.
- Every new write path has unit + pytest coverage and a `--dry-run` before any
  live apply. Everything gated; nothing applied to prod; nothing committed.
- Never print or commit a connection string. No em dashes in prose. Never the
  words "genuinely", "honestly", "straightforward".

**Open risks:**

1. The HUD ZIP-COUNTY crosswalk maps a multi-county ZIP to its
   highest-residential-ratio county, which can mis-assign a commercial property in
   the minority county of a split ZIP. Mitigation: prefer the lat/lng centroid
   match when both zip and lat/lng are present and disagree; record `geo_source`.
2. The `cre_parse.py` refactor of the existing `parse_lease_rates`/`parse_money`
   must not change current behavior; a behavior drift would silently move the 87k
   existing numeric values on the next ingest. Mitigation: the regression guard in
   `test_cre_parse.py` re-asserts the current `test_price_coalesce` expectations.
3. The existing-`brochure` re-classification UPDATE touches 70,414 rows; an
   over-broad regex could upgrade a true brochure to `om`. Mitigation: upgrade-only
   semantics, dry-run count review, and the shared `classify_doc` golden cases.
4. OM-parse scalar confidence is heuristic; a wrong NOI/cap_rate written onto a
   board-facing column could mislead. Mitigation: COALESCE-keep does not overwrite
   a higher-confidence source value, provenance is recorded per scalar, and a
   confidence floor (e.g. < 0.6 writes only `cre_listing_om_facts`, not the
   `cre_listings` column) is enforced in `om_parse.py`.
5. The 3.5 MB committed crosswalk CSV grows the repo; acceptable as durable
   reference data, but the build script must be deterministic so reviewers can
   diff it. Mitigation: `data/build_zip_cbsa_crosswalk.py` + recorded vintages.
6. Adapter agents working in parallel could each independently want a NEW
   `extra_facts` key colliding on naming. Mitigation: this contract is the single
   key registry; new `extra_facts` keys are snake_case and namespaced by source
   only when ambiguous (e.g. `tw_typical_floor_size`).
7. `lib/parse.ts` and `cre_parse.py` are authored by ONE agent (Workflow 1) but
   consumed by ~12 agents; a late change to a signature breaks every consumer.
   Mitigation: signatures in Section C are frozen at Workflow-1 sign-off; only
   additive golden-vector rows are allowed after.
```
