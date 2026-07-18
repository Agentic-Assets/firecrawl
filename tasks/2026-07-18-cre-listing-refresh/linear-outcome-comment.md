## 2026-07-18 controlled CRE refresh outcome

Implemented and pushed the collector hardening batch on
`fix/cre-enrich-source-paths` at `7020877773a2fa2793147843dffca30f50691db4`.
Draft PR: https://github.com/Agentic-Assets/firecrawl/pull/23

- Active inventory: 107,801 -> 114,323 (`+6,522`, `+6.05%`).
- Fully additive re-observation: 75,622 active rows (`66.15%` of current
  active inventory). Active records created in this run: 6,491 (`5.68%`).
- The 18 completed monitor source paths enumerated 97,217 current source
  records and recorded 5,555 append-only events: 1,871 new, 1,635 price/status
  changes, 2,018 disappearances, and 31 reappearances. No status/deleted-at
  mutation was applied.
- Final database validation returned `ok: true`; no orphan child rows, bad
  child URLs, or duplicate external-id groups. The known database collation
  warning remains non-blocking.
- Verification: collector typecheck, 485 TypeScript unit tests, and 64 Python
  enrichment/queue tests passed. The repository-wide pre-commit Knip hook is
  currently blocked by an unrelated missing `apps/api` dependency
  (`typescript5/package.json`), so the scoped batch was committed after those
  successful checks.

Deferred, not hidden:

1. NAI Global and Savills produced no complete live artifact within their
   bounded recovery attempts. Neither made an ingest or monitor write. Retry
   after their public endpoints are responsive.
2. 3,442 targeted-enrichment rows remain queued, principally Lee, SVN, and
   Marcus. One 200-row safe batch was run; unsafe thin results remain queued
   rather than replacing detailed data.
3. Do not restore recurring launchd work until this PR is reviewed, merged,
   and the documented Gate 5 approval path is satisfied. The only eventual
   candidates are monitor, enrich, and weekly; the retired daily job remains
   disabled.

The repository guide is
`tasks/2026-07-18-cre-listing-refresh/refresh-summary.md`.
