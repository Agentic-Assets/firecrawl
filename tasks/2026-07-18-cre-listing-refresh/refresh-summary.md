# CRE listing refresh summary - 2026-07-18

## Outcome

The supported collector completed a controlled additive refresh for 18 of 20
supported source adapters. No listing status, soft-delete field, OM-facts row,
or EQUIRE market-data object was changed by this run.

| Measure | Result |
| --- | ---: |
| Active listings before run | 107,801 |
| Active listings after run | 114,323 |
| Net active change | +6,522 (6.05%) |
| Active records created during run | 6,491 (5.68% of current active inventory) |
| Listings fully re-observed through additive ingest | 75,622 (66.15% of current active inventory) |
| Current records enumerated by the monitor | 97,217 |
| Monitor events | 5,555 (5.71% of monitored records) |

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
- Fresh Buildout inventory fetches used `BUILDOUT_REFRESH_PAGE_CACHE=1`; this
  bypasses stale durable page-cache reads only when explicitly requested.

## Deferred sources and exact next steps

1. Retry NAI Global and Savills after their public endpoints are responsive.
   Their bounded attempts produced no complete artifact and therefore made no
   database or monitor write. Do not label either source refreshed until it
   produces a complete artifact.
2. Drain the 3,442 targeted enrichment rows in safe batches after the branch is
   reviewed and merged. The queue is intentionally retained when a page lacks
   a safe enrichment payload; it must not be completed with URL-only data.
3. Re-run `cre_validate.py` after each deferred source recovery and after the
   queue materially drains. The final validation for this run returned `ok:
   true`; known database collation and source-URL duplicate findings remain
   separate cleanup work.
4. Do not restore recurring launchd work yet. After a clean merged deployment
   and the documented Gate 5 approval, enable monitor, enrich, and weekly only.
   The retired daily job remains disabled.

## Verification

- Collector `npm run typecheck` passed.
- Focused TypeScript tests for enrichment, Buildout, Cushman, JLL Investor,
  NAI, Savills, and scrape deadlines passed.
- Python enrichment/queue tests passed: 64 tests.
- Final `cre_validate.py` returned `ok: true`, with no orphan child rows, bad
  child URLs, or duplicate external-id groups.

See `progress-log.md` for the run chronology and durable artifact locations.
