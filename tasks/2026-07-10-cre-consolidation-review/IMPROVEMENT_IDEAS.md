# CRE Data Platform: Improvement Ideas (2026-07-10)

> **Historical idea inventory.** Retain this as source material, not current
> operating instruction. GitHub Actions scheduling is not an option; aa-hub
> activation, object-level schema ownership, and the gated
> [operator runbook](https://github.com/Agentic-Assets/firecrawl/blob/fix/cre-consolidation-safety/tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md)
> supersede conflicting ideas.

All ideas from the 3-lens workflow pass (reliability/ops on Sonnet, data-quality/coverage on Sonnet, strategic/architecture on Opus), deduplicated and re-ranked by Fable. Every idea is grounded in cited code or docs; items marked (docs-claim) depend on facts not yet verified against the live system. Impact/effort tags: H/M/L.

## Priority shortlist (do these first)

| # | Idea | Impact | Effort | Why first |
|---|---|---|---|---|
| 1 | Read-only DB truth check on `om_facts` + tier health | H | L | Everything else keys off facts that are currently docs-only |
| 2 | Root-cause the enrich/daily/weekly rc:1 tier failures | H | L | 3 of 4 scheduled tiers reportedly down; freshness path stalled |
| 3 | Reconcile the `om_facts` 4-col vs 5-col conflict key | H | L | Live cross-repo integrity seam; blocks any OM-parse activation |
| 4 | Pick and finish GetCREdata's scheduling lane | H | M | It currently has no automated refresh at all |
| 5 | Push alerting on tier failure + consecutive-failure counter | H | L | Silent multi-week failures already happened once |
| 6 | Listing-to-market-context join view for EQUIRE | H | L | Biggest product win; join key already exists on both sides |
| 7 | `credeals` table-ownership contract + one migration home | H | M | Fixes the root cause of the cross-repo collision |
| 8 | Wire the existing 1,400-test no-network suite into CI | H | L | Well-built suite currently runs only when someone remembers |

---

## A. Reliability and operations

1. **Root-cause and fix the live enrich/daily/weekly rc:1 failures** (H/L, docs-claim). START_HERE.md (2026-07-05) reports monitor OK, the other three tiers last-failed, with only partial diagnosis. Read `out/daily/cre-enrich.err.log`, `cre-daily.err.log`, `cre-weekly.err.log` and the `last_run_<tier>.json` markers; fix before any other reliability work.

2. **Add push-based alerting on tier failure** (H/L). Failure detection is entirely pull-based today: `cre_run_tier.sh` writes verdict markers but notifies no one (grep confirms alerting is only a TODO). Add a non-blocking webhook (Slack or mail draft) inside `finish()` when `ok:false`, and optionally mirror verdicts to a durable `credeals.cre_run_health` table so freshness is cloud-observable.

3. **Track consecutive failures, not just the last verdict** (M/L). A one-off blip and a three-week outage currently look identical. Add a rolling failure counter to the marker JSON; escalate the `cre_status.sh` WARN at 2-3 consecutive fails. Natural hook for idea 2.

4. **Off-host dead-man's-switch heartbeat** (H/M). `cre_status.sh` runs on the same Mac mini it monitors; a power loss or logout alerts no one. A scheduled job on any other host (GH Actions or aa-hub) queries the newest `cre_source_index`/`cre_scrape_log` timestamp through read-only views and alerts if it stops advancing for ~24h.

5. **Decommission the still-loaded retired daily tier** (M/L). `cre_run_tier.sh` documents daily as RETIRED, yet CLAUDE.md says the plist is still loaded at 06:30. It burns a full collect against every broker daily (anti-bot exposure) and can hold the shared lock against monitor/enrich. Unload it, keep the plist as rollback.

6. **Scope the shared lock per-tier and test the reclaim race** (M/M). All four tiers serialize on one mkdir lock; one wedged tier blocks the rest for up to 18h with no auto-kill. Split into per-tier locks where write paths do not overlap, and add a concurrency test (two invocations, dead-PID reclaim) for the nontrivial reclaim logic.

7. **Wire the pytest/TS suites into CI** (H/L). Zero of the 30 GH workflow files reference `cre_collector`; the 1,400-case no-network suite runs only manually. Add a workflow scoped to `scripts/firecrawl-ops/cre_collector/**` running pytest plus the TS unit tests on every push/PR touching that path.

8. **Decide and finish GetCREdata's scheduling lane** (H/M). The GH cron was demoted 2026-07-08; the aa-hub job manifest is `enabled=false`, exits 78, has no secrets profile, and points at a wrong host path. Restore GH Actions cron or finish the hub lane; the half-migrated state means no refresh at all.

9. **Harden credential-file resolution** (M/L). `load_db_url` silently falls through to hardcoded `~/Documents/GitHub/...` .env.local candidates when `CRE_ENV_FILE` is unset, so a misrendered plist can run against a stale credential file instead of failing. Make the fallback loud on every real ingest, add a pytest asserting rendered plists carry `CRE_ENV_FILE`, document one source of truth plus rotation cadence.

10. **Backup/restore runbook for `credeals`** (H/M). No scheduled backup script or restore runbook exists anywhere in the ops tree; archive tables protect against routine soft-deletes, not a bad migration or credential compromise. Verify Supabase PITR settings, write the restore procedure, and run one test restore into a branch project.

11. **Fix the stale launchd/CLAUDE.md drift landmine** (M/L). The module doc agents are told to read before touching launchd still describes the pre-cutover 2026-06-15 state, contradicting the parent doc. Replace the stale banner with a pointer to `cre_status.sh` output and the parent's dated banner.

12. **Detect drift between installed plists and templates** (L/L). Nothing verifies the loaded plist matches a fresh render of the checked-in template. Teach `cre_status.sh` to re-render in memory and WARN on mismatch.

## B. Data quality and coverage

13. **Listing-to-CBSA market-metrics join for EQUIRE** (H/L). `cre_listings.cbsa_code` exists (sql/012) and is surfaced on the active views; `cbsa_market_data` lives in the same project keyed by the same code; the fork references it nowhere. One read-only view (`v_cre_listing_market_context`) gives every deal card live vacancy/cap-rate/absorption context. The single highest-leverage product item found.

14. **Fix `v_cre_market_summary` grouping by raw city/state text** (H/L). The view groups by free-text city, fragmenting metros and diluting the median stats the MarketStrategist agent consumes, even though `cbsa_code` is on the base table. Add a CBSA-grouped variant so both systems cut markets by the same canonical key.

15. **Cross-broker duplicate detection** (H/M). Dedup today is strictly (brokerage_id, external_id); a property co-listed by two brokerages counts twice in every aggregate. Nightly observe-only `cre_listing_duplicate_groups` batch matching normalized address + zip + size tolerance; flag, never merge, consistent with the additive design discipline.

16. **Reconcile the `om_facts` unique key before either OM pipeline writes again** (H/L). 4-column index in sql/013 vs GetCREdata's 5-column conflict target including `parser_version` (which also contradicts GetCREdata's own README). One additive widening migration, existence-guarded, staged for approval; gate both writers on it.

17. **Verify live om_facts state, then retire or redirect the fork's `om_parse.py`** (M/L). If GetCREdata's extractor already populated ~398k facts and solved the anti-bot fetch problem, further investment in the fork's gated parser duplicates shipped work. One read-only count settles it.

18. **Wire `cre_validate.py` into the daily pipeline observe-only** (M/L). 364 lines of post-ingest quality checks currently run only when invoked manually. Mirror the `cre_gate.py` pattern: emit a JSON verdict as a pipeline step, never block by default, surface in `cre_status.sh`.

19. **Guard the 50km nearest-centroid geo fallback** (M/L). Pure distance fallback can assign the wrong CBSA in dense multi-metro corridors (NYC/NJ, Bay Area, DC/Baltimore). Cheap fix: reject or flag latlng-derived assignments where the crosswalk state differs from the listing's own state field. Protects idea 13's join accuracy.

20. **One ZIP/CBSA crosswalk source of truth** (M/M). The fork ships a static 33,791-row point-in-time CSV; GetCREdata downloads Census delineations live plus hand-maintained dicts; they can drift, and the join in idea 13 depends on agreement. Promote `credeals.cre_zip_cbsa_crosswalk` to canonical (GetCREdata already reads it in `cmbs_edgar.py`), define its refresh cadence, and retire the parallel implementation; or at minimum add a periodic diff alarm.

21. **Per-listing freshness/staleness signal for consumers** (H/L). No view exposes days-since-last-confirmed; with the weekly mark-missing sweep failing, a 90-day-stale listing looks identical to a fresh one on the board. Derive `staleness_days`/confidence tier from `scraped_at` + `cre_source_index` and surface it on the consumer views. Pairs with the strategic `last_seen_at` model (idea 30).

22. **Ephemeral-Postgres integration test for `cre_ingest.py`** (M/M). The 2,180-line SQL generator is tested only via string assertions on generated SQL. A CI-only docker Postgres loaded from `sql/000_run_all.sql`, with one real non-dry-run ingest, verifies COALESCE-keep, mark-missing gating, and circuit-breaker semantics against actual constraints (NULLS NOT DISTINCT, CHECKs).

23. **Cross-repo schema-evolution contract on shared tables** (M/L). GetCREdata reads/writes tables this fork owns with no ownership note anywhere; a rename would break it silently. Minimum: an "external readers" comment block in each shared table's DDL naming known consumers. Full fix is the ownership manifest (idea 25).

24. **Provenance tags on price/status history rows** (L/L). Monitor-detected changes, enrich-tier corrections, and full-collect re-scrapes all collapse into identical history rows. Tag rows with the originating run mode for future audits.

## C. Strategic and architecture

25. **`credeals` schema-ownership contract and one migration home** (H/M). The root fix for the collision: a CODEOWNERS-style manifest of every table/view, sole writer, and migration owner; designate the fork's `sql/` (14 idempotent migrations) as the single migration path both repos apply from, retiring GetCREdata's ad hoc REST/apply_migration path.

26. **Unified `om_facts` data contract** (H/L). Strategic twin of idea 16: pick the canonical key (recommend 5-column, parser-version-inclusive so parser generations coexist), ship one widening migration, gate both writers on the index existing.

27. **One geo/CBSA authority backing listings and market data** (H/M). Strategic twin of idea 20.

28. **One governed MCP surface joining listings and market context** (H/M). Agent tools are split across the EQUIRE deal server and the market/research server with overlapping query paths; no single call walks listing to CBSA to market metrics to comps. Spec `get_listing_with_market_context(listing_id)` backed by the idea-13 join; inventory and dedupe the overlapping tools while at it.

29. **Implement the documented cloud/Mac-mini split** (H/H). The 2026-06-14 decision doc already recommends it: containerize the collector, keep the DB on Supabase, test datacenter-IP block rates against CBRE/Colliers before any move (residential IP is quietly load-bearing today). First step is the Dockerfile plus an explicit env contract and a staging collect against a Supabase branch.

30. **`last_seen_at` freshness model decoupled from destructive sweeps** (H/M). Status activation is opt-in-off by design, so trust currently depends on the fragile mark-missing lifecycle. A `last_seen_at` stamped by every monitor/collect observation plus a derived freshness tier in the views lets EQUIRE filter to "verified within N days" without any soft-delete risk.

31. **Self-healing Tier-B enrichment queue** (H/M). The queue worker is exactly the tier failing rc:1, and a poison URL can stall it silently. Add `attempts`, `next_attempt_at`, `dead_letter` columns (additive migration in the 010 style) and backoff/dead-letter logic in `cre_enrich.py`. This is the substrate for all future agentic enrichment.

32. **Anti-bot fetching as a first-class shared service** (H/M). Stealth-proxy needs, the Savills sale cap, unreachable OM PDFs, and GetCREdata's cloud-API fallback are four symptoms of one unowned problem. Promote per-source fetch policy into the existing `cre_brokerages.scrape_config` jsonb as a policy registry, wire a residential proxy pool behind self-hosted Firecrawl, route both repos' fetches (listings and PDFs) through it.

33. **Per-run scrape and LLM cost accounting, then soft spend caps** (M/L). Model-profile infrastructure exists but nothing measures pages scraped or tokens burned per run. Log one cost line per run to `credeals.cre_run_cost`; later add a soft cap that degrades to the budget profile and defers non-critical enrichment, mirroring the coverage gate's auto-downgrade pattern.

34. **Lift the collector out of the firecrawl fork** (M/H). The main plan's Phase 2; see FINAL_PLAN.md. Biweekly upstream syncs touch 50-180 files guarded by a hand-maintained allowlist, over ~300k lines of tracked reference data, for a system whose only hard fork dependency is convenience scripts.

35. **Typed data contracts and source-registry parity tests** (M/M). The collector calls Firecrawl through `as any` casts, and adding a source requires synchronized edits in 4+ places (TS SOURCE_KEYS, Python SOURCE_TO_BROKERAGE, enrich fold prefixes, SQL seeds), which the fact-check confirmed are parallel duplicated definitions. Generate TS types from the `credeals` schema and add a parity test that fails when any source key lacks its counterparts.

36. **Retire the legacy CRE trees; declare `sql/` the sole schema authority** (M/L). `cre_scrapers/` (~2,000 lines) and `cre_pipeline.py` (432 lines) are unscheduled, untested, and carry their own REST upsert plus an ad hoc apply-schema path around the safety-gated production ingestor; a well-meaning agent could resurrect them. Timestamped backup, delete, and one line in CLAUDE.md naming `sql/` as the single migration source of truth.

37. **LLM-vision extraction as a resilience fallback for capped sources** (M/H). Savills sale is structurally capped and CBRE rides one stealth-proxied API on one residential IP, both with no secondary path. GetCREdata's qpdf + AI Gateway pattern (which cracked the cap-rate PDFs) suggests a rendered-page vision-extraction fallback that sidesteps the anti-bot surface entirely. Prototype on Savills before deciding.

---

### Fable additions (not from the workflow lenses)

38. **Sunset one of the two OM extraction stacks explicitly** (H/L). The workflow flagged verifying and reconciling; go one step further and make a written decision: GetCREdata's `documents/` becomes THE OM pipeline (it shipped, at scale), the fork's `om_parse.py`/`om_url_resolver.py` get an explicit deprecation header or deletion in Phase 3, and the enrichment-queue worker feeds new brochure URLs to the surviving stack. Two dormant parallel implementations are how the key mismatch happened in the first place.

39. **A single dated status banner, generated not hand-written** (M/L). Both repos' worst drift (om_facts "EMPTY", launchd tier state, step counts 25/26/30) is hand-maintained status prose inside CLAUDE.md files. Generate the status block from machine sources (`cre_status.sh --json`, run markers, a read-only DB probe) into one included file per repo, so status claims cannot silently age.

40. **Name the platform once the extraction lands** (L/L). After Phase 2 there will be three surfaces (fork-infra, cre-listings, GetCREdata) plus the EQUIRE consumers. A one-page platform README in the context repo (`products/equire/`) mapping repo to responsibility to schema slice keeps future agents and collaborators from re-deriving this review.
