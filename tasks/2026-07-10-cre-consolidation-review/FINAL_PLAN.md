# CRE Data Platform: Final Plan (2026-07-10)

> **Historical planning artifact.** This review informed the later safety work,
> but its scheduler alternatives and one-migration-home proposal are superseded.
> Current policy is aa-hub only for GetCREdata scheduling, object-level schema
> ownership, and the gated [operator runbook](2026-07-11-firecrawl-operator-runbook.md).

**Author:** Fable 5 synthesis over a 13-agent review (5 Opus/Sonnet surveys, 33-claim Opus fact-check, Opus consolidation analysis, 3 idea lenses).
**Companion files:** `report-1-firecrawl-cre-listing-system.md`, `report-2-getcredata-market-pipeline.md`, `report-3-consolidation-analysis.md`, `IMPROVEMENT_IDEAS.md`.

---

## 1. The answer to your question

**No, do not consolidate the two codebases into one repo. But yes, something does need consolidating: the data contract, not the code.**

The two systems are complements, not duplicates:

- **The listing system** (this fork, `scripts/firecrawl-ops/`): scrapes ~20 brokerage sources through the local self-hosted Firecrawl stack, upserts listing-level rows into Supabase `credeals.cre_listings` and children, runs on Mac mini launchd, feeds the live EQUIRE deal board. TypeScript + Python + bash, host-locked, transactional SLA.
- **GetCREdata**: pulls ~35 public/federal sources, computes 160+ CBSA-level market metrics and sector scores, exports 6 market tables to the same Supabase project for the EQUIRE chatbot. Pure Python, runnable anywhere, batch SLA.

The recommended end state (scored 8/10 by the analysis, and I concur):

1. **Extract the listing system into its own repo** (working name `cre-listings`), leaving this firecrawl fork as pure scraping infrastructure. The extraction is cheap because the coupling is shallow: the collector already talks to any Firecrawl endpoint via `FIRECRAWL_API_URL`; only convenience/healthcheck scripts hard-code `FC_DIR` and docker compose.
2. **Keep GetCREdata separate.** Different language, runtime, cadence, and product SLA. Merging would let a batch market-data change break the live deal board's CI, and the true overlap is one table plus one geo crosswalk, far too small to justify a merge.
3. **Consolidate ownership of the `credeals` schema in one place** (a written table-ownership contract plus one migration home), because that is where the systems actually collide today.

Full option scoring and the per-option pros/cons are in report-3. Full merge scored 3/10; extract-and-merge into GetCREdata scored 5/10; status quo scored 5/10; contract-only scored 7/10 (right first phase, wrong end state).

## 2. Why this is urgent: the two repos are already colliding in production

The single most important finding of this review, verified in code on both sides:

- Both repos write the **same Supabase project (`fhqycqubkkrdgzswccwd`), same schema (`credeals`)**, with **zero mutual awareness**: neither repo mentions the other anywhere (repo-wide greps on both sides).
- GetCREdata's `documents/` pipeline has (per its own `SUPABASE_DATA_MAP.md`, 2026-06-24) already written **~398,040 rows into `credeals.cre_listing_om_facts`**, the table this fork created in `sql/013` and whose own docs still say "remains EMPTY, OM-parse gated" (stale since 2026-06-15).
- GetCREdata upserts that table with a **5-column conflict key** (`documents/writer.py:29`, includes `parser_version`) while this fork's checked-in DDL creates a **4-column unique index** (`sql/013_cre_listing_om_facts.sql:69`). No widening migration exists in either repo. If this fork's gated `om_parse.py --apply` ever runs, the two writers can collide or coexist incorrectly.
- Meanwhile, this fork has a second, never-run OM parser (`om_parse.py`) that was blocked by the CBRE/JLL anti-bot problem GetCREdata already solved a different way (Gemini via Vercel AI Gateway with a Firecrawl-cloud fallback).

Caveat carried through from the fact-check: the 398k row count and the launchd tier health are documentation claims, not live reads. Phase 0 below starts with a read-only verification.

## 3. The plan

Every step respects the hard rules: feature branches only, no pushes to main, no PR creation without authorization, no DDL applied and no launchd/aa-hub changes without your explicit go-ahead. Each phase is independently shippable and reversible.

### Phase 0: Truth and contract (this week, no code moves, lowest risk)

1. **Read-only DB verification** (one psql session, SELECT only): actual row count of `cre_listing_om_facts`, the actual unique-index definition on it, live `cre_listings` counts, and last-write timestamps per table. This settles the docs-vs-docs contradictions before anything else acts on them.
2. **Write the `credeals` ownership contract**: one markdown manifest enumerating every table/view, its sole writer repo, and its migration home. Proposed split: this fork (later `cre-listings`) owns `cre_listings`, children, crosswalk, and all DDL; GetCREdata owns the 6 market tables, CMBS/REIT/cap-rate tables, and `om_facts` population. The manifest lives with the SQL source of truth and is mirrored in both repos' CLAUDE.md files.
3. **Fix the stale docs on both sides** (fork CLAUDE.md om_facts status; launchd/CLAUDE.md 2026-06-15 stale banner; GetCREdata step-count drift 25/26/30).
4. **Draft, do not apply, the index-alignment migration** for `om_facts` (recommend widening to the 5-column key so parser generations can coexist), staged for your approval.
5. **Decide OM-parser ownership**: with GetCREdata's extractor proven at scale, the fork's `om_parse.py` should be retired or explicitly demoted to a fallback, not activated.

### Phase 1: Stabilize what is live (before moving anything)

1. **Root-cause the launchd tier failures.** Docs (2026-07-05) report monitor OK but enrich/daily/weekly last-failed rc:1. Read the actual tier logs and markers on the Mac mini, fix, and verify with a clean scheduled cycle. Do not extract a broken pipeline.
2. **Finish the aa-hub GetCREdata lane.** GitHub Actions is not an approved
   scheduler. The aa-hub job remains disabled until the reviewed runtime,
   environment, snapshot, validation-only, and supervised-export gates pass.
3. **Minimal alerting**: failure webhook in `cre_run_tier.sh` finish hook, plus a consecutive-failure counter, so silent multi-week tier failures cannot recur.

### Phase 2: Extract the listing system (gated on your approval to create the repo)

1. Create `cre-listings` via `git filter-repo` on a branch, preserving `scripts/firecrawl-ops/` history.
2. Make `FC_DIR`/healthcheck coupling optional; the collector keeps using `FIRECRAWL_API_URL` (unchanged, points at the same local stack).
3. Run the extracted copy dark, in parallel with the fork copy, until N consecutive runs produce identical artifacts; only then flip launchd plists (with your approval). The fork copy stays as rollback until verified.

### Phase 3: Fork cleanup (after the flip)

Delete (with timestamped backups per house rules) `cre_scrapers/`, `cre_pipeline.py`, and the ~300k-line `prometheus/data.json` bloat; trim the upstream-sync protected-paths allowlist; leave a deprecation pointer. This shrinks the biweekly upstream-sync blast radius, which currently touches 50-180 files per sync.

### Phase 4: Product unlocks (the payoff, can start any time after Phase 0)

The two highest-leverage cheap wins found by the review, both enabled by the shared project:

1. **Join listings to market context.** `cre_listings` already carries `cbsa_code`; GetCREdata's `cbsa_market_data` is keyed by it, same database. One read-only view (`v_cre_listing_market_context`) puts metro fundamentals on every EQUIRE deal card. Near-zero schema cost.
2. **One geo source of truth.** Promote `credeals.cre_zip_cbsa_crosswalk` to canonical and have GetCREdata resolve geo through it, retiring the duplicated live Census download, so a listing and its market row can never land in different CBSAs.

The remaining 30+ ideas are ranked in `IMPROVEMENT_IDEAS.md`.

## 4. Decisions I need from you

1. Approve the Phase 0 read-only DB verification (SELECT-only psql session against prod).
2. Confirm the recommended end state (Option 3: extract to `cre-listings` + shared contract) or pick another; the repo name if extraction proceeds.
3. Which conflict key is canonical for `om_facts` (recommend 5-column) so the staged migration can be finalized for your review.
4. GetCREdata scheduling lane: complete the gated aa-hub activation path.
5. Whether Phase 4 item 1 (the listing-to-market join view) should be drafted now; it is independent of everything else.

## 5. Evidence quality

All code claims in the reports carry file:line citations that survived a dedicated Opus fact-check (33 claims checked, 1 softened, 0 refuted). Two things remain documentation-only until the Phase 0 read-only check: the 398,040-row om_facts count and the 2026-07-05 launchd tier health. Both repos show a pattern of docs drifting from code, so no irreversible action should key off doc claims alone.
