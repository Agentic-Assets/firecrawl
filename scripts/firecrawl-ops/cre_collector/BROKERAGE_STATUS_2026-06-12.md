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
| Avison Young | Public feed complete; bounded detail enrichment verified | 2,200 staged unique rows, 636 sale + 1,431 lease + 133 sale_or_lease | Public SharpLaunch active website/team_member feed, client-side transaction partition, contact joins, CDN image/avatar URLs, and bounded detail enrichment for selected rows | Full-feed detail enrichment is not run by default; VCards remain unproven in sampled pages |
| Savills | Partial, lease has small defensible public CRE subset live-ingested; sale still not CRE-defensible | 104 active rows, 101 sale + 3 lease | Server-rendered commercial lease `__NEXT_DATA__` for two Chicago retail listings with PDF/image/contact URLs; legacy global sale cards remain separate caveat | Find a public U.S. commercial sale source before claiming sale coverage; product decision needed on whether to retain legacy global/residential sale rows |
| JLL Investor Center | Complete public sitemap detail feed, live-ingested and validated | 934 active U.S. sale rows from 1,857 sitemap detail URLs scanned | Public XML sitemap discovery, public detail-page `__NEXT_DATA__`, U.S. filtering, URL-only docs/images/contacts | All rows lack coordinates (Investor path exposes none); lease not applicable; 50 stale early-probe rows soft-deleted after user approval |
| Marcus & Millichap | Complete public sale feed, live-ingested and validated; public lease blocked | 3,124 active sale rows | Public `mapproperties` ActivityIds, `mappropertydetail` tiles, direct public detail HTML, image URLs, visible contacts/profile URLs, and gated deal-room URLs retained only in raw metadata | Public lease remains unproven; auctions need a product decision before inclusion |
| NAI Global | Complete public active feed, status-filtered | 241 active rows, 183 sale + 58 lease | Public Infabode feed plus `publicPost` detail enrichment, stable `infabode:<id>` IDs, source/original URLs, image URLs, one document URL, and raw status proof | Do not ingest historical/`UNKNOWN` Infabode rows as active inventory; optional future archive table only |
| Lee & Associates | Complete public Buildout feed, live-ingested and validated | 9,223 active rows, 2,611 sale + 5,691 lease + 921 sale_or_lease | Public Buildout inventory feed with durable page cache/window fill, broker refs, document URLs, image URLs, and source-scoped reconciliation | Optional future detail-page enrichment only if a safe public path is proven |
| Colliers | Partial, live-ingested SalesTracker subset | 1,300 SalesTracker cards collected, 1,172 unique active Supabase rows | Public SalesTracker RCM GET list/map endpoints plus anonymous SLP detail enrichment for investment-sale cards | Main `www.colliers.com/en/properties` Coveo sale/lease path remains blocked; SalesTracker filtered total reports 1,653 but public card pagination exposed 1,300 unique cards |
| Transwestern | Complete public feed, live-ingested and validated | 2,021 active rows, 389 sale + 1,502 lease + 130 sale_or_lease | Public properties GET feed plus detail-page enrichment for property docs, images, contacts, profile URLs, and VCards; footer descriptions suppressed | Optional future accuracy work: availability parser hardening and detail-cache speedup |

## Completed Or Closest To Complete

CBRE is complete for its public feed. CBRE Deal Flow is complete for the public
RCM cards exposed through the public endpoint, has been live-ingested, and has
had 21 stale URL-hash duplicate rows soft-deleted. Cushman & Wakefield is complete for its public API feed and is now
live-ingested with source-scoped reconciliation. SVN is complete for its public
Buildout feed after durable page-cache assembly, source-scoped reconciliation,
and Supabase validation.

## Not Complete Yet

JLL main property search is now complete for its public rendered feed after full
detail enrichment, live ingest, validation, and stale same-URL cleanup. Avison
Young now has bounded detail enrichment proof, but not a full-feed
detail-enriched live run. Savills is explicitly partial, and main
Colliers Coveo sale/lease coverage remains blocked. JLL Investor Center
completed a full sitemap detail ingest on 2026-06-12 22:47 UTC with 934 active
U.S. sale rows retained from 1,857 scanned sitemap URLs; source-scoped
soft-delete cleanup was applied with user approval. Colliers SalesTracker is
complete only for the public RCM investment-sale subset. Marcus & Millichap is complete for the
defensible public sale feed after full ActivityId expansion, detail enrichment,
source-scoped ingest, and Supabase validation; public lease remains blocked.
Savills remains weak for EQUIRE sale coverage because no safe repeatable public
U.S. commercial sale path was found in the latest recheck, but the commercial
lease path now exposes two defensible Chicago retail listings and those rows
are live-ingested additively. Lee and SVN are now complete for their public Buildout feeds after durable page-cache assembly, source-scoped
reconciliation, and Supabase validation. Transwestern is now complete for its
public GET feed after full collection, cleaned artifact ingest, source-scoped
reconciliation, and Supabase validation.

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
2. Avison Young, only if we decide to schedule bounded or full-feed detail enrichment beyond the verified sample.
3. Savills, because sale remains not CRE-defensible and the lease subset is only 2 defensible rows.
4. ~~JLL Investor full run and ingest proof~~ (complete: 1,857 sitemap detail URLs scanned, 934 active U.S. sale rows live-ingested and reconciled, 2026-06-12 22:47 UTC).
5. Main Colliers Coveo sale/lease coverage only after a safe non-POST-blocked path exists.
6. Marcus auctions only after EQUIRE decides whether public auction inventory
   belongs in the listing surface.
