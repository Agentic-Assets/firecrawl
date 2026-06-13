# CRE Collector And Live Board Contract Sync - 2026-06-12

This note reconciles the collector-side contract with the UI-side live board
handoff docs in
`/Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/docs/superpowers/plans/`.

## Current Contract

- Supabase is a server-only snapshot index for the display board. The deployed
  live board must not depend on local Firecrawl.
- Collector-owned `credeals.cre_*` base tables and `v_cre_*` views are
  service-role only. `anon` and `authenticated` should not have direct table or
  view `SELECT`.
- The display views use `security_invoker=true` in the live Supabase posture.
- `credeals.search_cre_listings(text,text,text,text,text)` and
  `credeals.update_cre_listing_timestamp()` should be executable by
  `service_role`, not by `public`, `anon`, or `authenticated`.
- Documents and images are URL-only. The collector stores external URLs and
  metadata; it does not upload PDF or image binaries into Supabase storage.
- UI `snapshotFetchedAt` maps to collector `scraped_at`, which is the collector
  snapshot time.
- UI `snapshotUpdatedAt` maps to database `updated_at`, which is the database
  row mutation time.
- `listing_date` is source-proven first-listed, date-published, or on-market
  date only. Do not infer it from scrape time, database `updated_at`, or generic
  `lastUpdated` fields.
- `updated_date` is source-provided listing recency or last-modified date when
  exposed by the brokerage. It is not necessarily the first-listed date.

## Stale Or Contradictory Claims Found

- `2026-06-12-cre-supabase-live-hybrid-completed.md` still lists function
  execute hardening as not complete.
- `2026-06-12-cre-supabase-live-hybrid-finish-plan.md` still includes an
  unchecked Phase 2 for function privilege hardening.
- Later UI-side docs outside those two plan files record the function hardening
  as complete in `docs/supabase-cre-function-security-2026-06-12.sql` and
  `docs/cre-live-listing-current-state-2026-06-12.md`. The collector docs and
  security note follow that later posture.
- `scripts/firecrawl-ops/sql/005_cre_views.sql` does not itself encode the
  `security_invoker` view settings or final function grants. For a fresh
  database rebuild, apply the separate view and function hardening SQL recorded
  in the UI repo, then verify grants again.

No mismatch was found on URL-only documents/images or the conservative date
semantics after the 2026-06-12 collector doc updates.
