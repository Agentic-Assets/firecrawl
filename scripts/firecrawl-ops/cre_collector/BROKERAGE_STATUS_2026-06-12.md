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
| CBRE | Complete public feed | 20,684 total, 5,879 sale + 14,805 lease | Full internal JSON API pagination, agents, charges, brochures, photos, coordinates, size fields | Optional detail-page audit only if we discover fields missing from the API |
| CBRE Deal Flow | Complete public endpoint for exposed cards, with gated-detail limits | 1,836 full-run rows, 1,809 sale + 27 lease | Public RCM ListingEngine endpoint, sale and lease filters, pagination, public card/detail enrichment, URL-only docs/images, contacts | RCM reports 2,042 sale total but public card pagination exposed 1,809 sale cards; gated agreement/deal-room docs stay in raw metadata only |
| SVN | Partial live refresh, mapping complete from prior full artifact | 5,521 total, 2,988 sale + 2,533 lease in latest full artifact | Buildout inventory feed mapping, sale/lease partitioning, broker refs, PDFs, images | Fresh 2026-06-12 probes failed closed on Buildout 403 HTML before writing JSON; wait for throttling-safe clean run before marking refreshed complete |
| Cushman & Wakefield | Complete in code, pending full run and Supabase ingest | 11,318 live total, 2,743 sale + 8,575 lease | Public search API pagination plus detail enrichment for facts, document URLs, image URLs, contacts, profile URLs, VCard URLs, JSON-LD | Run full Cushman collection and live ingest after reviewing runtime cost |
| JLL | Needs deep audit | 4,678 total, 333 sale + 4,345 lease | Search-page pagination and listing URLs | Add/verify detail-page enrichment for documents, image galleries, broker contact links, and richer facts |
| Newmark | Needs deep audit | 4,368 collected, 1,121 sale + 3,247 lease of 3,250 reported lease | Algolia credentials scraped from page, state and property-type splits for the 1,000-hit cap | Close the 3-row lease gap if still present and audit detail fields, brokers, documents, and images |
| Avison Young | Public feed complete, needs detail enrichment | 2,200 staged unique rows, 636 sale + 1,431 lease + 133 sale_or_lease | Public SharpLaunch active website/team_member feed, client-side transaction partition, contact joins, CDN image/avatar URLs | Optional detail-page enrichment for public PDFs, richer galleries, JSON-LD, VCard/profile URLs |
| Savills | Partial | 100 sale rows of 105 source cards, 0 US lease | Server-rendered sale pages, foreign fallback cards filtered | Resolve 5 filtered or missing sale cards and confirm lease inventory source |
| JLL Investor Center | Partial | 50 sale rows | Rendered public grid | Determine whether more pages exist and enrich details where public |
| Marcus & Millichap | Partial | 12 sale rows | Rendered first grid under stealth | Find public pagination/API and detail enrichment; lease is not publicly listed |
| NAI Global | Partial | 30 rows, 15 sale + 15 lease | First Infabode widget batch, synthesized `card:` ids | Find stable per-card links and widget pagination or API |
| Lee & Associates | Blocked | 0 uploaded in latest full run | Buildout feed path known | Sustained full run failed around pages 286-297; needs throttling-safe or resumable paging |
| Colliers | Blocked | 0 | POST-only Coveo path identified | Needs repeatable public GET, request-body support, safe action path, or authorized integration |
| Transwestern | Blocked | 0 | POST-only map app path identified | Needs repeatable public GET, request-body support, safe action path, or authorized integration |

## Completed Or Closest To Complete

CBRE is complete for its public feed. CBRE Deal Flow is complete for the public
RCM cards exposed through the public endpoint and has been live-ingested
additively. Cushman & Wakefield is complete in code and targeted verification,
but not yet reflected in Supabase because the full upgraded run has not been
ingested. SVN remains mapping-complete from the latest full artifact, but fresh
live refresh verification is partial because Buildout returned 403 HTML during
2026-06-12 probes.

## Not Complete Yet

JLL, Newmark, and Avison Young need the Cushman-style deep audit: prove detail
page enrichment, document URLs, image URLs, contact URLs, and source totals.
Savills, JLL Investor Center, Marcus & Millichap, NAI Global, and SVN live
refresh are explicitly partial. Lee and Colliers are blocked. Transwestern now
has a proven public GET feed and targeted probe, but still needs a full run and
live ingest validation before being marked complete.

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

1. JLL, because it already collects all reported rows and likely only needs detail enrichment.
2. Newmark, because the Algolia feed is strong but the 3-row lease gap and contact/document completeness need proof.
3. Avison Young, because the current 11/11 per transaction might be complete or might be a rendered-sidebar illusion.
4. Savills, because it has a small sale gap and lease ambiguity.
5. NAI Global, Marcus & Millichap, JLL Investor, and CBRE Deal Flow, because each has known first-batch or gated limitations.
6. Lee, after adding a resumable or much slower Buildout paging mode.
7. Colliers and Transwestern only after a safe non-POST-blocked path exists.
