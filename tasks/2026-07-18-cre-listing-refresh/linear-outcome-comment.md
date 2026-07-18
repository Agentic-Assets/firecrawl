## 2026-07-18 controlled CRE refresh outcome

Implemented the final collector recovery batch on
`fix/cre-enrich-source-paths`; the draft PR is
https://github.com/Agentic-Assets/firecrawl/pull/23.

- Active inventory: 107,801 -> 114,323 (`+6,522`, `+6.05%`).
- Fully additive re-observation: 75,624 active rows (`66.15%` of current
  active inventory). Active records created in this run: 6,491 (`5.68%`).
- Comparable monitor paths recorded 5,555 append-only events across 97,217
  records: 1,871 new, 1,635 price/status changes, 2,018 disappearances, and
  31 reappearances. No status/deleted-at mutation was applied.
- Savills completed a current direct-source pass; its coverage gate suppressed
  false disappearance inference from a 103-to-2 snapshot change. NAI Global
  completed a 13,779-record untruncated current-feed pass (10,419 sale and
  3,360 lease). Its 12,517 previously unmatched rows are deliberately held
  out of additive ingestion until a detail/status enrichment path exists.
- Final database validation returned `ok: true`; no orphan child rows, bad
  child URLs, or duplicate external-id groups. The known database collation
  warning remains non-blocking.
- Verification: collector typecheck, 491 TypeScript unit tests, and 69 Python
  enrichment/queue tests passed. The repository-wide pre-commit Knip hook is
  currently blocked by an unrelated missing `apps/api` dependency
  (`typescript5/package.json`), so the scoped batch was committed after those
  successful checks.

Bounded follow-up, not hidden:

1. Build the bounded NAI detail/status enrichment path before adding its 12,517
   monitor-only unmatched rows as listings.
2. 2,672 targeted-enrichment rows remain queued, principally Lee, SVN, CBRE,
   Cushman, and Newmark. All 766 Marcus rows were safely drained with the new
   exact source-filtered worker; unsafe thin results remain queued rather than
   replacing detailed data.
3. Do not restore recurring launchd work until this PR is reviewed, merged,
   and the documented Gate 5 approval path is satisfied. The only eventual
   candidates are monitor, enrich, and weekly; the retired daily job remains
   disabled.

The repository guide is
`tasks/2026-07-18-cre-listing-refresh/refresh-summary.md`.
