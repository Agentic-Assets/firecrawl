# CRE listing refresh progress log

## 2026-07-18 preflight

- Confirmed current checkout is clean `main` at
  `1bae23bcc9234d3a9c1731221b5290dc7a604484`, which contains merged PR #22.
- Confirmed the live canonical inventory is
  `credeals.cre_listings` and related objects in Supabase project
  `fhqycqubkkrdgzswccwd`.
- Confirmed the supported writer is `scripts/firecrawl-ops/cre_collector`.
  Legacy `cre_scrapers` is not a production update path.
- Current runtime preflight found the local Docker/OrbStack daemon and local
  Firecrawl API unavailable. Existing launchd monitor, enrich, daily, and
  weekly labels are loaded. Monitor last passed, but enrich, daily, and weekly
  have failed markers. The retired daily tier must not be allowed to resume
  before the supported refresh path is repaired and verified.

## 2026-07-18 runtime and enrichment repair

- Recovered the local Docker/OrbStack runtime and passed the Firecrawl health
  check, including an API scrape smoke test.
- Added direct source enrichers for Marcus & Millichap, Avison Young, SRS, and
  Kidder Mathews. These sources no longer fall through to generic JSON-LD
  scraping.
- The generic path now produces no completion record when a page lacks
  JSON-LD, preventing a queued source record from being replaced by a URL-only
  payload.
- Verified with `npm run typecheck`, 479 TypeScript unit tests, 64 Python
  queue/enrichment tests, and a live four-source probe. Each probe listing
  returned through its source-specific path.

## 2026-07-18 additive refresh and watcher run

- Continued implementation on branch `fix/cre-enrich-source-paths`; no change
  has been pushed to `main`.
- Ran additive collection and ingestion for CBRE, CBRE Dealflow, JLL, JLL
  Investor, Colliers, Cushman & Wakefield, Newmark, Avison Young, Kidder
  Mathews, Marcus & Millichap, SRS, Hanley, SVN, Franklin Street, and Lee &
  Associates. Every ingest kept status activation off; source disappearance
  signals were recorded but no listing was deactivated.
- Added a tested `BUILDOUT_REFRESH_PAGE_CACHE=1` switch. It bypasses stale
  durable Buildout page-cache reads during an explicitly requested live refresh,
  refreshes successfully fetched cache pages, and rejects incompatible
  cache-only/assembly recovery modes. The live SVN and Lee runs used it.
- Bounded Savills list-page rendering to a configurable 30-second default
  (`SAVILLS_LIST_TIMEOUT_MS`, range 10–90 seconds). It prevents the
  enumeration-only source from inheriting a 90-second timeout three times per
  failed page while preserving an explicit recovery override.
- Used monitor-only current enumeration for Colliers Main, Matthews, and
  Transwestern, which is their intended low-cost current-data path. The monitor
  is observe-only: it records events, refreshes source enumeration metadata,
  and queues targeted enrichment without changing listing status.
- The first broad monitor pass covered 66,304 grouped current source records.
  It produced 3,319 events: 1,032 new, 1,284 price/status changes, 999
  disappearances, and four reappearances. The subsequent live Lee pass added
  1,934 events: 839 new, 350 price/status changes, and 745 disappearances.
- Mid-run readback: 113,484 active listings; 66,305 freshly re-observed since
  the run began (58.43% of active inventory); 5,652 active records created
  during the run (4.98% of active inventory). The starting active baseline was
  107,801, so the active inventory increased by 5,683 (+5.27%).
- `cre_validate.py` completed with `ok: true`. It found no orphan child rows,
  bad child URLs, or duplicate external-id groups. It retained the existing
  database collation-version warning and source-URL duplicate findings for
  review; neither was introduced by this additive refresh.

## Recovery, final validation, and bounded follow-up

- The 18 completed supported sources produced 97,217 grouped monitor records
  and 5,555 append-only events: 1,871 new (1.92%), 1,635 price/status changes
  (1.68%), 2,018 disappeared (2.08%), and 31 reappeared (0.03%). The combined
  observed-change rate is 5.71%. Disappearances are review signals only; this
  run never changed listing status or `deleted_at`.
- Final readback: 114,323 active listings; 75,622 refreshed through a full
  additive ingest (66.15% of active inventory); 6,491 active records created
  during this run (5.68%); and a net active increase of 6,522 from the 107,801
  baseline (+6.05%).
- Savills recovered through direct server-rendered `__NEXT_DATA__` enumeration,
  with a validated Firecrawl fallback and real provider `NextUrl` pagination.
  The live U.S. result was two commercial lease records; the coverage gate
  correctly suppressed disappearance inference from its older 103-record
  snapshot.
- NAI Global recovered through bounded body reads, 100-row GraphQL pages,
  40-office batches, and a bounded fan-out of two. Its complete monitor
  artifact contained 13,779 records: 10,419 sale and 3,360 lease, with no
  source error or truncation. The source index is now fresh at that count.
  The monitor classified 12,517 as `enumerated_unmatched`, not new database
  records, because the monitor payload intentionally omits the detail/status
  fields needed to make an additive listing write safe.
- The remaining targeted-enrichment queue is 2,672 rows (principally Lee, SVN,
  CBRE, Cushman, and Newmark). All 766 Marcus rows were drained through the
  new exact `cre_enrich.py --source marcus-millichap` claim filter. Rows that
  lack safe enrichment output remain queued/retryable rather than being
  replaced with thin data. Do not reload recurring launchd jobs in this run:
  drain/review the queue after the code is merged and follow the documented
  Gate 5 approval path for monitor, enrich, and weekly only (never the retired
  daily job).
- Final read-only validation returned `ok: true`; the known database collation
  warning remains outside this refresh. Publish the concise evidence comment
  to AGENTIC-1229. Do not reload recurring launchd jobs in this run: scheduler
  restoration remains gated on a clean merged deployment and the documented
  Gate 5 approval path.
