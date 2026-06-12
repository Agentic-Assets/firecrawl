# CRE Collector Validation Gap Audit - 2026-06-12

Scope: read-only audit of tracked collector docs and live Supabase state after
the latest completed source-specific loads. No collector code, generated
artifacts, or database rows were changed. Supabase was queried through the
existing `cre_ingest.py::load_db_url(None)` and `find_psql()` helper pattern,
with SQL sent on stdin inside `BEGIN READ ONLY`; the database URL was not
printed. Document and image handling remains URL-only.

## Live Active Counts

Live totals from Supabase:

- `credeals.cre_listings` active rows: 64,539 after the later Savills lease
  live ingest.
- `credeals.v_cre_listings_full` rows: 64,539 after the later Savills lease
  live ingest.
- `credeals.v_cre_active_for_sale` rows: 24,455.
- `credeals.v_cre_active_for_lease` rows: 43,499 after the later Savills lease
  live ingest.
- `credeals.v_cre_market_summary` rows: 15,411.

Source-split active counts:

| Source | Active | Sale | Lease | Sale/lease | Latest batch active | Latest scraped at | Soft-deleted |
|---|---:|---:|---:|---:|---:|---|---:|
| CBRE | 19,028 | 4,222 | 13,145 | 1,661 | 19,023 | 2026-06-12 04:31:24Z | 0 |
| CBRE Deal Flow | 1,857 | 1,830 | 27 | 0 | 1,836 | 2026-06-12 09:23:38Z | 0 |
| JLL | 4,543 | 198 | 4,210 | 135 | 4,543 | 2026-06-12 04:31:24Z | 0 |
| JLL Investor | 50 | 50 | 0 | 0 | 50 | 2026-06-12 04:31:24Z | 0 |
| Cushman & Wakefield | 11,318 | 2,743 | 8,575 | 0 | 11,318 | 2026-06-12 12:09:41Z | 24 |
| Newmark | 4,371 | 1,121 | 3,250 | 0 | 4,371 | 2026-06-12 13:52:24Z | 715 |
| Marcus & Millichap | 3,124 | 3,124 | 0 | 0 | 3,124 | 2026-06-12 13:08:59Z | 0 |
| Avison Young | 2,200 | 636 | 1,431 | 133 | 2,200 | 2026-06-12 09:33:46Z | 0 |
| Savills | 104 | 101 | 3 | 0 | 2 | 2026-06-12 21:05:23Z | 0 |
| SVN | 5,287 | 2,660 | 2,192 | 435 | 5,287 | 2026-06-12 20:41:44Z | 34 |
| NAI Global | 241 | 183 | 58 | 0 | 241 | 2026-06-12 11:31:05Z | 19 |
| Lee & Associates | 9,223 | 2,611 | 5,691 | 921 | 9,223 | 2026-06-12 13:39:08Z | 0 |
| Colliers SalesTracker | 1,172 | 1,172 | 0 | 0 | 1,172 | 2026-06-12 10:05:58Z | 0 |
| Transwestern | 2,021 | 389 | 1,502 | 130 | 2,021 | 2026-06-12 12:42:43Z | 0 |

The live active total is updated here after the later Savills lease live ingest.
The previous 64,537 total was two rows lower before those additive lease rows.

## Status By Source

Clearly complete public-feed sources:

- CBRE main listing feed: complete public feed, but 5 older active rows remain
  outside the latest batch.
- Cushman & Wakefield: complete public API feed with detail enrichment and
  source-scoped reconciliation.
- Newmark: complete public Algolia feed with no-state DC recovery and contact
  enrichment; documents, full galleries, second/third broker joins, and VCards
  remain unproven enrichment gaps rather than feed blockers.
- Marcus & Millichap: complete public sale feed; public lease remains blocked.
- NAI Global: complete active public Infabode feed after `FOR_SALE_ON_MARKET`
  filtering; historical/public non-active rows are intentionally excluded.
- Lee & Associates: complete public Buildout feed from durable cache assembly
  and live ingest.
- SVN: current live Supabase job is complete with source-scoped reconciliation,
  despite older README language that still described live refresh as partial.
- Transwestern: complete public GET feed with detail enrichment.

Partial or limited sources:

- CBRE Deal Flow: complete for the exposed public RCM card endpoint, but still
  partial relative to reported source totals and gated deal-room material.
- Avison Young: public SharpLaunch feed complete. Bounded detail-page
  enrichment for selected rows is now implemented and verified after this
  audit; full-feed detail enrichment has not been live-run.
- Colliers: SalesTracker investment-sale subset is live and validated; main
  Colliers Coveo sale/lease coverage remains blocked.
- JLL main: collects current search rows, but no documents, contacts, images
  beyond minimal source data, and all 4,543 active rows lack coordinates.
- JLL Investor: still first rendered batch only, 50 rows, with policy-sensitive
  pagination choices unresolved.
- Savills: not defensible as U.S. CRE sale coverage; only a tiny commercial
  lease path plus legacy/global/residential sale rows remain.

Blocked or product-decision gaps:

- Main Colliers `www.colliers.com/en/properties`: blocked until a safe public
  non-POST path or authorized integration exists.
- Marcus lease: blocked until a public lease UI mode or endpoint is proven.
- JLL Investor full pagination: blocked on route choice because query-string
  search is robots-disallowed; sitemap/detail crawl is cleaner but heavier.
- Marcus auctions: available publicly, but excluded pending product decision.
- Savills U.S. commercial sale inventory: no safe public source found yet.

## Validation Findings

Strong checks:

- No active duplicate `(brokerage_id, external_id)` groups.
- No orphan contacts, documents, or images.
- No non-HTTP document or image URLs.
- No missing source URLs on active rows.
- `search_cre_listings` smoke calls returned rows for industrial Texas sale,
  office sale, Lee, and National Avenue samples.
- Recent source-specific jobs for SVN, Newmark, Lee, Marcus, Transwestern,
  Colliers, NAI, Avison Young, CBRE Deal Flow, and Cushman are completed with
  `errors_count = 0`.

High-priority validation gaps:

1. Reconcile stale additive rows where safe: 21 CBRE Deal Flow rows, 5 CBRE
   rows, and 2 Savills rows are older than each source's latest active batch.
2. Audit missing states and missing coordinates by source before relying on
   map, state-filter, or market-summary workflows. Largest missing-coordinate
   gaps are JLL 4,543, Savills 102, JLL Investor 50, CBRE Deal Flow 427, SVN
   40, Lee 27, Cushman 13. Missing-state gaps include Lee 323, CBRE Deal Flow
   118, Marcus 101, JLL Investor 50, Cushman 31, Colliers 29, CBRE 23.
3. Review duplicate source URLs: Avison Young has 4 groups / 8 rows, CBRE Deal
   Flow has 21 groups / 42 rows, and Cushman has 8 groups / 18 rows. External
   IDs are still unique, so this is a display/merge-policy issue, not an
   upsert-key failure.
4. Closed after this audit: `cre_ingest.py` now drops non-HTTP contact and
   document URLs, and Lee/SVN live child refresh reduced bad active
   `avatar_url` values from 37 to 0. Profile and VCard URL checks stayed clean.
5. Review price and rate outliers flagged by conservative QA: Avison Young has
   2 sale-price flags and 1 PSF flag, Lee has 1 PSF flag and 3 lease-rate
   flags, SVN has 2 PSF flags and 2 lease-rate flags, Colliers has 1 PSF flag,
   and CBRE has 3 PSF flags.
6. Bring source docs back into alignment where older notes conflict with live
   status. SVN's README was aligned after this audit; the remaining immediate
   doc correction is the older total-active count in `START_HERE.md`.

## Next Recommended Checks

1. Run a focused stale-row reconciliation audit before any broad all-source
   `--mark-missing` run.
2. Run source-specific coordinate/state QA for JLL, JLL Investor, Savills, CBRE
   Deal Flow, Lee, SVN, Cushman, Colliers, and Marcus.
3. Add a saved read-only validation command or script that reproduces this
   source-split active count, child-count, stale-row, duplicate-URL, and quality
   flag report without printing credentials.
4. Keep Colliers main, Savills U.S. commercial sale, Marcus lease, JLL Investor
   full pagination, and Marcus auctions out of completeness claims until the
   specific blocker or product decision is resolved.
