# Newmark Scraper Notes

Production bulk collection uses Newmark's Algolia search API in `cre_collector/collect.ts`.

## Algolia Discovery

- Read `algoliaAppId`, `algoliaSearchApiKey`, and `algoliaIndexName` from `https://www.nmrk.com/properties`.
- Query the Algolia index with:
  - `sectionGroup:Properties`
  - `saleOrLease:Sale` or `saleOrLease:Lease`
  - `country_code:US`
  - `siteHandle:enUs`
- Algolia caps retrievable hits per query, so the production collector splits by state and, when needed, property type.

## Data Shape

Hits expose title, slug, address, city, state, zip, coordinates, sale or lease mode, property types, thumbnails, and size fields.

## 2026-06-12 Deep Dive Notes

Scope: Newmark only, source key `newmark`. This was a bounded read-only probe for
EQUIRE's URL-only Supabase dataset. No binaries were downloaded, no Supabase
ingest was run, and no collector code was changed.

### Commands And Artifacts

Local Firecrawl health and collector shape:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=newmark --transaction=both --max-items=5 --page-cap=5 --concurrency=2 --out=/tmp/newmark_small_probe_2026-06-12.json
```

Saved probe artifacts:

- `/tmp/newmark_small_probe_2026-06-12.json`
- `/tmp/newmark_properties_page_2026-06-12.json`
- `/tmp/newmark_algolia_count_probe_2026-06-12.json`
- `/tmp/newmark_lease_gap_recovery_2026-06-12.json`
- `/tmp/newmark_no_state_lease_probe_2026-06-12.json`
- `/tmp/newmark_detail_701_8th_lease_2026-06-12.json`
- `/tmp/newmark_detail_1919_sterling_sale_2026-06-12.json`
- `/tmp/newmark_detail_8230_baycenter_lease_2026-06-12.json`
- `/tmp/newmark_detail_1919_wait10_2026-06-12.json`
- `/tmp/newmark_detail_enrichment_summary_2026-06-12.json`
- `/tmp/newmark_index_js_2026-06-12.json`
- `/tmp/buildout_api_js_v8_2026-06-12.json`
- `/tmp/newmark_algolia_hit_shape_2026-06-12.json`
- `/tmp/newmark_people_algolia_probe_2026-06-12.json`

The Algolia search key was treated as public page configuration but is not
recorded here. Saved summaries redact it.

### Endpoint And Count Evidence

Best public discovery path remains the Algolia index exposed from
`https://www.nmrk.com/properties`:

- Host pattern: `https://<appId>-dsn.algolia.net/1/indexes/<indexName>`.
- Base filters: `sectionGroup:Properties`, `saleOrLease:Sale` or
  `saleOrLease:Lease`, `country_code:US`, `siteHandle:enUs`.
- Current count evidence from local Firecrawl mediated Algolia probes:
  - Sale: `nbHits=1121`, collector-style state retrieval found `1121`, gap `0`.
  - Lease: `nbHits=3250`, collector-style state retrieval found `3247`, gap `3`.

The 3-row lease gap still exists. It is not caused by California or
property-type splitting. California lease reported `1243` hits and reconciled to
`1243` unique hits after property-type sub-splits. The exact gap is that
Algolia's lease `state` facet sums to `3247`, while `nbHits` is `3250`.

The three missing lease rows are valid Washington, DC listings with no `state`
facet:

- `buildout_1400130_lease_enUs`: `701 8th St NW`,
  `/properties/701-8th-st-nw-washington-lease`, ZIP `20001`.
- `buildout_1399007_lease_enUs`: `The Wharf`,
  `/properties/800-maine-avenue-southwest-washington-lease`, ZIP `20024`.
- `buildout_1397698_lease_enUs`: `1800 Massachusetts Ave NW`,
  `/properties/1800-massachusetts-avenue-northwest-washington-lease`, ZIP
  `20036`.

They were isolated with an Algolia `filters` query that kept the base filters
and excluded every observed `state` facet value. That returned `nbHits=3`.

### Detail Enrichment Feasibility

Detail page scraping through local Firecrawl is weak for Newmark. Sample pages:

- `https://www.nmrk.com/properties/701-8th-st-nw-washington-lease`
- `https://www.nmrk.com/properties/1919-sterling-palms-ct-brandon-sale`
- `https://www.nmrk.com/properties/8230-baycenter-rd-jacksonville-lease`

Observed detail behavior:

- Static HTML contains the title, canonical/meta fields, JSON-LD for the site
  and breadcrumbs, and a `data-module="buildout"` mount.
- A 10-second local Firecrawl wait still did not hydrate listing facts,
  contacts, brochures, or gallery images.
- Raw HTML only exposed site-wide legal PDFs, placeholder or favicon images,
  and Newmark social/meta images. Those should not be stored as listing
  documents or listing photos.
- Newmark's frontend bundle mounts a consent-gated Buildout widget. It loads
  `https://buildout.com/api.js?v8` only after Usercentrics Buildout consent and
  requires `window.buildoutConfig` fields such as `token`, `plugin`, `target`,
  optional `rootPath`, and optional `forceDomain`.
- The sampled static detail pages did not expose the Buildout token or plugin
  configuration needed to construct the direct Buildout iframe URL.

Conclusion: do not treat Newmark detail pages as a reliable source for
documents, full galleries, or contacts under the current local Firecrawl path.
Use Algolia for discovery and available structured fields. Only add Buildout
detail enrichment later if a public token/config path is found and can be
proved without consent bypassing or binary downloads.

### Contact And Profile Feasibility

Algolia listing hits expose these broker fields:

- `broker_id`
- `broker_ids`
- `broker_name`
- `second_broker_id`
- `third_broker_id`

The current collector drops these by setting `brokerIds: []`.

The same Algolia index can enrich first broker names through `sectionGroup:People`
and `siteHandle:enUs`. Name queries returned public profile/contact records:

- `Andrew Visnick`: profile `https://www.nmrk.com/people/andrew-visnick`,
  email and phone present.
- `Jeff Sanita`: profile `https://www.nmrk.com/people/jeff-sanita`, email and
  phone present.
- `Mark Repstad`: profile `https://www.nmrk.com/people/mark-repstad`, email and
  phone present.
- `Kevin Hansen`: profile `https://www.nmrk.com/people/kevin-hansen`, email and
  phone present.

Direct People facet filters by listing `broker_id` or `broker_ids` returned
zero hits in the samples, so the numeric Buildout broker IDs do not directly
join to Newmark People records. First broker enrichment by exact public name is
feasible. Second and third broker IDs remain opaque unless a separate Buildout
broker mapping is found.

### Missing Fields

Reliable today:

- Sale and lease counts except the current 3-row lease state-facet gap.
- Stable source URL and slug.
- Title, headline/content, transaction type, property type, address, city,
  state where present, ZIP, county, submarket, market, coordinates, status,
  sale price text/value, building size, lot size, unit count, post/update dates.
- One high-resolution thumbnail URL from Algolia.
- First broker name in many rows, plus public People profile/email/phone when
  the name query matches.

Missing or not reliable today:

- Listing brochure, flyer, OM, or floor-plan URLs.
- Full image gallery beyond Algolia thumbnails.
- VCard URLs.
- Contacts for rows where `broker_name` is absent but only numeric broker IDs
  are present.
- Second and third broker names/contact details when Algolia listing rows expose
  only numeric IDs.
- `state` on the three Washington, DC lease rows unless inferred from city/ZIP
  or recovered through the no-state query path.

### Status

Newmark is a strong public-feed source via Algolia, but it is not yet a complete
deep-enriched source. Current status should remain: active via Algolia, needs
deep audit. The public feed count can be closed to `1121` sale and `3250` lease
with a small no-state recovery query.

### Concrete Collector Patch Plan

Keep the patch scoped to Newmark in `cre_collector/collect.ts`.

1. Preserve the current base Algolia credential scrape and base filters.
2. Keep the state split and property-type sub-split for over-cap states.
3. After reading `first.facets.state`, compute `stateFacetSum`. If
   `stateFacetSum < total`, run a recovery query using Algolia `filters`:
   base filters plus `NOT state:"<facet value>"` for every observed state.
   Add those hits to the same `hitMap`.
4. If the recovery query reports more than `1000` hits, sub-split recovery by
   `property_types`, then by another facet if needed. Current evidence is only
   `3`, so this is a guardrail rather than expected runtime.
5. For Newmark row mapping, add `rawNewmark` or equivalent fields into
   `raw_data` by leaving the original hit payload attached where practical.
6. Populate `contactsDetailed` from public People Algolia lookups when
   `broker_name` is present:
   - Query `sectionGroup:People`, `siteHandle:enUs`, `query=<broker_name>`.
   - Accept exact case-insensitive title matches first.
   - Store `name`, `email`, `phone`, `company: "Newmark"`, `profileUrl`, and
     optional office/title fields.
   - Cache people lookups by broker name for the run.
7. Also preserve `broker_id`, `broker_ids`, `second_broker_id`,
   `third_broker_id`, and `broker_name` in `raw_data` for future Buildout
   mapping.
8. Set state for the known no-state Washington, DC shape by using
   `state_code` when present, otherwise infer `DC` only when `city` is
   `Washington` and ZIP starts with `200`.
9. Keep documents empty unless a listing-specific public document URL is found.
   Do not capture Newmark site-wide legal PDFs as listing documents.
10. Keep images to Algolia thumbnail URLs only for now. Prefer the largest
    thumbnail in `h.thumbnails`; do not store placeholder/favicon/meta images
    from detail pages.
11. Verify with:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=newmark --transaction=both --max-items=0 --page-cap=5 --concurrency=2 --out=/tmp/newmark_after_patch_probe.json
python3 cre_ingest.py --in /tmp/newmark_after_patch_probe.json --dry-run --keep-artifacts /tmp/newmark_after_patch_ingest_check
```

Expected post-patch proof:

- Sale collected: `1121` of `1121`.
- Lease collected: `3250` of `3250`.
- Three Washington, DC lease rows included.
- Newmark contacts appear for rows with exact People matches.
- Document child rows remain empty unless listing-specific document URLs are
  later proven.
- Image child rows store Algolia thumbnail URLs only.

### 2026-06-12 Full Run And Live Ingest (Superseded By Refined Reload)

The no-state recovery patch was verified and ingested additively. This section
is historical. The refined reload below is the current Newmark production proof.

Commands:

```bash
cd /Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=newmark --transaction=both --max-items=0 --out=/tmp/newmark_no_state_full_probe.json
python3 cre_ingest.py --in out/newmark_full_2026-06-12_no_state_recovery.json --dry-run --keep-artifacts /tmp/newmark_full_2026-06-12_no_state_recovery_ingest_check
python3 cre_ingest.py --in out/newmark_full_2026-06-12_no_state_recovery.json --keep-artifacts /tmp/newmark_full_2026-06-12_no_state_recovery_live_ingest
```

Saved artifact:

- `out/newmark_full_2026-06-12_no_state_recovery.json`, copied from the verified full probe so the evidence lives under `out/`.

Results:

- Collected 4,371 rows: 1,121 sale and 3,250 lease.
- Dry-run staged 4,371 rows and skipped 0 missing URLs.
- Live additive ingest completed without `--mark-missing`.
- Latest Supabase Newmark batch validation: 4,371 latest rows, 1,121 sale, 3,250 lease, 0 missing URLs, 0 missing titles, 0 missing raw data, 0 bad state codes, 0 impossible coordinates, 0 bad cap rates, 4,303 image child rows, and 0 orphan image rows.
- Active Newmark rows after ingest: 5,086 because previous additive rows remain active until a clean all-source reconciliation is eligible.

Historical limits at this point:

- Newmark is feed-complete for public Algolia listing rows after no-state
  recovery, but not deep-contact complete.
- Listing documents and VCard URLs remain unproven.
- Contacts should be added later through the People Algolia exact-name lookup
  plan above. This contact item was later implemented in the refined reload
  below.

### 2026-06-12 Refined Full Run, Contacts, And Source-Scoped Reconciliation

The contact/state refinement plan above has now been implemented, live-ingested,
and validated.

Commands:

```bash
cd /Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=newmark --transaction=both --max-items=20 --concurrency=3 --out=/tmp/newmark_refinement_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/newmark_refinement_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/newmark_refinement_probe_2026-06-12_ingest_check
npx tsx collect.ts --source=newmark --transaction=both --max-items=0 --concurrency=4 --out=out/newmark_full_refined_2026-06-12.json
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --dry-run --keep-artifacts /tmp/newmark_full_refined_2026-06-12_ingest_check
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --dry-run --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/newmark_full_refined_2026-06-12_mark_missing_check
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --mark-missing --mark-missing-floor 100 --keep-artifacts /tmp/newmark_full_refined_2026-06-12_mark_missing_live
```

Collector changes:

- Algolia listing queries now use direct public JSON requests after the initial
  properties-page credential bootstrap.
- Credential bootstrap retries with a longer render wait when the first scrape
  misses the public Algolia config.
- The three Washington, DC no-state lease rows infer `DC` from city and ZIP.
- Original Algolia hit payloads are preserved as `rawNewmarkHit`.
- Broker provenance fields are preserved in `newmarkBrokerProvenance`.
- Public People Algolia lookup by exact broker name populates
  `contactsDetailed` with name, title, email, phone, office, profile URL, and
  avatar URL when available.
- Profile URLs are normalized to absolute `https://www.nmrk.com/...` URLs.
- Detail-page scraping remains out of scope because the public detail shell is
  noisy and Buildout detail config remains consent-gated.

Full artifact result:

- Artifact: `out/newmark_full_refined_2026-06-12.json`, 22.0 MB.
- Collected 4,371 rows: 1,121 sale and 3,250 lease.
- Missing URLs: 0.
- Missing titles: 0.
- Missing states: 0.
- Washington, DC recovered rows with state `DC`: 3.
- Public People contacts/profile URLs: 3,961.
- Contacts with phone: 3,910.
- Raw Algolia hits preserved: 4,371.
- Broker provenance objects preserved: 4,371.
- Document rows: 0.
- Image URLs: 4,303.

Ingest and Supabase proof:

- Dry-run staged 4,371 rows and skipped 0 missing URLs.
- Source-scoped `--mark-missing` was dry-run and then live-applied only for
  `newmark`.
- Active Newmark rows after cleanup: 4,371, with 1,121 sale and 3,250 lease.
- Old additive rows soft-deleted: 715.
- Live child rows: 4,303 image URL rows, 3,961 contact rows, 3,961 profile URLs,
  and 0 document rows.
- Quality checks: 0 bad source URLs, 0 missing titles, 0 missing raw data, 0
  missing states, 0 invalid states, 0 impossible coordinates, 0 malformed
  guarded prices/cap rates, 0 duplicate external IDs, 0 bad image/profile URLs,
  and 0 child orphans.
- Search proof:
  `credeals.search_cre_listings('Alvista', null, null, null, null)` returned
  the live Newmark `Alvista Sterling Palms` sale row.

Remaining limits:

- Listing-specific documents, full galleries, second/third broker joins, and
  VCard URLs remain unproven.
- Do not store Newmark site-wide legal PDFs or placeholder images as listing
  assets.
