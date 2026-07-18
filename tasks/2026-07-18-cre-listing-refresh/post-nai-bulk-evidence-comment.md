## 2026-07-18 NAI bulk-detail recovery and final refresh readback

Branch: `fix/cre-enrich-source-paths`  
Draft PR: https://github.com/Agentic-Assets/firecrawl/pull/23

- NAI now reads the public Infabode `publicPosts` bulk-detail feed. The full
  refresh evaluated 13,750 public posts and conservatively retained 368
  source-eligible active listings: 283 sale and 85 lease. Those rows were
  additively ingested without a terminal-status activation.
- Full and monitor now share that exact eligibility rule. The corrected source
  index contains 368 NAI rows and has no pending NAI enrichment rows.
- The old 13,779-row broad NAI index was not comparable to the new inventory.
  Its 231 derived queue rows and 231 false price-change events were removed;
  the 368-row inventory was rebaselined with zero events. No listing, soft
  delete, OM-facts, or EQUIRE market-data record was removed or changed by the
  corrective metadata cleanup.
- Final active inventory is 114,487, up 6,686 (6.20%) from 107,801. This run
  created 6,655 active records (5.81% of current inventory) and fully
  re-observed 75,992 (66.38%). Current source watchers enumerated 97,587
  records. Genuine comparable-source events remain 5,555 / 97,217 (5.71%):
  1,871 new, 1,635 price/status changes, 2,018 disappearance review signals,
  and 31 reappearances.
- Verification: collector typecheck; 493 TypeScript unit tests; 230 Python
  enrichment/monitor tests; final `cre_validate.py --format json` returned
  `ok: true`.

No scheduler was restored. The remaining 2,672 targeted enrichment rows are
kept queued for safe, source-specific enrichment after review; they are not a
reason to overwrite detailed listings with thin URL-only data.
