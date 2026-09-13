# Active Pointer

- Branch: `fix/cre-refresh-test-determinism`
- Workstream: `tasks/2026-09-08-cre-listing-full-refresh/`
- Goal: complete the strict 51-source CRE listing refresh and prove current Supabase freshness and integrity.
- Next action: finish exact Colliers sitemap/API reconciliation and field-parity verification, push the source fixes and migration-016 fail-fast preflight, then run a new full checkpoint series from the exact pushed SHA.
- Latest verification: production migration-016 readback passed 11/11 contract items; the public Colliers Sitecore/Coveo query matched all 15,944 current sitemap IDs in a read-only probe; collector Python suite passed with 2,138 tests and one intentional skip on 2026-09-09.
