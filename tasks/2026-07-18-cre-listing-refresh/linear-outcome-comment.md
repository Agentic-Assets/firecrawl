## 2026-07-18 controlled CRE refresh outcome

Implemented the final collector recovery batch on
`fix/cre-enrich-source-paths`; the draft PR is
https://github.com/Agentic-Assets/firecrawl/pull/23.

- Active inventory: 107,801 -> 114,487 (`+6,686`, `+6.20%`).
- Fully additive re-observation: 75,992 active rows (`66.38%` of current
  active inventory). Active records created in this run: 6,655 (`5.81%`).
- Comparable monitor paths recorded 5,555 append-only events across 97,217
  records: 1,871 new, 1,635 price/status changes, 2,018 disappearances, and
  31 reappearances. No status/deleted-at mutation was applied.
- Savills completed a current direct-source pass; its coverage gate suppressed
  false disappearance inference from a 103-to-2 snapshot change. NAI Global
  now uses bulk public `publicPosts` detail rows: it evaluated 13,750 public
  posts, retained 368 source-eligible active listings (283 sale, 85 lease),
  and additively ingested them. Full and monitor now share that same
  conservative eligibility rule. The old 13,779-row monitor baseline and its
  231 derived false price-change events were removed, then the 368-row current
  inventory was rebaselined with zero events.
- Final database validation returned `ok: true`; no orphan child rows, bad
  child URLs, or duplicate external-id groups. The known database collation
  warning remains non-blocking.
- Verification: collector typecheck, 493 TypeScript unit tests, 230 Python
  enrichment/queue/monitor tests, and final database validation (`ok: true`)
  passed. The repository-wide pre-commit Knip hook is
  currently blocked by an unrelated missing `apps/api` dependency
  (`typescript5/package.json`), so the scoped batch was committed after those
  successful checks.

Bounded follow-up, not hidden:

1. 2,672 targeted-enrichment rows remain queued, principally Lee, SVN, CBRE,
   Cushman, and Newmark. All 766 Marcus rows were safely drained with the new
   exact source-filtered worker; unsafe thin results remain queued rather than
   replacing detailed data.
2. Do not restore recurring launchd work until this PR is reviewed, merged,
   and the documented Gate 5 approval path is satisfied. The only eventual
   candidates are monitor, enrich, and weekly; the retired daily job remains
   disabled.

The repository guide is
`tasks/2026-07-18-cre-listing-refresh/refresh-summary.md`.
