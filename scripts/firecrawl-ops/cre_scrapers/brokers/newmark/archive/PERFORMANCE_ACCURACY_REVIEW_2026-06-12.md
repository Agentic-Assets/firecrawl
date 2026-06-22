Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Newmark Performance And Accuracy Review - 2026-06-12

Scope: source key `newmark` in `scripts/firecrawl-ops/cre_collector/collect.ts`.
No live ingest was run in this review. No PDFs or images were downloaded.

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
jq '.listings[] | select(.id=="701-8th-st-nw-washington-lease" or .id=="800-maine-avenue-southwest-washington-lease" or .id=="1800-massachusetts-avenue-northwest-washington-lease") | {id,name,city,state,postalCode,url}' out/newmark_full_2026-06-12_no_state_recovery.json
```

Reference artifacts inspected:

- `out/newmark_full_2026-06-12_no_state_recovery.json`
- `/tmp/newmark_detail_enrichment_summary_2026-06-12.json`
- `/tmp/newmark_people_algolia_probe_2026-06-12.json`

## Current Collector Shape

Newmark's public feed path is strong. The collector reads public Algolia
configuration from `https://www.nmrk.com/properties`, queries the public index
for `sectionGroup:Properties`, `saleOrLease:Sale|Lease`, `country_code:US`,
and `siteHandle:enUs`, then splits by state and property type to stay under
Algolia's retrievable-hit cap.

The no-state recovery patch is already present. The saved full artifact
contains 4,371 rows: 1,121 sale and 3,250 lease.

The 2026-06-12 bounded review probe wrote 18 Newmark rows, 9 sale and 9 lease.
Dry-run ingest staged all 18 Newmark rows and skipped 0 for missing URL.

## Accuracy Findings

- In the bounded probe, Newmark had 18 of 18 with state, 18 of 18 with
  coordinates, 17 of 18 with at least one Algolia thumbnail image, 0 of 18 with
  contacts, 0 of 18 with broker refs, 0 of 18 with document URLs, and 0 of 18
  with VCard URLs.
- The full no-state recovery artifact includes the three recovered Washington,
  DC lease rows, but they still have `state: null`:
  - `701-8th-st-nw-washington-lease`, ZIP 20001.
  - `800-maine-avenue-southwest-washington-lease`, ZIP 20024.
  - `1800-massachusetts-avenue-northwest-washington-lease`, ZIP 20036.
- Newmark detail pages are not a good enrichment source under the current
  local Firecrawl path. Sample pages rendered mostly static shell content,
  site-wide legal PDFs, placeholder or favicon images, and company/meta images.
  Those should not become listing documents or listing photos.
- The static detail pages did not expose listing-specific brochure URLs, full
  galleries, contacts, profiles, or VCard URLs.
- The public Algolia hit shape contains broker-related fields such as
  `broker_name`, `broker_id`, `broker_ids`, `second_broker_id`, and
  `third_broker_id`, but the collector currently drops them.
- Public People records can be found through the same Algolia index with
  `sectionGroup:People` and an exact name query. Prior bounded probes matched
  names such as Andrew Visnick, Jeff Sanita, Mark Repstad, and Kevin Hansen and
  returned profile URLs, email addresses, phone numbers, and offices.
- Direct People facet filters by numeric Buildout broker IDs returned 0 in the
  samples, so numeric IDs are useful raw provenance but not a reliable join key
  yet.

## Performance Findings

- Newmark should not use detail-page scraping for bulk enrichment right now.
  It is slower and returns noisy shell assets rather than listing-specific
  documents or contacts.
- The best faster enrichment path is cached Algolia People lookup by exact
  `broker_name`. That avoids one rendered detail scrape per listing and should
  require at most one People query per unique broker name in the run.
- Keep the lookup bounded and cached. A source-local concurrency of 3 or less
  is enough, because this is enrichment rather than discovery.
- Preserve `broker_id`, `broker_ids`, `second_broker_id`, and
  `third_broker_id` in raw data for a future Buildout or People mapping pass.

## Patch Recommendation

Next source-specific patch should be small:

1. Preserve raw Newmark hit fields used for future joins, especially
   `broker_name`, `broker_id`, `broker_ids`, `second_broker_id`, and
   `third_broker_id`.
2. Add a cached People Algolia lookup keyed by normalized `broker_name`.
3. Accept only exact case-insensitive People title matches.
4. Populate `contactsDetailed` and broker refs with name, email, phone,
   company, profile URL, offices, and title when present.
5. Infer `DC` only for the known no-state shape where city is Washington and
   ZIP starts with `200`.
6. Continue keeping documents empty unless a listing-specific public document
   URL is proven. Do not promote Newmark site-wide legal PDFs.
7. Continue using Algolia thumbnails only for images until a reliable
   listing-gallery source is found.
8. Keep VCard empty unless a public VCard or contact-card URL is proven.

Recommended immediate status: Newmark is feed-complete for public Algolia row
coverage after no-state recovery, but not deep-contact complete. The next
full-run order should patch People enrichment and DC state inference, then run
a Newmark-only full dry run before any live ingest decision.
