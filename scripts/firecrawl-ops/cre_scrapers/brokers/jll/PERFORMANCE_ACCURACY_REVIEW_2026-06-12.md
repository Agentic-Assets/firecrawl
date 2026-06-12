# JLL Performance And Accuracy Review - 2026-06-12

Scope: source key `jll` in `scripts/firecrawl-ops/cre_collector/collect.ts`.
This note excludes `jll-investor`, which has separate Investor Center behavior.
No live ingest was run. No PDFs or images were downloaded.

## Commands Run

From repo root:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

From `scripts/firecrawl-ops/cre_collector`:

```bash
npm run typecheck
npx tsx collect.ts --source=jll,newmark --transaction=both --max-items=9 --page-cap=1 --concurrency=2 --out=/tmp/jll_newmark_review_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_newmark_review_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_newmark_review_ingest_2026-06-12
jq '{totalListings, sourceCounts:(.listings|group_by(.sourceKey+":"+.transactionMode)|map({key:(.[0].sourceKey+":"+.[0].transactionMode), count:length})), fieldCoverage:(.listings|group_by(.sourceKey)|map({source:.[0].sourceKey,count:length, with_contacts:map(select((.contactsDetailed // [])|length>0))|length, with_brokers:map(select((.brokerIds // [])|length>0))|length, with_docs:map(select((.brochures // [])|length>0))|length, with_photos:map(select((.photos // [])|length>0))|length, with_state:map(select(.state != null))|length, with_latlon:map(select(.latitude != null and .longitude != null))|length}))}' /tmp/jll_newmark_review_probe_2026-06-12.json
```

Reference artifacts inspected:

- `scripts/firecrawl-ops/cre_collector/out/jll_probe_2026-06-12/jll_detail_summary.json`
- `scripts/firecrawl-ops/cre_collector/out/jll_investor_probe_fixed_2026-06-12.json` as contrast only

## Current Collector Shape

The main JLL collector now uses rendered public search pages with
`propertyTypes=<type>` and `tenureTypes=sale|rent`, not the old implicit
office-only search. The 2026-06-12 bounded probe confirmed first-page totals
for all nine configured property type filters:

- Sale: office 333, industrial 492, retail 220, land 765, medical 16,
  multifamily 5, lab 4, coworking 53, data-center 1. Sum before de-dupe:
  1,889.
- Lease: office 4,345, industrial 2,576 in this probe, retail 1,391, land 304,
  medical 170, multifamily 17, lab 199, coworking 524, data-center 4. Sum
  before de-dupe: 9,530.

The small combined probe wrote 18 JLL rows, 9 sale and 9 lease. Dry-run ingest
staged all rows and skipped 0 for missing URL.

## Accuracy Findings

- Search coverage is materially better than the old office-default path because
  it loops every documented public property type.
- Main JLL rows are still search-card rows. In the bounded probe, JLL had:
  18 of 18 with state, 0 of 18 with contacts, 0 of 18 with broker refs, 0 of
  18 with document URLs, 0 of 18 with image URLs, and 0 of 18 with coordinates.
- Existing detail probes show that the missing fields are available on public
  JLL detail pages through `script#__NEXT_DATA__`, not from search cards alone.
  Sample detail pages exposed stable JLL property IDs, title and address,
  property and tenure types, price text, size fields, descriptions, highlights,
  amenities, custom attributes, latitude and longitude, brochure URL arrays,
  Cloudinary image URL arrays, and broker arrays.
- Sample detail evidence from saved artifacts:
  - Westlake Professional Campus: 1 brochure URL, 1 property image in
    structured data, broker record with email, phone, profile slug, avatar, and
    LinkedIn.
  - 615 3rd Street: 1 brochure URL, 5 structured images, 3 broker records.
  - Steuart Street Tower: 1 brochure URL, 8 structured images, 5 broker records.
- VCard URLs were not observed in the sampled public main-JLL detail pages.
  Profile slugs, email, telephone, avatar, and LinkedIn are available.
- Firecrawl link extraction and markdown are incomplete for main JLL. Raw HTML
  and `__NEXT_DATA__` should be the source of truth for detail enrichment.
- JLL pricing remains sparse by source design. Many public rows say "Please
  contact us for price"; detail enrichment will improve price text provenance
  but should not be expected to create broad numeric asking-price coverage.

## Performance Findings

- The search pass is bounded and reasonably efficient: one rendered scrape per
  `(tenure, property type, page)`, with round-robin row selection for small
  probes.
- Full search pagination can still require many rendered page scrapes because
  rent office alone reports 4,345 cards and the collector must honor `PAGE_CAP`.
- A full detail scrape of every de-duped JLL URL could be expensive. Detail
  enrichment should be explicitly bounded by low concurrency and should tolerate
  per-listing failures by preserving the search row plus `detailError`.
- Faster detail paths worth testing before a full detail run:
  - Reproduce the exact browser GraphQL request for `getPDPProperty`,
    `getPDPAvailabilities`, and `getSRPPropertyBrokers`. Prior simple POST
    attempts returned 400, so this requires browser-context capture rather than
    guessing request bodies.
  - Check whether a stable Next data JSON route exists for JLL detail pages.
    If available, it would be cheaper than rendered detail-page scraping.
  - Reuse one detail scrape per normalized listing URL across sale, lease, and
    property-type duplicates.

## Patch Recommendation

Next source-specific patch should add optional main-JLL detail enrichment:

1. Keep the current property-type search pagination and URL de-dupe.
2. Add `JLL_DETAIL_ENRICHMENT=1` or an equivalent source-local switch, plus
   `JLL_DETAIL_CONCURRENCY` capped at 2 or 3.
3. Scrape detail pages with `rawHtml`, parse `#__NEXT_DATA__`, and map
   `pageProps.property` plus `pageProps.brokers`.
4. Populate stable JLL property ID, coordinates, descriptions, brochures,
   image URLs, `contactsDetailed`, broker refs, profile URL or profile slug,
   avatar URL, email, phone, and LinkedIn URL.
5. Store brochure and image URLs only. Do not download binaries.
6. Keep VCard empty unless a public VCard URL is proven.
7. Verify with a targeted probe containing at least one non-office sale and one
   non-office lease row, then dry-run ingest.

Recommended immediate status: main `jll` is no longer office-only, but it
should remain "needs deep audit" until public detail-page enrichment is patched
and proven on a bounded sample.
