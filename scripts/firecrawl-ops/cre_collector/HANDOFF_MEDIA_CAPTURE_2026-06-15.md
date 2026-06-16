# Handoff: capture all videos, links, documents, and stranded page data (2026-06-15)

Status: BUILT and verified in code on branch `feat/cre-brokerage-collectors-2026-06-12`.
Nothing applied to prod, nothing committed. Every live step is gated (Section 7).

## 1. Why

The collector was dropping almost everything of value that is not one of ~25
mapped columns. Concretely, before this work (live board 87,328 active rows):

- Only 146 listings carried any video URL, all incidental JLL passthrough inside
  `raw_data`. No video was ever extracted on purpose; there was no place to store one.
- The `cre_listings.markdown` column was 100% empty (0 rows).
- `noi` and `occupancy_rate` were 0% populated; `cap_rate` 2.5%; `year_built` 13%.
- Documents captured only `brochures`; every other link (offering memorandum,
  flyer, financials, rent roll, data room, external listing site, virtual tour,
  matterport, all images beyond a truncated subset) was discarded at parse time.

Load-bearing finding from a live probe of a real Lee Associates listing: the
listing detail and its Vimeo video live inside a cross-origin Buildout iframe
that Firecrawl does not descend into. Scraping the parent page yields 2 links and
no video. Resolving and scraping the Buildout iframe content URL directly yields
the Vimeo share link, the offering-memorandum file, the property photos, both
brokers, and the financials.

## 2. What shipped (all additive)

A single generic harvester plus richer scrape formats, two new child tables, full
ingest wiring, a backfill script, and tests.

### Capture mechanism
- `lib/scrape.ts` `scrapeDoc` now requests `markdown`, `links`, `images`,
  `rawHtml`, and an `attributes` format with selectors for
  `div[component=video][url]`, `iframe[src]`, `a[href]`, `video source[src]`,
  `[data-video-url]`. The local Firecrawl fork returns `images` and `attributes`
  (smoke-verified). Missing formats degrade gracefully to a rawHtml regex fallback.
- `lib/harvest.ts` `harvestDetail(doc, ctx)` is a pure function returning
  `{ media, links, documents, images }`. It never throws, dedups every array, and
  classifies video/tour providers (Vimeo, YouTube, Wistia, Brightcove, Matterport,
  Kuula/360), documents (om/brochure/flyer/floor_plan/financials/rent_roll/other),
  links (external_listing/social/map/other, broker bios dropped because they live
  in `cre_listing_contacts.profile_url`), and images (full gallery, map-tile and
  data:/svg noise dropped, never truncated). `ctx.extra*` promotes a source's
  already-known URLs (the zero-extra-fetch path).
  - Media dedups on canonical identity (provider + video id), so the share URL
    `vimeo.com/<id>/<hash>` and the player iframe `player.vimeo.com/video/<id>`
    fold into ONE item. The Vimeo privacy hash (`?h=`) is preserved in the embed
    URL so unlisted videos still play.
  - Buildout hosted-file links (`buildout.com/sharing/...?file=<id>`, no extension
    or keyword) are classified as documents, not external links.

### Schema (`sql/011_cre_listing_media.sql`, idempotent, registered in `000_run_all.sql`)
- NEW `credeals.cre_listing_media` (`media_type` IN
  video/virtual_tour/matterport/other; `provider`, `url`, `embed_url`, `title`).
- NEW `credeals.cre_listing_links` (`link_type` IN
  external_listing/social/map/broker_bio/document/video/other; `url`, `rel`).
- NEW archive mirrors `cre_listing_media_archive`, `cre_listing_links_archive`
  (009 retirement-snapshot pattern).
- WIDENED `cre_listing_documents.doc_type` CHECK to add `financials`, `rent_roll`.
- `v_cre_listings_full` widened (in `sql/005_cre_views.sql`) with two LATERAL
  `json_agg` blocks exposing media and links.
- Both new tables: FK `ON DELETE CASCADE`, `(listing_id, <type>, url)` unique index
  `NULLS NOT DISTINCT`, RLS enabled with no public policy, table COMMENT.

### Reused, not duplicated
- Full page text reuses the existing empty `cre_listings.markdown` column.
- Stranded structured fields populate EXISTING `cre_listings` columns (`noi`,
  `gross_revenue`, `occupancy_rate`, `units`, `floors`, `parking_*`,
  `available_sf`, `min/max_divisible_sf`, `term_*_months`, `lease_rate_*`,
  `zoning`, `market`, `submarket`, `cap_rate`, `year_built`, ...).
- Documents reuse `cre_listing_documents`. Broker bios reuse
  `cre_listing_contacts.profile_url`. Images reuse `cre_listing_images` (the
  truncation that capped galleries is removed).
- Net new schema is exactly two tables plus their archive mirrors.

### Ingest wiring (`cre_ingest.py`, additive)
- `to_row` builds `media`/`links` arrays, carries `markdown`, honors per-document
  `docType`, and maps the lifted structured fields. `lease_rate_type` is clamped
  to the four CHECK-allowed tokens (junk maps to NULL) so a free-text value cannot
  abort the transaction.
- `merge_rows` folds media/links across sale+lease passes; markdown prefers the
  longer text.
- Staging and `_stage` DDL carry `media jsonb`, `links jsonb`, `markdown text`.
- `build_sql` wholesale-replaces media/links per listing, mirroring images:
  `to_regclass`-guarded (safe to run before `011` is applied), excluding rows
  whose latest source pass had a `detailError` (preserves children on transient
  failures), `ON CONFLICT DO NOTHING`. `markdown` and the numeric structured
  columns use COALESCE-keep so a sparse pass never clobbers good data.

### Per-source wiring (`sources/*.ts`, `lib/enrich.ts`)
- Every detail-fetching source calls `harvestDetail` and attaches media/links
  additively (existing photos/brochures pass through `ctx.extra*`).
- Stranded `raw_data` promoted with no extra fetch: JLL
  `videos/virtualTours/view360URLs` + floor plans, Marcus `gatedDocuments` (OM /
  deal room), Colliers `brochureUrl`/`agreementUrl`, NAI `urlOriginal` + detail
  iframes, Cushman/Transwestern/Colliers-main rawHtml iframes and link mining.
- Structured-field lift where the payload exposes it (e.g. Transwestern
  year_built/units/floors/parking/zoning from the facts block).
- Enumeration-only Buildout sources (`lee-associates`, `svn`): `sources/buildout.ts`
  gains an iframe-content-URL resolver and a detail enricher invoked ONLY via the
  Tier-B `--enrich-input` path, NOT the daily bulk collect.

### Backfill (`backfill_media_from_raw_data.py`, authored, not run)
- Cheap one-time lift of media/docs already stranded in `raw_data`, additive and
  idempotent (`ON CONFLICT DO NOTHING`), `--dry-run` default, `--apply` gated.
- Recoverable at zero scrape cost: ~328 JLL media rows, ~3,124 Marcus documents,
  ~814 Colliers documents.

## 3. Verification

- `npm run typecheck`: clean.
- `npm run test:unit`: 224 pass / 0 fail (new `tests/ts/lib/harvest.test.ts`
  including the Lee Associates Vimeo case, the same-video fold, and the Buildout
  document case).
- `python3 -m pytest tests/`: 435 pass / 0 fail (new `test_media_links_ingest.py`,
  `test_backfill_media.py`).
- Real saved Lee Associates fixture through `harvestDetail`: exactly 1 video
  (share URL, embed URL with privacy hash), 1 document (offering memorandum), 14
  links, 19 images.
- Ingest dry-run on a live artifact: guarded media/links blocks present, markdown
  and structured COALESCE-keep present, no connection string emitted.
- Adversarial review (two reviewers): additive-only, `to_regclass`-guarded,
  detailError-preserved, COALESCE-keep correct, no-narrow transaction_type /
  status activation / mark-missing untouched, monitor enumeration byte-identical,
  no schema duplication. Both blocking findings fixed (lease_rate_type clamp,
  stale enricher-set test).

## 4. Files

New: `lib/harvest.ts`, `tests/ts/lib/harvest.test.ts`, `sql/011_cre_listing_media.sql`,
`backfill_media_from_raw_data.py`, `tests/test_media_links_ingest.py`,
`tests/test_backfill_media.py`.
Edited: `types.ts`, `lib/scrape.ts`, `lib/enrich.ts`, all 14 `sources/*.ts`,
`cre_ingest.py`, `sql/000_run_all.sql`, `sql/005_cre_views.sql`, and the matching
source unit tests.

## 5. Operational note (working-tree execution)

The live launchd monitor/daily/enrich tiers run `collect.ts`/`cre_ingest.py` from
this working tree via `tsx`, so these changes take effect on the next scheduled
run without a deploy step. Effects:
- The next daily ingest will start populating the existing `markdown` and
  structured columns additively (COALESCE-keep; never clobbers). This is the
  requested behavior and is safe.
- Media and links will NOT be written until `sql/011` is applied (the INSERTs are
  `to_regclass`-guarded). Until then those blocks are no-ops.
- The Buildout iframe detail fetch for `lee-associates`/`svn` runs only through
  the Tier-B enrich worker, never the bulk daily collect.
- Detail scrapes now request `images` + `attributes` in addition to the prior
  formats, a small per-page payload increase.

## 6. Backfill sizing (live, active rows)

`raw_data` lift candidates: ~328 JLL videos/tours, ~3,124 Marcus documents, ~814
Colliers documents. Going-forward capture handles all new/changed listings via the
monitor to enrich path automatically.

## 7. Gated live steps (need explicit go-ahead)

1. Apply `sql/011_cre_listing_media.sql` (new tables + widened `doc_type` CHECK +
   archive mirrors) and the `v_cre_listings_full` widening in `sql/005_cre_views.sql`
   to project `fhqycqubkkrdgzswccwd`. Verify a read-only zero-row no-op first.
2. Going-forward media/links capture then begins automatically on the next
   monitor to enrich cycle (no separate deploy).
3. Run the cheap backfill: `python3 backfill_media_from_raw_data.py --dry-run`,
   review counts, then `--apply`.
4. Buildout Tier-B detail go-live for `lee-associates`/`svn` is exercised by the
   existing enrich worker; no extra step beyond `011` being applied.

Unchanged and still separately gated: status activation, `--mark-missing`
soft-delete, the consumer board-gate deploy, and the enrichment-cadence launchd
cutover (`ENRICHMENT_WORKER_DESIGN_2026-06-15.md` Section 9).
