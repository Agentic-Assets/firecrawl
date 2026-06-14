# Lee & Associates Scraper Notes

> **COMPLETE (2026-06-12).** Lee & Associates is fully collected and live-ingested
> (9,223 active rows) through the durable Buildout page cache. Live counts and
> status: `../../../../cre_collector/START_HERE.md`.

**Production path:** `../../../../cre_collector/sources/buildout.ts` (source key
`lee-associates` in `collect.ts`). Shared Buildout adapter; Lee-specific paging
controls documented in `../../../../cre_collector/sources/CLAUDE.md`.

## Public endpoint

Lee exposes US sale and lease inventory through a Buildout plugin feed (no
server-side sale/lease filter; client-side partition). Direct JSON GET with
browser headers works; Firecrawl fallback handles transient interstitials.

```text
GET https://leeassociates.com/plugins/inventory/?pluginKey=<key>&page=<n>&limit=30
```

Inventory metadata: `total` ~9,975 rows, 333 pages (`limit=30`). Dual-mode
properties appear as separate `-sale`/`-lease` URLs; ingest merges to
`sale_or_lease`.

## Collector controls (Lee-only defaults)

- Durable page cache: `out/cache/buildout/lee-associates/page-NNNN.json`
- `BUILDOUT_CACHE_ONLY=1`, `BUILDOUT_PAGE_START`/`END`, `BUILDOUT_ASSEMBLE_FROM_CACHE=1`
- `requireCompletePages: true` (any failed page aborts the source)
- Assembly requires contiguous pages 0 through 332 before artifact write

## Completion proof (2026-06-12)

- Cached 333 of 333 pages; assembled artifact `out/lee_full_cache_2026-06-12_assembled.json`
- Dry-run staged 9,223 unique rows (752-row reduction from sale+lease merge)
- Live ingest with source-scoped `--mark-missing`; 0 quality-flag failures
- Transaction split: 2,611 sale, 5,691 lease, 921 sale_or_lease

**Remaining limit:** feed-level Buildout JSON only; no separate Lee detail pages,
VCards, or richer galleries unless a safe public path appears.

## Historical probe notes (pre-completion)

Chronological buildout probes (throttling, direct-first fetch, cache-fill windows)
are archived under `archive/`. Do not treat "blocked" or "not production-complete"
wording in those files as current status.

- `archive/LEE_BUILDOUT_THROTTLING_RESUMABILITY_2026-06-12.md`
- `archive/LEE_BUILDOUT_SUBAGENT_FINDINGS_2026-06-12.md`
