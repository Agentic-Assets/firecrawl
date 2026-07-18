# CRE listing refresh summary - 2026-07-18

## Outcome

The supported collector completed a controlled refresh for all 20 supported
source adapters. No listing status, soft-delete field, OM-facts row, or EQUIRE
market-data object was changed by this run.

| Measure | Result |
| --- | ---: |
| Active listings before run | 107,801 |
| Active listings after run | 114,323 |
| Net active change | +6,522 (6.05%) |
| Active records created during run | 6,491 (5.68% of current active inventory) |
| Listings fully re-observed through additive ingest | 75,622 (66.15% of current active inventory) |
| Current records enumerated by source watchers | 110,998 |
| Confirmed monitor events | 5,555 (5.71% of the 97,217 records with a comparable prior snapshot) |

## What changed

The change ledger is the correct measure of a source-observed listing change.
It is distinct from `scraped_at`, which advances for every successful refresh.

| Change type | Count | Share of monitored records |
| --- | ---: | ---: |
| New listing | 1,871 | 1.92% |
| Price or status change | 1,635 | 1.68% |
| Disappeared from source feed | 2,018 | 2.08% |
| Reappeared | 31 | 0.03% |

Disappearance is an append-only review signal. It did not deactivate a
listing, because public source coverage cannot safely prove that a property is
no longer active.

The 6,491 database-created records are not expected to equal the 1,871 monitor
`new` events: several sources had no prior monitor index and therefore seeded a
baseline silently, while the database count also reflects identity-normalized
additive upserts.

## Completed source paths

- Full additive collector and ingest: CBRE, CBRE Dealflow, JLL, JLL Investor,
  Colliers, Cushman & Wakefield, Newmark, Avison Young, Kidder Mathews, Marcus
  & Millichap, SRS, Hanley, SVN, Franklin Street, and Lee & Associates.
- Current-enumeration monitor: Colliers Main, Matthews, and Transwestern.
- Savills now uses its server-rendered public list state directly, with a
  validated Firecrawl fallback. It enumerated two live U.S. commercial lease
  records; its coverage guard suppressed any false disappearance inference from
  the older 103-record snapshot.
- NAI Global now uses bounded body reads, 100-row API pages, and a two-batch
  fan-out. Its complete public-feed monitor observed 13,779 records (10,419
  sale and 3,360 lease) without truncation.
- Fresh Buildout inventory fetches used `BUILDOUT_REFRESH_PAGE_CACHE=1`; this
  bypasses stale durable page-cache reads only when explicitly requested.

## Bounded follow-up work

1. NAI’s 12,517 previously unmatched public-feed records were deliberately not
   added as thin, monitor-only database listings. They need a bounded
   detail/status enrichment path before additive ingestion. This is a data
   quality follow-up, not a failed source refresh.
2. Drain the remaining 2,672 targeted enrichment rows after the branch is
   reviewed and merged. Marcus & Millichap’s 766 rows were safely drained using
   the new exact `--source` claim filter. The remaining queue is intentionally
   retained when a page lacks
   a safe enrichment payload; it must not be completed with URL-only data.
3. Re-run `cre_validate.py` after the NAI detail follow-up and after the queue
   materially drains. The final validation for this run returned `ok: true`;
   known database collation and source-URL duplicate findings remain
   separate cleanup work.
4. Do not restore recurring launchd work yet. After a clean merged deployment
   and the documented Gate 5 approval, enable monitor, enrich, and weekly only.
   The retired daily job remains disabled.

## Verification

- Collector `npm run typecheck` passed.
- Full collector TypeScript unit suite passed: 491 tests.
- Python enrichment/queue tests passed: 69 tests.
- Final `cre_validate.py` returned `ok: true`, with no orphan child rows, bad
  child URLs, or duplicate external-id groups.

See `progress-log.md` for the run chronology and durable artifact locations.
