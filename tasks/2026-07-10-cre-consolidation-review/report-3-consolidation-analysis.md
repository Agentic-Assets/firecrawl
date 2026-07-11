---
title: "CRE Systems Consolidation Decision: Should the Two CRE Systems Live in One Repo?"
date: 2026-07-10
generated-by: "Workflow orchestration with multi-model review. Overlap survey and consolidation analysis passes (Sonnet and Opus). Fact-check verification pass (Opus). All code claims below carry file:line citations that survived the verification pass; documentation-only assertions are flagged as such."
report-id: report-3-consolidation-analysis
---

# CRE Systems Consolidation Decision

> **Historical review evidence.** The architectural separation conclusion remains
> useful, but current ownership is object-level rather than one universal
> migration home. Use the governed ownership contract and current operator
> runbook for decisions.

## Executive summary

**Direct answer: no, the two CRE systems do not need to live in one repo, and a full merge is the wrong move.**

The recommended path is to **extract the listing system into its own repository (a new `cre-listings` repo), keep GetCREdata separate, and add a lightweight shared-contract layer that assigns single ownership of the `credeals` schema, the ZIP-to-CBSA crosswalk, and the consumer views.** This scored highest of the five options considered (8/10) at medium effort and medium risk. It gets you separation and consolidation, each where it belongs.

Three facts drive that conclusion:

1. **The data is already consolidated. Repo separation is not data separation.** Both systems write the same Supabase project `fhqycqubkkrdgzswccwd`, schema `credeals` (`scripts/firecrawl-ops/sql/014_cre_geo_crosswalk.sql:4`; `GetCREdata/SUPABASE_DATA_MAP.md:3`). A single repo would not consolidate anything that is not already sharing a database. The schema contract, not the repo count, is what matters.

2. **The real overlap is small and surgical, so it is a contract problem, not a merge justification.** The only genuine code-level overlap is one shared table (`cre_listing_om_facts`) and a duplicated ZIP-to-CBSA crosswalk. The two systems' CBRE paths are disjoint (one scrapes the listings API, the other extracts the published cap-rate survey PDF). Merging two codebases to reconcile one table and one lookup file is disproportionate.

3. **There is a live-integrity seam that needs attention regardless of the repo decision.** GetCREdata's document pipeline writes into the listing system's `cre_listing_om_facts` table using a 5-column conflict key (`GetCREdata/documents/writer.py:29`) that does not match the 4-column unique index the listing system's own migration actually creates (`scripts/firecrawl-ops/sql/013_cre_listing_om_facts.sql:69`). Separately, GetCREdata's data map records 398,040 rows in that table while the listing system's own docs still describe it as empty. This contradiction is documentation-versus-documentation (not verified against the live database) and should be checked with a single read-only query before anyone enables the listing system's gated OM parser.

**What NOT to do:** do not full-merge the codebases (different languages, deployment surfaces, and product SLAs), do not merge their CI/test suites, and do not fragment the Supabase project (it is already single).

**Immediate, low-risk first step (no repo moves):** write down the schema-ownership contract, reconcile the `om_facts` index mismatch on paper, correct the stale docs, and stage (do not apply) one index-alignment migration for founder approval. Everything downstream of that is phased, reversible, and gated.

---

## How the two systems relate

Two systems, unaware of each other in code, already coupled through one shared database.

- **System A (the listing system):** the firecrawl fork's `scripts/firecrawl-ops/` CRE broker-listing ingestion for EQUIRE. A TypeScript collector (`cre_collector/collect.ts`, broker sources under `sources/<broker>.ts`) produces JSON that a Python ingestor (`cre_ingest.py`) upserts via `psql`. It scrapes broker sites through a local self-hosted Firecrawl at `localhost:3002` (`lib/config.ts:4`, `lib/scrape.ts:8`), runs on the Mac mini via `launchd`, and feeds EQUIRE deal-intelligence agents through service-role consumer views.
- **System B (GetCREdata):** a separate market-data pipeline. A 30-step Python orchestrator (`GetCREdata/run.py:92`, `TOTAL_STEPS=30`) aggregates 25-plus federal and public economic sources plus CMBS, REIT, and the CBRE cap-rate survey at the CBSA level, writing through the `supabase-py` REST client. It powers a market-analytics chatbot that reads precomputed aggregate views.

### Overlap map

| Axis | System A (listings) | System B (GetCREdata) | Relationship |
|---|---|---|---|
| **Storage** | Supabase `fhqycqubkkrdgzswccwd`, schema `credeals` (`sql/014_cre_geo_crosswalk.sql:4`) | Same project and schema (`SUPABASE_DATA_MAP.md:3`; writes `credeals` via `cmbs_edgar.py:264`) | **Fully shared.** Data is already consolidated. |
| **Table ownership** | Creates `cre_brokerages`, `cre_listings`, contacts/documents/images/media/links, `cre_listing_om_facts`, `cre_scrape_jobs/log`, `cre_source_index`, `cre_enrichment_queue`, `cre_zip_cbsa_crosswalk` (`sql/001`-`014`) | Creates a disjoint set: `cbsas`, `cbsa_market_data`, `national_macro`, CMBS, REIT, and cap-rate tables | **Mostly disjoint.** B creates its own tables and does not duplicate A's schema. |
| **Shared table (write)** | Owns and created `cre_listing_om_facts` (`sql/013_cre_listing_om_facts.sql:44`); its own OM parser is gated | Writes into `cre_listing_om_facts` (`documents/writer.py:27`, `:187`) | **Cross-write into A's table.** This is the highest-risk seam (see below). |
| **Shared table (read)** | Owns `cre_listings` and `cre_zip_cbsa_crosswalk` | `cre_market_index` view SELECTs from `credeals.cre_listings` (`sql/cre_cmbs_schema.sql:140,168`); `cmbs_edgar.py:251,264` reads `cre_zip_cbsa_crosswalk` | **B reads A's live tables** to blend asking-rent and geo data into the chatbot aggregate. |
| **Geo (ZIP/county to CBSA)** | Static committed CSV, ZIP-level, 33,791 rows with nearest-centroid fallback (`cre_geo_backfill.py:5-15`; `cre_collector/data/zip_cbsa_crosswalk.csv`) | Live Census delineation Excel download, county-FIPS-level, plus hand-maintained mapping dicts (`fetchers/county_cbsa_mapping.py:1-8`) | **Duplicated engineering.** Two independent crosswalk implementations, no shared library. |
| **CBRE** | Scrapes CBRE's internal listings JSON API for individual for-sale properties (`prometheus/CLAUDE.md:19-24`, `listings-api/propertylistings/query`) | LLM-extracts CBRE's published cap-rate survey PDF into a benchmark table (`fetchers/cbre_cap_rate_survey.py:1-4`) | **Disjoint.** Same vendor, different data, different method. Not overlap. |
| **DB access** | Shells to `psql` with `POSTGRES_URL_NON_POOLING` (`cre_ingest.py:1879-1880`) | `supabase-py` REST client with `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (`output/supabase_export.py`; `.env.example:8-9`) | **Divergent patterns** against the same project: different failure modes, connection limits, and pooling awareness. |
| **Firecrawl runtime** | Local self-hosted stealth instance at `localhost:3002` | Cloud hosted API `api.firecrawl.dev` for PDF fallback (`documents/pilot_extract.py:65`) | **Duplicated capability** for the same anti-bot PDF problem. |
| **Operations** | Mac mini `launchd` tiers (`launchd/ai.agentic.cre-daily.plist:15`) | GitHub Actions cron demoted to `workflow_dispatch`-only 2026-07-08 (`.github/workflows/run-pipeline.yml:1`); aa-hub stub disabled, pointed at a different host path `/Users/cayman-mac-mini/...` | **Different runtimes and hosts**, no shared scheduling or lock. |
| **Consumers** | EQUIRE deal-intelligence agents (listing sourcing) via service-role views/RPC | A market-analytics chatbot reading aggregate views; the chatbot consumer appears to live in a third repo (`agentic-assets-app`, existence only, not opened) | **Different products, different SLAs** (live deal board vs batch analytics). |
| **Cross-references** | No mention of GetCREdata anywhere (repo-wide grep: zero hits) | No mention of the fork's listing system; "firecrawl" hits only the unrelated cloud API product | **Zero mutual awareness in code or docs**, despite active cross-writes. |

### The highest-risk seam, stated precisely

GetCREdata's `documents/writer.py` upserts into `credeals.cre_listing_om_facts` (`writer.py:27`, `:187`), the table System A created in `sql/013_cre_listing_om_facts.sql:44` and which System A's own gated `om_parse.py` is meant to populate.

Two concrete problems:

1. **Conflict-key mismatch, and it is internally inconsistent inside GetCREdata too.** `writer.py:29` uses a 5-column `on_conflict` including `parser_version`. System A's checked-in DDL creates a 4-column unique index `NULLS NOT DISTINCT` (`sql/013:69`). GetCREdata's own `documents/README.md:91` describes the 4-column index, so `writer.py` contradicts its own repo's documentation. A 5-column `on_conflict` errors against a 4-column index unless a matching index exists, and no widening migration for that index exists in either repo's checked-in `sql/`.

2. **A documentation contradiction on the table's state.** GetCREdata's `SUPABASE_DATA_MAP.md:679` records 398,040 rows in `cre_listing_om_facts` as of 2026-06-24. System A's `CLAUDE.md` (Track-2 section) states the table "remains EMPTY" as of 2026-06-15. **This is a documents-versus-documents contradiction and has not been verified against the live database.** Read at face value, with GetCREdata's dated later, it indicates System B has been populating a table System A still treats as gated and empty. No single document in either repo enumerates both systems' table ownership, so an engineer reading only System A's docs would not know an external pipeline writes this table.

The practical consequence: if System A ever runs its gated `om_parse.py --apply`, its 4-column key could collide with or coexist incorrectly against rows GetCREdata wrote keyed on 5 columns. A one-line read-only check of the live row count and the actual index definition should precede any activation.

---

## Decision drivers

1. **Data is already consolidated.** Both repos write `fhqycqubkkrdgzswccwd`, schema `credeals`. Repo separation is not data separation, and the schema contract matters more than repo count.
2. **Deployment surfaces differ materially.** System A is host-locked to the Mac mini plus a local Firecrawl Docker stack at `localhost:3002`; System B is pure Python and runnable anywhere, headed for an aa-hub lane.
3. **System A's only hard coupling to the fork is convenience tooling.** The health-check and setup scripts resolve `FC_DIR` and call the fork's `firecrawl_healthcheck.sh` plus `docker compose up` (`cre_setup.sh:28,95-101`; `cre_status.sh:36,258-268`). The scrape and ingest logic itself only reads `FIRECRAWL_API_URL`/`FIRECRAWL_API_KEY` (`config.ts:4`, `scrape.ts:8`), so it is already portable to any endpoint. Extraction is mostly tooling and docs, not a rewrite.
4. **Fork-sync pollution is real and recurring.** Upstream syncs land every one to two weeks with large diffs (the most recent, `fdae874e4`, touched 176 files, +11,398/-4,780), governed by a hand-maintained protected-path allowlist (`sync_upstream_main.sh:41-47`). The fork also carries roughly 300,000 lines of tracked reference data (`prometheus/data.json`, 300,165 lines). Fork ops code is about 14% of tracked files (343 of 2,479).
5. **Real cross-repo overlap is small and surgical.** One shared table (`cre_listing_om_facts`) and a duplicated ZIP-to-CBSA crosswalk. CBRE paths are disjoint. This is a contract problem, not a merge justification.
6. **There is a live data-integrity seam.** Per the documented state, System B has written into System A's `cre_listing_om_facts` with a conflict key that mismatches System A's unique index, while System A's docs still call the table empty (verify against the live DB before acting).
7. **Different product SLAs.** Listings feed the live EQUIRE deal board (transactional); market data feeds an analytics chatbot (batch). Merging their CI and tests would break isolation: a market-data change could fail the live board's checks.
8. **Migration risk to a live, partly-degraded pipeline.** Per the docs as of 2026-07-05 (not verified against the running system), System A's monitor tier last ran clean (rc:0) while enrich, daily, and weekly last failed (rc:1) (`START_HERE.md`; `cre_collector/CLAUDE.md:247-250`). Combined with the founder hard rules (never push `main`, no deploy without approval), this demands a phased, reversible, stabilize-first approach.

---

## Options considered

### Comparison table

| # | Option | Effort | Risk | Score | One-line verdict |
|---|---|---|---|---|---|
| 1 | Full merge (one CRE-data monorepo) | High | High | 3 | Couples two different SLAs and runtimes to reconcile one table. Disproportionate. |
| 2 | Extract-and-merge into GetCREdata | High | Medium | 5 | Consolidates ownership but mixes a host-locked polyglot scraper into a batch pipeline and inherits B's debt. |
| 3 | **Extract-to-new (`cre-listings` repo) + shared contract** | **Medium** | **Medium** | **8** | **Clean separation by language, surface, and SLA; contract fixes the real seam. Recommended.** |
| 4 | Status quo + docs/contracts | Low | Low | 5 | Zero migration risk but leaves A rotting in the fork and the live bug unaddressed. |
| 5 | Shared-contract only (repos stay put) | Low | Low | 7 | Attacks the real seam with no code move, but does not remove fork pollution. Best as a companion to extraction. |

### Per-option detail

**Option 1: Full merge (one CRE-data monorepo).** Combine both codebases into one new monorepo.
- *Pros:* single home for the shared schema, crosswalk, and CBRE logic; forces the two systems to become aware of each other; removes fork-sync pollution for A.
- *Cons:* couples a transactional live-board SLA with a batch analytics SLA (a market-data change can break the live board's CI); forces host-locked A and anywhere-runnable B into one runtime story; doubles agent context (A already carries 37 CRE markdown docs, B carries thousands of lines of root markdown); big-bang migration against a live, partly-failing `launchd` pipeline; the real overlap (one table plus one crosswalk) is too small to justify a full code merge.
- *Effort:* high. *Risk:* high. *Score:* 3.

**Option 2: Extract-and-merge into GetCREdata.** Lift the listing system out of the fork into GetCREdata (renamed); the fork becomes pure scraping infra.
- *Pros:* removes A from biweekly fork-sync pollution and git-history bloat; consolidates both CRE systems and shared `credeals` ownership in one repo; B is already runnable anywhere and A becomes portable via `FIRECRAWL_API_URL`; resolves the `om_facts` and crosswalk overlap inside one codebase.
- *Cons:* merges a pure-Python batch pipeline with a TypeScript-plus-Python-plus-bash host-locked scraper (polyglot, mixed deployment cadence); doubles agent context and worsens doc sprawl; live-pipeline migration must re-point `FC_DIR`, host setup/status scripts, env paths, and `launchd` plists; inherits GetCREdata's own quality debt (a reported backlog of critical audit findings, not verified in this pass) into the listing repo's blast radius.
- *Effort:* high. *Risk:* medium. *Score:* 5.

**Option 3: Extract-to-new (`cre-listings` repo) + shared contract. RECOMMENDED.** Lift the listing system into its own repo, keep GetCREdata separate, make the fork infra-only, and designate a single owner for the `credeals` schema, crosswalk, and consumer views.
- *Pros:* clean separation by language, deployment surface, and product SLA (live deal board vs batch chatbot); removes A's biweekly fork-sync pollution and gives it a focused `CLAUDE.md` and clean git history; A's only hard fork coupling is convenience and health-check scripts (`FC_DIR` plus Docker compose), so extraction is mostly tooling and docs, not a rewrite; the shared-contract layer fixes the actual highest-risk seam (the `om_facts` key mismatch, the duplicated crosswalk, and migration ownership) without a code merge; test and CI isolation preserved per product.
- *Cons:* three repos to coordinate (fork-infra, `cre-listings`, GetCREdata), more surfaces for a two-founder shop; requires a disciplined schema-ownership contract to prevent drift; A still needs a running Firecrawl endpoint somewhere for collection.
- *Effort:* medium. *Risk:* medium. *Score:* 8.

**Option 4: Status quo + docs/contracts.** Leave both systems where they are; improve cross-repo docs and interface contracts only.
- *Pros:* zero migration risk to the live `launchd` pipeline; lowest effort; keeps the one working tier (monitor) untouched.
- *Cons:* leaves A rotting inside the fork with biweekly 50-to-180-file sync merges and a manually maintained protected-path allowlist; leaves the live data-integrity issue unaddressed (B writes `cre_listing_om_facts` with a 5-column conflict key against A's 4-column index, and A's docs still call the table empty); two uncoordinated pipelines keep writing the same `credeals` schema with no shared lock.
- *Effort:* low. *Risk:* low. *Score:* 5.

**Option 5: Shared-contract only (repos stay put).** Keep the repos separate but consolidate `credeals` schema ownership, the geo crosswalks, and consumer-view ownership in one designated place.
- *Pros:* directly attacks the highest-risk seam (shared writes, `om_facts` key mismatch, duplicate crosswalks) with no big-bang code move; low risk to the live pipeline; respects that the two deployment surfaces differ.
- *Cons:* does not remove A's fork-sync pollution or git-history bloat; relies on governance discipline rather than structure; strongest as a companion to extraction, weaker as a standalone end state.
- *Effort:* low. *Risk:* low. *Score:* 7.

---

## Recommendation and rationale

**Recommendation: Option 3, extract the listing system to its own `cre-listings` repo, and pair it with a shared-contract layer that puts `credeals` schema, crosswalk, and consumer-view ownership in one place.**

The reasoning, stated for a founder who wants the tradeoff and not the ceremony:

- **A full merge solves a problem you do not have and creates ones you do not want.** The data is already consolidated because both systems write the same Supabase project and schema. Putting them in one repo does not consolidate data; it consolidates CI, deployment, and agent context across two systems that have different languages, different runtimes, and different SLAs. A change to the batch market-data pipeline should never be able to break the live EQUIRE deal board's checks. Option 1 makes that possible.

- **What actually hurts are two separate things, and one mega-repo would not fix either.** First, the listing system is rotting inside the firecrawl fork: biweekly upstream syncs touch 50 to 180 files, guarded by a hand-maintained protected-path allowlist, on top of roughly 300,000 lines of tracked reference data. Extraction, not merger, removes that. Second, the two pipelines share a write surface with a real bug: GetCREdata's document pipeline writes `cre_listing_om_facts` with a conflict key that does not match System A's unique index, while System A's docs still call the table empty. A shared contract, not a merger, fixes that.

- **The overlap that would tempt a merge is small and surgical.** One shared table and one duplicated ZIP-to-CBSA crosswalk. The CBRE paths look like overlap but are not: A scrapes the listings API for individual properties, B extracts the published cap-rate survey PDF into a benchmark table. Small, surgical overlap is a contract problem.

- **Extraction is cheap because the coupling is shallow.** System A's scrape and ingest logic already points at any Firecrawl endpoint via `FIRECRAWL_API_URL`. The only hard tie to the fork is convenience and health-check tooling (`FC_DIR` plus Docker compose). So this is mostly moving files, re-pointing scripts, and writing docs, not rewriting a scraper.

Option 3 gives you separation where the systems differ (language, runtime, SLA, agent context) and consolidation where they overlap (one schema owner, one crosswalk owner, one place that reconciles the `om_facts` contract). Option 5 (shared contract only) is the right first phase and a reasonable fallback if extraction is deferred, but as a standalone end state it leaves the fork-sync tax in place.

---

## Phased migration sketch

Every phase is independently shippable and rollback-safe. Nothing is pushed to `main`, no DB DDL is applied, and no `launchd` or aa-hub cutover happens without explicit founder approval. Feature branches only, per the hard rules.

**Phase 0: Contract first, no repo moves (lowest risk).**
Designate a single owner of the `credeals` schema and migrations (System A's `sql/` is the source of truth). Assign System B as the documented owner of CMBS, REIT, cap-rate, and `om_facts` population; assign System A as owner of `cre_listings`, the crosswalk, and DDL. Reconcile the `om_facts` unique index against B's 5-column conflict key (the live seam) and correct System A's stale docs about that table's state. This is a documentation and contract pass. The one index-alignment migration is drafted and staged for founder approval, not applied. **Precondition to acting on the index:** one read-only query to confirm the live row count and the actual unique-index definition on `cre_listing_om_facts`.

**Phase 1: Stabilize in place.**
Root-cause and fix System A's failing enrich, daily, and weekly `launchd` tiers (documented rc:1) before moving anything. Do not extract a broken pipeline. Verify the fix against the running tiers, not just docs.

**Phase 2: Extract System A.**
Create the `cre-listings` repo on a branch via `git filter` to preserve `scripts/firecrawl-ops/` history. Make `FC_DIR` and the Firecrawl endpoint fully configurable (already portable via `FIRECRAWL_API_URL`). Keep `launchd` on the Mac mini. Run the extracted copy in parallel with the fork copy until N clean runs match, then flip the `launchd` plists. Reversible: retain the fork copy until the extracted copy is verified.

**Phase 3: Fork cleanup.**
Drop the legacy `cre_scrapers/`, `cre_pipeline.py`, and the `prometheus` data bloat from the live path (all confirmed stale, not scheduled, and not referenced by tests). Trim the protected-path allowlist. Leave a deprecation pointer to the new repo.

**Phase 4: Unrelated, already gated.**
Activate GetCREdata's aa-hub lane separately (its stub is disabled and currently points at a different host path, so it needs its `cwd`/host assumption corrected before reactivation). This is not part of the extraction and should not block it.

---

## What NOT to consolidate, and why

- **Do not merge the two codebases.** Different languages (TypeScript-plus-Python-plus-bash vs pure Python), different deployment surfaces (host-locked Mac mini plus local Firecrawl Docker vs anywhere-runnable), and different product SLAs (live deal board vs batch chatbot). The overlap is one table and one crosswalk, which is too small to justify coupling everything else.

- **Do not merge CI or test suites.** Test and CI isolation per product is a feature, not a defect. Merging them lets a batch market-data change fail the live board's checks. Keep them separate.

- **Do not consolidate the CBRE integrations.** They only look like overlap. System A scrapes CBRE's internal listings JSON API for individual for-sale properties (`prometheus/CLAUDE.md:19-24`); System B LLM-extracts CBRE's published cap-rate survey PDF into an aggregate benchmark table (`fetchers/cbre_cap_rate_survey.py:1-4`). Different data, different method, no shared code to gain.

- **Do not fold System B's economic-data fetchers into System A.** B's broad, mature coverage of 25-plus federal and public sources is exactly what A does not attempt to duplicate. That coverage belongs in B. Consolidating it into a listing repo adds surface for no benefit.

- **Do not fragment the Supabase project.** It is already a single project (`fhqycqubkkrdgzswccwd`, schema `credeals`) shared by both systems. This is the one place consolidation already exists de facto. Preserve it; assign clear per-table ownership through the contract instead of splitting the database.

- **Do not force one runtime story onto both.** A is host-locked to the Mac mini with a local stealth Firecrawl instance for anti-bot scraping; B runs pure Python and is headed for an aa-hub lane. Keep them separate. There is a real opportunity to route B's cloud-Firecrawl PDF fallback (`documents/pilot_extract.py:65`) through A's local stealth instance for the same anti-bot PDF problem, but that is a contract and integration opportunity, handled through a shared interface, not a reason to merge repos.

---

### Notes on evidence quality

Code-level claims above carry file:line citations that held up under a dedicated verification pass. Two categories are explicitly documentation-only and should be confirmed against the live system before they drive an irreversible action: (1) the `launchd` tier health (monitor rc:0, enrich/daily/weekly rc:1 as of 2026-07-05) is a documented status, not a verified live read; and (2) the 398,040-row count in `cre_listing_om_facts` versus the "remains EMPTY" claim is a contradiction between two documents, not a query against the database. Both repos show a pattern of docs drifting from code (for example, GetCREdata's `run.py` is 30 steps while its README says 25 and its `CLAUDE.md` says 26), so any status or live-count claim sourced from docs should be treated as provisional until verified read-only.
