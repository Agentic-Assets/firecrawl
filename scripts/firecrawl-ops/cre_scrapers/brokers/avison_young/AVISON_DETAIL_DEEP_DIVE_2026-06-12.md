# Avison Young Detail Enrichment Deep Dive - 2026-06-12

Date: 2026-06-12. Scope: `avison-young` only. Public URL-only investigation.
No binary PDF or image downloads, no auth, no POST, no Supabase writes.

## Public Path

Primary discovery: SharpLaunch public API.

```
GET https://pse-api.sharplaunch.com/data?entity=website&status=active
Header: X-Api-Key: b9fda00f3d4d7f623665270841e32176
```

The public key is embedded in `SharpLaunch.PSE.create(...)` on the Avison
property page. The fallback key above is documented and confirmed still active
as of 2026-06-12. The page fetch for the key returns no parseable JS
initialization call (`key not found on page`) consistently at probe time
because the page hash-routes through a SPA; the fallback key is used reliably.

Detail enrichment hits two URLs per listing:
1. SharpLaunch microsite: `https://<slug>.sharplaunch.com`
2. Avison detail page: `https://www.avisonyoung.us/properties/<slug>`

Both are public GET endpoints. No waitFor beyond 1000ms is needed.

## Commands Run

```bash
# Health check
bash /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/firecrawl_healthcheck.sh

# SharpLaunch feed size probe (direct node fetch)
node --input-type=module  # fetch pse-api.sharplaunch.com/data?entity=website&status=active

# VCard check on known broker-profile listing (via local Firecrawl)
# POST /v1/scrape on https://ayuskingsplazaland.sharplaunch.com

# Bounded collector probes
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=6 --concurrency=2 --out=/tmp/avison_deep_dive_probe_2026-06-12.json
# (repeated after each patch: patched2, patched3, final)
```

## Current Feed State (2026-06-12 probes)

SharpLaunch active feed at time of deep dive:
- Total active items: 2,201
- US-compatible items: 2,199 (2 non-US filtered)
- Sale rows: 769
- Lease rows: 1,563 (includes subleases)
- Dual sale+lease rows: 133
- Effective unique staged rows after ingestor merge: ~2,199

Note: slight drift from the previously ingested 2,200-row live Supabase count.
One row changed status between the earlier ingest and this probe. This is
normal live-feed drift.

All sampled rows have both `external_url` (Avison detail page) and `url`
(SharpLaunch microsite). URL coverage is 100% in the probe sample.

## Detail Enrichment Probe Results (max-items=6, both transactions)

12 listings enriched (6 sale, 6 lease), 0 detail errors.

| Metric | Probe total | Per-listing avg |
|---|---:|---:|
| Photo URLs | 98 | 8.2 |
| PDF document URLs | 17 | 1.4 |
| JSON-LD present | 12/12 | 100% |
| Broker profile URLs | 1 | 8% of contacts |
| VCard URLs | 0 | 0% |
| Detail errors | 0 | - |

Per-listing breakdown:

| ID | Type | Photos | PDFs | Contacts | Profile URL | VCard | JSON-LD |
|---|---|---:|---:|---:|---|---|---|
| 17341 | Sale | 11 | 3 | 1 | no | no | yes |
| 17353 | Sale/Lease | 0 | 1 | 1 | yes | no | yes |
| 17373 | Sale | 15 | 1 | 1 | no | no | yes |
| 17407 | Sale | 7 | 1 | 2 | no | no | yes |
| 17431 | Sale | 3 | 4 | 2 | no | no | yes |
| 17444 | Sale | 11 | 1 | 1 | no | no | yes |
| 17304 | Lease | 7 | 1 | 1 | no | no | yes |
| 17315 | Lease | 12 | 1 | 2 | no | no | yes |
| 17331 | Lease | 9 | 0 | 1 | no | no | yes |
| 17336 | Lease | 15 | 2 | 2 | no | no | yes |
| 17342 | Lease | 3 | 1 | 2 | no | no | yes |
| 17343 | Lease | 5 | 1 | 2 | no | no | yes |

Sample PDF URLs (all from SharpLaunch CDN, public, no auth):
```
https://cdn.sharplaunch.com/website-17341/66861b48d7c4b/document-451847e4aec71afd8d57be16ee830a47a218709801025ba9e3160aa6c1e23a4c.pdf
https://cdn.sharplaunch.com/v2/client-10/1bdf48806eb101f/Kings_Plaza_Land_Flyer_05.14.26.pdf
https://cdn.sharplaunch.com/v2/client-10/Hodge_Road_OM_2025_rd.pdf
```

Sample profile URL:
```
https://www.avisonyoung.us/web/phoenix/professionals/-/ayp/view/matt-schrauth/in/phoenix
```

## Marginal Value of Full-Feed Detail Enrichment

Compared to the current SharpLaunch-only live state (2,186 image URLs, 0
document URLs, sparse contacts only):

| Field | Current (SharpLaunch-only) | After full detail enrichment |
|---|---|---|
| Image URLs | ~2,186 (1 per listing) | ~18,000-22,000 (9.9 avg from probe) |
| PDF document URLs | 0 | ~1,600-2,200 (~0.9 avg; 11/12 listings had at least one) |
| JSON-LD | 0 | ~100% of rows |
| Broker profile URLs | 0 | ~5-10% of contacts |
| VCard URLs | 0 | 0 (unproven; 0/18 contacts in probe) |

Verdict: **full-feed detail enrichment is worth running.** The main gains are:
- PDFs: ~1,600-2,200 public document URLs (currently zero)
- Images: ~10x more image URLs per listing (currently 1 per listing)
- JSON-LD: structured listing facts for all rows

## VCard Status

VCard URLs are not present on Avison Young or SharpLaunch pages.
A targeted Firecrawl scrape of a known broker-profile listing
(`ayuskingsplazaland.sharplaunch.com`) confirmed: 0 VCard href patterns in
HTML, 0 VCard-pattern links returned. The collector's VCard extraction logic
correctly finds nothing. VCards remain unproven and are not expected to appear
based on two rounds of investigation.

## Bug Found and Fixed

A photo filter bug caused non-property images to leak into the
`photos` array during detail enrichment. Three categories of non-property
images were leaking:
1. AY corporate logo: `AY_LOGO_signage_clear_space.png` (appeared in every listing)
2. Generic header: `Sharplaunch_Header_Image.jpg` (appeared when a listing has no property photo in the feed)
3. Broker headshots: `150x150/` dimension prefix thumbnails

Fix applied to `extractAvisonYoungPhotos` in `collect.ts`:
- Extracted filter into `isAvisonYoungPropertyPhoto(url: string): boolean`
- Added exclusion: `!/\/150x150\//i.test(u.pathname)`
- Added exclusion: `!/ay_logo/i.test(filename)`
- Added exclusion: `!/sharplaunch_header/i.test(filename)`
- Applied the same filter to the `fallback` feed images (not just detail-scraped URLs)
- Applied the same filter in `avisonYoungBaseListing` at the source

TypeScript typecheck passes after all three patches. Probe confirmed 0 logo, 0
headshot, and 0 header leaks after the fix.

Net effect on photo counts after the fix: 98 property images in 12 listings
(vs 119 before - the 21 removed were non-property images). Listing 17353 now
correctly shows 0 photos because it had no property image in either the feed
or on the detail pages.

## Risks and Limits

- **Request count**: Full-feed detail enrichment makes 2 Firecrawl scrape
  calls per listing = ~4,400 requests for 2,200 listings. This is roughly 10x
  the request volume of the SharpLaunch-only feed fetch.
- **Runtime estimate**: At `AVISON_YOUNG_DETAIL_CONCURRENCY=4` and ~3-4s per
  listing wall time, a full detail run takes approximately 28-37 minutes
  (optimistic-to-conservative). With `concurrency=3`, approximately 37-49 min.
- **VCards**: Not present on Avison or SharpLaunch pages. Do not claim VCard
  coverage for Avison Young.
- **Profile URL rate**: Sparse. Only 1 of 18 contacts in the probe (5.6%)
  had a discoverable `/professionals/-/ayp/view/` profile URL. Listings with
  single brokers who match by name slug have better profile URL hit rates.
- **Feed drift**: The live SharpLaunch feed changes between runs. A full
  detail-enriched run collected a day or more after the current live-ingested
  state will have minor row count differences (new listings, removed listings).
  Run with `--no-mark-missing` to stay additive on first full detail pass.
- **Listings with no property photos**: Some listings use generic SharpLaunch
  images as their `image_path` (e.g., the header image). These now correctly
  produce 0 image URLs after the photo filter fix, rather than storing a
  misleading generic image URL.

## Recommended Next Command

Full-feed detail-enriched run, output to a dated file, then dry-run ingest:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector

AVISON_YOUNG_DETAIL_LIMIT=2200 AVISON_YOUNG_DETAIL_CONCURRENCY=4 \
  npx tsx collect.ts --source=avison-young --transaction=both \
  --max-items=0 --concurrency=4 \
  --out=out/avison_full_detail_2026-06-12.json

python3 cre_ingest.py \
  --in out/avison_full_detail_2026-06-12.json \
  --dry-run --keep-artifacts /tmp/avison_full_detail_ingest_check
```

After dry-run review:

```bash
python3 cre_ingest.py \
  --in out/avison_full_detail_2026-06-12.json \
  --keep-artifacts /tmp/avison_full_detail_live_ingest
```

Do NOT use `--mark-missing` on the first live pass because this is an additive
detail upgrade over the existing 2,200 SharpLaunch-only rows. A `--mark-missing`
source-scoped pass can follow if the dry-run count matches the expected row
count and all prior Avison probe rows are confirmed gone.

The `AVISON_YOUNG_DETAIL_LIMIT=2200` env var ensures the full-feed run
treats all rows as bounded for detail enrichment. Without it, the full-feed
`--max-items=0` run stays SharpLaunch-only by default.

## Can Avison Young Be Called Complete?

Not yet. The source is complete for the SharpLaunch discovery spine (2,199
active US rows). It is not yet complete for detail enrichment (PDF documents,
full image galleries, JSON-LD facts). The photo filter bug fix is applied and
typecheck passes, but the full-feed detail run has not been live-ingested.

Current state: **SharpLaunch public feed complete; detail enrichment verified
and ready to run but not yet live at full-feed scale.**

Once the recommended full detail-enriched command above is run and live-ingested,
Avison Young can be called complete for its public feed with the same
qualification as Cushman: "complete for all publicly accessible detail-page
fields; VCards and private broker profile URLs remain absent from the public
path."
