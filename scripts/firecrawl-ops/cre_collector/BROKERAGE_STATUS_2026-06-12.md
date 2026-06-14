# Brokerage Coverage Status - 2026-06-12

This status is for the CRE collector in `scripts/firecrawl-ops/cre_collector/`.
It combines the latest full artifact, the post-validation Cushman upgrade, and
source-specific notes in `cre_scrapers/brokers/*/README.md`.

## Status Definitions

- Complete public feed: full public pagination is implemented and verified for the source feed. Public documents/images are stored as URLs only.
- Needs deep audit: pagination works, but detail-page enrichment or field completeness still needs the Cushman-style verification pass.
- Partial: only the first page, first rendered batch, or a small public subset is collected.
- Blocked: no repeatable public GET, Firecrawl-compatible action path, or clean sustained run exists yet.

## Summary

| Brokerage / site | Current status | Latest verified listing count | What we have | Main remaining work |
|---|---|---:|---|---|
| CBRE | Complete public feed | 19,028 active rows, 4,222 sale + 13,145 lease + 1,661 sale_or_lease | Full internal JSON API pagination, agents, charges, brochures, photos, coordinates, size fields | Optional detail-page audit only if we discover fields missing from the API |
| CBRE Deal Flow | Complete public endpoint for exposed cards, with gated-detail limits | 1,836 active rows, 1,809 sale + 27 lease | Public RCM ListingEngine endpoint, sale and lease filters, pagination, public card/detail enrichment, URL-only docs/images, contacts | RCM reports 2,042 sale total but public card pagination exposed 1,809 sale cards; 21 stale URL-hash duplicate rows soft-deleted; gated agreement/deal-room docs stay in raw metadata only |
| SVN | Complete public Buildout feed, live-ingested and validated | 5,287 active rows, 2,660 sale + 2,192 lease + 435 sale_or_lease | Public Buildout inventory feed with durable page cache/window fill, sale/lease partitioning, broker refs, document URLs, image URLs, and source-scoped reconciliation | One active SVN row is missing state; optional future detail-page enrichment only if a safe public path is proven |
| Cushman & Wakefield | Complete public feed, live-ingested and validated | 11,318 active rows, 2,743 sale + 8,575 lease | Public search API pagination plus detail-page enrichment for facts, document URLs, image URLs, contacts, profile URLs, VCard URLs, JSON-LD | Optional future field audit only if missing fields are discovered |
| JLL | Complete main public property feed, live-ingested and validated | 10,741 active main JLL rows, 1,247 sale + 8,733 lease + 761 sale_or_lease | Rendered public search pagination across property type filters plus detail-page `__NEXT_DATA__` enrichment for documents, image galleries, broker contacts/profile URLs, richer facts, and source-scoped stale same-URL cleanup | 135 latest-batch duplicate source URL groups remain as sale/lease same-page variants; JLL Investor is now complete as a separate folded source (934 active sale rows) |
| Newmark | Complete public Algolia feed with contact enrichment, live-ingested and validated | 4,371 active rows, 1,121 sale + 3,250 lease | Public Algolia listing feed, state/property-type split, no-state DC recovery, raw hit preservation, broker provenance, public People exact-name contacts/profile URLs, image URLs | Listing documents, full galleries, second/third broker joins, and VCards remain unproven |
| Avison Young | Complete public feed, detail-enriched, live-ingested and validated | 2,201 active rows, 636 sale + 1,432 lease + 133 sale_or_lease | Public SharpLaunch active website/team_member feed, client-side transaction partition, contact joins, public PDF URLs, full SharpLaunch image galleries, profile URLs where public, and non-property photo filtering | VCards absent from the public path; broker profile URLs remain sparse |
| Savills | Partial, lease has small defensible public CRE subset live-ingested; sale still not CRE-defensible | 104 active rows, 101 sale + 3 lease | Server-rendered commercial lease `__NEXT_DATA__` for two Chicago retail listings with PDF/image/contact URLs; legacy global sale cards remain separate caveat | Find a public U.S. commercial sale source before claiming sale coverage; product decision needed on whether to retain legacy global/residential sale rows |
| JLL Investor Center | Complete public sitemap detail feed, live-ingested and validated | 934 active U.S. sale rows from 1,857 sitemap detail URLs scanned | Public XML sitemap discovery, public detail-page `__NEXT_DATA__`, U.S. filtering, URL-only docs/images/contacts | All rows lack coordinates (Investor path exposes none); lease not applicable; 50 stale early-probe rows soft-deleted after user approval |
| Marcus & Millichap | Complete public sale feed, live-ingested and validated; public lease blocked | 3,124 active sale rows | Public `mapproperties` ActivityIds, `mappropertydetail` tiles, direct public detail HTML, image URLs, visible contacts/profile URLs, and gated deal-room URLs retained only in raw metadata | Public lease remains unproven; auctions need a product decision before inclusion |
| NAI Global | Complete public active feed, status-filtered | 241 active rows, 183 sale + 58 lease | Public Infabode feed plus `publicPost` detail enrichment, stable `infabode:<id>` IDs, source/original URLs, image URLs, one document URL, and raw status proof | Do not ingest historical/`UNKNOWN` Infabode rows as active inventory; optional future archive table only |
| Lee & Associates | Complete public Buildout feed, live-ingested and validated | 9,223 active rows, 2,611 sale + 5,691 lease + 921 sale_or_lease | Public Buildout inventory feed with durable page cache/window fill, broker refs, document URLs, image URLs, and source-scoped reconciliation | Optional future detail-page enrichment only if a safe public path is proven |
| Colliers (SalesTracker) | Complete public RCM investment-sale subset, live-ingested | 1,172 unique active Supabase rows | Public SalesTracker RCM GET list/map endpoints plus anonymous SLP detail enrichment | Retained alongside the new main-site source; SalesTracker filtered total reports 1,653 but public card pagination exposed 1,300 unique cards |
| Colliers main (`colliers-main`) | Unblocked via public sitemap; full run in progress 2026-06-13 | 15,896 sitemap detail URLs; bounded 2,000-URL batch live (943 rows) | Public XML sitemap (`/sitemap` -> `en/sitemap?type=properties`) through local Firecrawl plus per-listing detail-render `RealEstateListing` JSON-LD + markdown parse; folded into `colliers` with `main:` prefix; no Coveo POST/auth/gated path | Complete the full ~15,896-URL run, ingest, and validate before claiming complete main-site coverage. See `HANDOFF_COLLIERS_MAIN_2026-06-13.md` |
| Transwestern | Complete public feed, live-ingested and validated | 2,021 active rows, 389 sale + 1,502 lease + 130 sale_or_lease | Public properties GET feed plus detail-page enrichment for property docs, images, contacts, profile URLs, and VCards; footer descriptions suppressed | Optional future accuracy work: availability parser hardening and detail-cache speedup |

## Completed Or Closest To Complete

Per-source completion state is in the Summary table above (the "Current status"
column); it is not re-narrated here.

## Not Complete Yet

Main Colliers (`colliers-main`) is no longer blocked: a public XML sitemap path
(`/sitemap` -> `en/sitemap?type=properties`, 15,896 detail URLs) fetched through
local Firecrawl plus detail-render JSON-LD parse replaced the blocked Coveo POST
route. A bounded 2,000-URL batch (943 rows) is live; the full run is in progress
as of 2026-06-13.

Savills remains weak for EQUIRE sale coverage because no safe repeatable public
U.S. commercial sale path was found in the latest recheck, but the commercial
lease path now exposes three defensible Chicago retail lease listings and those rows are
live-ingested additively.

NAI Global is complete only for the active public Infabode inventory whose
`publicPost.listingStatus` contains `FOR_SALE_ON_MARKET`. The same public feed
also exposes historical or ambiguous rows back to 2021 with `UNKNOWN`, `SOLD`,
`UNDER_OFFER`, or null statuses; those are deliberately excluded from the active
`credeals` surface.

## Cushman Proof Snapshot

Command:

```bash
CUSHMAN_QUERY='1800 Central' npx tsx collect.ts --source=cushman-wakefield \
  --transaction=sale --max-items=5 --page-cap=5 --concurrency=2 \
  --out=/tmp/cushman_1800_probe.json
```

Result:

- 1 listing.
- 2 PDF URLs.
- 15 image URLs.
- 1 contact with phone, profile URL, avatar URL, and VCard URL.
- No PDF or image binaries stored.
- Dry-run ingest staged 1 listing.

## Next Broker Order

1. ~~JLL main detail enrichment~~ (complete: full detail enrichment live-ingested and validated).
2. ~~Avison Young full detail enrichment~~ (complete: full detail-enriched run live-ingested additively and validated; VCards absent from the public path).
3. Savills, because sale remains not CRE-defensible and the lease subset is only 3 defensible rows.
4. ~~JLL Investor full run and ingest proof~~ (complete: 1,857 sitemap detail URLs scanned, 934 active U.S. sale rows live-ingested and reconciled, 2026-06-12 22:47 UTC).
5. ~~Main Colliers Coveo sale/lease coverage only after a safe non-POST-blocked path exists.~~ (unblocked 2026-06-13 via the public XML sitemap + detail-render path; `colliers-main` source built, bounded batch live, full run in progress.)
6. Marcus auctions only after EQUIRE decides whether public auction inventory
   belongs in the listing surface.
