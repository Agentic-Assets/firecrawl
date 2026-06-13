# CLAUDE.md Maintenance Report

- **Run mode:** apply
- **Date:** 2026-06-13
- **Branch:** feat/cre-brokerage-collectors-2026-06-12
- **Scope:** 7 existing CLAUDE.md files, audited in 3 per-subtree groups
- **Verdicts:** 3 significant edits, 4 minor edits

## 1. Executive summary

This run audited all 7 CLAUDE.md guidance files (root, `examples/domain-availability/`, and the five `scripts/firecrawl-ops/` subtree files) in 3 per-subtree groups. Two files were edited in place (root `CLAUDE.md` 182 to 157 lines, `examples/domain-availability/CLAUDE.md` 136 to 126 lines; 12 in-file changes, net -35 lines), trimming a block of corbis-imported testing references that do not exist in this repo and condensing always-on CRE module detail (the colliers-main live status, the section-14.4 build sequence) down to pointers. Eight new folder-level CLAUDE.md files (~291 lines total) were added across `apps/api/` to close a coverage gap in the actual product code. The dominant theme is drift from migration 007: it landed in code (`007_cre_change_tracking.sql`, `000_run_all.sql`, `005_cre_views.sql`) but three docs (`sql/`, `firecrawl-ops/`, and the root) still describe a pre-007 schema, producing three high-severity flags that need a human-applied fix because they were out of scope for the two-file remediation. Health scores for the four scored files range 52 to 67, with `sql/CLAUDE.md` lowest at 52.

## 2. What changed this run

**Files edited (in place):**

| File | Lines before | Lines after | Changes |
| --- | --- | --- | --- |
| `CLAUDE.md` (root) | 182 | 157 | 7 |
| `examples/domain-availability/CLAUDE.md` | 136 | 126 | 5 |

Net: -35 lines, 12 changes across 2 files.

**Dedupes applied / decided (see ledger in section 4):** 5 cross-tree duplications condensed to pointers, 1 deliberately left in place as load-bearing (credeals RLS contract). 3 altitude moves pull module-level detail out of always-on files down to their owning docs.

**Coverage files added:** 8 new folder-level CLAUDE.md files under `apps/api/` (~291 lines total). See section 5.

## 3. Top stale / broken references fixed or flagged

Grouped by area. "Fixed" = addressed in the root edit; "Flagged" = high-severity, out of scope for the two edited files, needs human follow-up.

| Area | File | Issue | Status |
| --- | --- | --- | --- |
| Root / testing harness | `CLAUDE.md` | `pytest Tests/cli/` references a corbis-only path absent from this repo | Fixed (root edit) |
| Root / testing harness | `CLAUDE.md` | `run_all_tests.py` does not exist under `.agents/skills/` or the repo | Fixed (root edit) |
| Root / testing harness | `CLAUDE.md` | `corbis test *` CLI is not part of this repo | Fixed (root edit) |
| Root / skills path | `CLAUDE.md` | `.agents/skills/supabase-postgres-best-practices/SKILL.md` mirror path does not exist | Flagged: drop the link or create the mirror (human decision) |
| SQL migrations | `scripts/firecrawl-ops/sql/CLAUDE.md` | `000_run_all.sql` row states run order `001,002,003,004,006,005`; actual order is `001,002,003,004,007,006,005` | Flagged (high) |
| SQL migrations | `scripts/firecrawl-ops/sql/CLAUDE.md` | `007_cre_change_tracking.sql` is entirely absent from the migration file table though it exists on disk and runs in `000_run_all.sql` | Flagged (high) |
| firecrawl-ops schema inventory | `scripts/firecrawl-ops/CLAUDE.md` | credeals object list missing the four 007 tables (`cre_listing_events`, `cre_source_index`, `cre_enrichment_queue`, `cre_source_baseline`) and `v_cre_recent_changes` | Flagged (high) |
| prometheus broker APIs | `scripts/firecrawl-ops/prometheus/CLAUDE.md` | Cushman & Wakefield and Transwestern sit under "Similar APIs to investigate" but both are fully implemented and validated | Flagged (high) |
| cre_scrapers package layout | `scripts/firecrawl-ops/cre_scrapers/CLAUDE.md` | Package layout is stale (3 broker subdirectories added since last update are missing); CBRE "~5,877 listings" figure is slightly off from the actual `data.json` count | Flagged (medium) |

## 4. Dedup / altitude ledger

Topic, canonical home (single owner going forward), files the duplicate was removed from, action taken.

| Topic | Canonical home | Removed-from | Action |
| --- | --- | --- | --- |
| Colliers two-source mechanic (SalesTracker + colliers-main sitemap unblock, no Coveo POST) | `cre_collector/CLAUDE.md` | root `CLAUDE.md`, `firecrawl-ops/CLAUDE.md`, `prometheus/CLAUDE.md` | condense to pointer |
| CRE change-tracking / monitor build sequence (007 tables, 002/004 ALTERs, capture wins, diff+event runner, launchd order) | `docs/firecrawl-ops/references/cre-intelligence-system-design.md` (section 14) | root `CLAUDE.md` | condense to pointer |
| CRE 2026-06-12 all-source baseline run counts (35,510 raw / 33,488 staged / 34,218 active) | `cre_collector/START_HERE.md` | `firecrawl-ops/CLAUDE.md` | condense to pointer |
| CBRE internal listings JSON API discovery (endpoint, curl, response shape) | `prometheus/CLAUDE.md` | `cre_scrapers/CLAUDE.md` | condense to pointer |
| CRE reference-doc pointer list (`cre-intelligence-system-design.md` + `cre-equire-consumer-api.md`) | `firecrawl-ops/CLAUDE.md` ("Start Here") | `cre_scrapers/CLAUDE.md` | condense to pointer |
| credeals RLS / service-role-only security contract | `sql/CLAUDE.md` (+ `cre_collector/CLAUDE.md`) | none | leave as is (load-bearing safety rule; both copies kept) |
| "Next steps (CRE)" subsection: 14.4 build order, 007 table names, enumeration-key invariant test, jll-investor `<lastmod>` / cbre `Common.Created` capture wins, monitor mode, diff+event runner, tiered launchd | design-doc section 14 | root `CLAUDE.md` (lines 136-173) | altitude move down; root keeps a 1-2 line pointer |
| `colliers-main` full-run live status (~15,896-URL detail run, resumable JSONL cache) | `cre_collector/START_HERE.md` (+ `HANDOFF_COLLIERS_MAIN_2026-06-13.md`) | root `CLAUDE.md` (lines 143-147) | altitude move down; root keeps a pointer |
| `playwright_stealth` launch/context/apply_stealth_sync code block | `examples/domain-availability/check-domains-playwright.py` (`check_domains_batch()`) | `examples/domain-availability/CLAUDE.md` (lines 84-97) | altitude move down; CLAUDE keeps the "why stealth" prose plus a one-line pointer |

## 5. Coverage

**New folder CLAUDE.md added (8, all written this run):**

| Directory | Est. lines |
| --- | --- |
| `apps/api/src/lib` | 35 |
| `apps/api/src/controllers/v2` | 28 |
| `apps/api/src/services/monitoring` | 44 |
| `apps/api/src/scraper/scrapeURL/transformers` | 38 |
| `apps/api/src/scraper/scrapeURL/lib` | 32 |
| `apps/api/src/lib/extract` | 46 |
| `apps/api/src/services/worker` | 38 |
| `apps/api/src/services/webhook` | 30 |

Follow-up tie-in: `apps/api/src/lib/extract` needs a child row added to its parent index `apps/api/src/lib/CLAUDE.md` (flagged below).

**Deliberately skipped:** None recorded this run. No `merge_up` or split candidates were produced, and no directories were marked as intentionally excluded from coverage.

## 6. Flagged for HUMAN follow-up

**Merge-up candidates:** none.

**Split candidates:** none.

**Protected (do not dedup):**
- credeals RLS / service-role-only security contract, kept in both `scripts/firecrawl-ops/sql/CLAUDE.md` and `scripts/firecrawl-ops/cre_collector/CLAUDE.md`. This is a load-bearing safety rule each working agent must see locally; the duplication is intentional.

**High-severity fixes not auto-applied (out of scope of the 2 edited files):**
- `scripts/firecrawl-ops/sql/CLAUDE.md`: correct the `000_run_all.sql` run order to `001, 002, 003, 004, 007, 006, 005` and add a `007_cre_change_tracking.sql` row to the migration table. Following the current table runs migrations out of order and skips 007 entirely.
- `scripts/firecrawl-ops/CLAUDE.md`: add the four 007 tables (`cre_listing_events`, `cre_source_index`, `cre_enrichment_queue`, `cre_source_baseline`) and `v_cre_recent_changes` to the credeals objects list.
- `scripts/firecrawl-ops/prometheus/CLAUDE.md`: rename "Similar APIs to investigate for other brokers" to a discovery log and mark Cushman & Wakefield and Transwestern as implemented; keep JLL as the only open lead.

**Decision needed (unverifiable / two valid fixes):**
- Root `CLAUDE.md` references `.agents/skills/supabase-postgres-best-practices/SKILL.md`, which does not exist. Either drop the "(in-repo mirror: ...)" link and point to the plugin skill `supabase:supabase-postgres-best-practices`, or actually create the mirror if one is intended.

**Coverage follow-up:**
- Add a child row for `apps/api/src/lib/extract` to `apps/api/src/lib/CLAUDE.md`.

**Lower-severity staleness:**
- `scripts/firecrawl-ops/cre_scrapers/CLAUDE.md`: refresh the package layout (3 broker subdirectories are missing) and correct the CBRE "~5,877 listings" figure against the actual `data.json` count.

## 7. Cross-cutting observations

1. **Migration 007 drift is the schema story of this run.** The code change (`000_run_all.sql`, `007_cre_change_tracking.sql`, the `005` view) landed without fanning the documentation out: `sql/CLAUDE.md`, `firecrawl-ops/CLAUDE.md`, and the root all still describe a pre-007 schema, so three docs disagree about which tables exist. Any schema change should update every CLAUDE.md that enumerates schema objects in the same pass.

2. **CRE detail is duplicated across 4+ altitudes and is the single largest drift surface.** The colliers-main live status and the section-14.4 build plan recur in the always-on root, the `firecrawl-ops` hub, `prometheus`, the `cre_collector` docs, and design-doc section 14. Assign one owner each: live run status in `START_HERE.md` / `HANDOFF_COLLIERS_MAIN_2026-06-13.md`, the authorized plan in design-doc section 14, per-source method in `cre_collector/CLAUDE.md`. Everything above those points, never restates.

3. **The always-on root is accumulating module-level CRE cost.** Per-source method, dated run counts, and ordered build steps were all being paid on every turn of every session, including non-CRE and `apps/api` work. The root should keep only the cross-cutting pointer set; the named CRE entrypoints already exist to hold the detail. This run condensed those, but the pattern will recur without discipline.

4. **Reference-doc paths were listed verbatim in four places.** `cre-intelligence-system-design.md` and `cre-equire-consumer-api.md` appeared in the root, the `firecrawl-ops` "Start Here", `cre_scrapers`, and `cre_collector`. The `firecrawl-ops` "Start Here" is the natural canonical list since it auto-loads into both child subtrees; children should keep at most a one-line pointer.

5. **The tree correctly distinguishes wasteful duplication from load-bearing reminders.** Two duplications were preserved on purpose: the credeals RLS contract (a safety rule each agent must see locally) and the CBRE internal-API pointer in the root (already a proper pointer to `prometheus`, not a re-documentation). The default of condense-to-pointer over deletion is the right call for these cross-tree reminders.

6. **Product code (`apps/api/`) was under-documented relative to the ops layer.** Eight active directories (controllers, services/worker, services/webhook, services/monitoring, scraper transformers/lib, lib, lib/extract) had no folder CLAUDE.md while the `scripts/firecrawl-ops/` subtree carried five. This run closed that gap; keep parent index tables (for example `apps/api/src/lib/CLAUDE.md`) in sync as children are added.
