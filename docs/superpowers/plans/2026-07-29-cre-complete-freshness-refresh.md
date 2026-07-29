# CRE Complete Freshness Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a current, complete, and auditable refresh of every supported CRE listing source, with fresh source observations, additive production writes, exact generation-based database readback, and an explicit accounting of unsupported legacy inventory.

**Architecture:** A checkpoint run assigns one immutable generation ID and start time to each source artifact. Strict sources bypass Firecrawl response caches, record inventory and admitted detail or authoritative-feed observation times, and fail closed on incomplete source universes or detail pages. Child behavior is explicit by source class: CBRE and Buildout replace collector-owned children, SRS/Hanley/Kidder preserve children, and other strict sources require current detail rather than preservation. Production ingest remains additive and is admitted only after artifact validation; readback proves the exact generation, canonical row count, inventory-only scope count, and freshness timestamps rather than treating artifact creation time as source observation time.

**Tech Stack:** TypeScript/tsx collector, local Firecrawl Docker API, Python 3 checkpoint/ingest/validation tools, PostgreSQL through `psql`, Node test runner, pytest, GitHub pull request, Linear.

## Global Constraints

- Work only on `fix/cre-refresh-freshness`; never push `main` and never merge without explicit Cayman approval.
- Run collection through the local Firecrawl/OrbStack stack, not GitHub Actions.
- Keep production ingest additive: never pass `--mark-missing`, `--activate-status`, or `--om-parse`.
- Firecrawl owns listing enumeration and listing-side freshness; GetCREdata owns market data and OM extraction.
- Never print or persist database credentials, API keys, cookies, or environment-file contents.
- Bind the checkpoint to a credential-free hash of its PostgreSQL host, port, and database; reject database-target drift before any resumed gate or write.
- Pass that expected target hash into every database child and verify it after URL loading but before access; reject ambiguous libpq target overrides and multi-host forms.
- Reject artifact, listing, and database observation timestamps more than five minutes in the future.
- Verify the four CRE launch-agent labels are not loaded while supervised refresh generations are active. Do not install or load them as a closeout action without separate named recovery approval.
- A source failure blocks that source's ingest. A failed aggregate gate blocks every pending write in that checkpoint.
- Unsupported legacy brokerages are reported as unsupported; they are not silently described as fresh.

---

## File Structure

- Create: `scripts/firecrawl-ops/cre_collector/lib/freshness.ts` — shared generation, strict-mode, and observation-time primitives.
- Modify: `scripts/firecrawl-ops/cre_collector/types.ts` — typed listing and run-level freshness provenance.
- Modify: `scripts/firecrawl-ops/cre_collector/collect.ts` — generation metadata and strict contract in artifacts.
- Modify: `scripts/firecrawl-ops/cre_collector/lib/scrape.ts` — explicit Firecrawl `maxAge` control.
- Modify: `scripts/firecrawl-ops/cre_collector/sources/*.ts` — source-specific completeness, cache bypass, detail validation, and provenance.
- Modify: `scripts/firecrawl-ops/cre_collector/cre_checkpoint_refresh.py` — strict source policy, generation age, aggregate admission, ingest orchestration, and readback.
- Modify: `scripts/firecrawl-ops/cre_collector/cre_ingest.py` — explicit strict-artifact admission and freshness persistence.
- Modify: `scripts/firecrawl-ops/cre_collector/cre_validate.py` — generation-based live database readback.
- Modify: `scripts/firecrawl-ops/cre_collector/tests/` — regression tests for every fail-closed contract.
- Modify: `scripts/firecrawl-ops/cre_collector/START_HERE.md` and `CLAUDE.md` — operator procedure and interpretation limits.
- Create: `tasks/2026-07-29-cre-complete-freshness-refresh/refresh-summary.md` — exact source-by-source live evidence and remaining exceptions.

## Task 1: Make strict freshness a runner-owned contract

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/cre_checkpoint_refresh.py`
- Modify: `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
- Test: `scripts/firecrawl-ops/cre_collector/tests/test_cre_checkpoint_refresh.py`
- Test: `scripts/firecrawl-ops/cre_collector/tests/test_cre_ingest_builders.py`

**Interfaces:**

- Consumes: source key, artifact `runMeta.freshness`, and checkpoint environment.
- Produces: `STRICT_FRESHNESS_SOURCE_KEYS`, `--require-strict-freshness`, and a validator that rejects missing or false strict metadata.
- Invariant: an artifact cannot opt itself out of the strict contract.

- [ ] **Step 1: Add regression tests**

Add tests that pass a JLL artifact with no `runMeta.freshness`, with `requireFreshDetails: false`, and with a mismatched `generationId`; assert checkpoint validation and direct strict ingest reject all three before any subprocess write.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_checkpoint_refresh.py tests/test_cre_ingest_builders.py -k "fresh or strict or generation"
```

Expected: the missing-metadata cases are accepted by the pre-fix implementation.

- [ ] **Step 3: Implement explicit strict admission**

Define the strict source set in `cre_ingest.py`, pass `--require-strict-freshness` on both dry-run and live ingest for those sources, and make the ingestor require `runMeta.freshness.requireFreshDetails is True`, a nonempty generation ID, a timezone-aware generation start, and matching per-listing provenance.

- [ ] **Step 4: Run focused tests**

Run the command from Step 2 and require zero failures.

## Task 2: Prove freshness at every source boundary

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/lib/scrape.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/jll.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/cushman-wakefield.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/colliers-main.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/buildout.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/newmark.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/savills.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/transwestern.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/marcus-millichap.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/nai-global.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/cbre.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/jll-investor.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/avison-young.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/matthews.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/srs.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/hanley.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/kidder-mathews.ts`
- Modify: matching files under `scripts/firecrawl-ops/cre_collector/tests/ts/sources/`

**Interfaces:**

- Consumes: `CRE_REQUIRE_FRESH_DETAILS=1`, `CRE_REFRESH_GENERATION`, and `CRE_REFRESH_STARTED_AT`.
- Produces: every canonical listing has `freshnessProvenance.generationId`, `inventoryObservedAt`, and the source-class-specific admitted detail or authoritative-feed provenance.
- Invariant: strict collection never labels a cached response or error shell as a fresh source observation.

- [ ] **Step 1: Add source-specific failure tests**

Cover Firecrawl cache bypass, missing provider totals, invalid page URLs, challenge/error shells, identity mismatches, stale generation caches, missing Algolia `nbHits`, malformed NAI office discovery, and detail fetch failures.

- [ ] **Step 2: Verify each new test fails for its intended reason**

Run each changed source test independently with:

```bash
cd scripts/firecrawl-ops/cre_collector
node --import tsx --test tests/ts/sources/{jll,cushman-wakefield,colliers-main,buildout,newmark,savills,transwestern,marcus-millichap,nai-global}.test.ts
```

- [ ] **Step 3: Implement minimal fail-closed behavior**

Pass `maxAge: 0` on strict Firecrawl calls; require provider totals and reconcile exact identities; validate detail page structure and identity before stamping `detailObservedAt`; apply the explicit authoritative-feed child contract instead of generic preservation; bind resumable caches to the current generation.

- [ ] **Step 4: Prove the complete TypeScript surface**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npm test
```

Expected: typecheck exits 0 and every TypeScript test passes.

## Task 2A: Close legacy adapter completeness gaps

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/sources/cbre.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/jll-investor.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/avison-young.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/matthews.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/srs.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/hanley.ts`
- Modify: `scripts/firecrawl-ops/cre_collector/sources/kidder-mathews.ts`
- Modify: matching files under `scripts/firecrawl-ops/cre_collector/tests/ts/sources/`

**Interfaces:**

- Consumes: provider pagination metadata, stable provider identities, direct source records, property-detail pages, and the shared refresh generation.
- Produces: either a strict complete-detail artifact or an explicitly scoped authoritative-inventory artifact with the correct source-specific replace-or-preserve child behavior.
- Invariant: no missing page, duplicate identity, parse-null page, detail failure, or cached fallback can silently become a complete current-source claim.

- [ ] **Step 1: Add adverse completeness tests**

Cover malformed later CBRE pages, JLL Investor detail failures before country classification, cached Avison details, Matthews parse-null pages, unstable SRS/Kidder totals and IDs, and Hanley fallback/cache or duplicate rows.

- [ ] **Step 2: Verify the tests fail**

Run each affected source test independently and require the failure message to identify the intended completeness gap.

- [ ] **Step 3: Implement source-specific admission**

Require stable provider totals and identities where exposed; bypass Firecrawl cache in strict mode; make every parse/detail failure truncate or fail the source; attach current-generation provenance. For authoritative inventory feeds, emit `detailScope: authoritative_inventory_feed`: CBRE and Buildout replace collector-owned children, while SRS, Hanley, and Kidder Mathews preserve existing children. Other strict sources require an admitted current detail observation and cannot use preservation after a detail failure.

- [ ] **Step 4: Keep public identity limits explicit**

CBRE Deal Flow and Colliers SalesTracker unlinked cards remain inventory-only source-index rows. They prove current provider-card inventory but never exact canonical identity or fresh child collections. Do not promote provisional cards into `cre_listings`.

- [ ] **Step 5: Run the full TypeScript proof**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npm test
```

Expected: all tests pass and every source admitted to strict policy has matching failure-path coverage.

## Task 3: Make production readback generation-exact

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/cre_validate.py`
- Modify: `scripts/firecrawl-ops/cre_collector/cre_checkpoint_refresh.py`
- Test: `scripts/firecrawl-ops/cre_collector/tests/test_cre_validate.py`
- Test: `scripts/firecrawl-ops/cre_collector/tests/test_cre_checkpoint_refresh.py`

**Interfaces:**

- Consumes: expected source generation ID, generation start, staged canonical count, and staged inventory-only count.
- Produces: `latest_generation_id`, `latest_generation_active`, minimum and maximum generation observation times, and exact scope readback.
- Invariant: pagination or sale/lease observation-time differences do not split one logical refresh into false batches.

- [ ] **Step 1: Add readback regression fixtures**

Create rows from one generation with different `inventoryObservedAt` values and assert they count as one generation; create a newer mismatched generation and assert validation fails.

- [ ] **Step 2: Verify old max-timestamp logic fails**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_validate.py tests/test_cre_checkpoint_refresh.py -k "generation or readback"
```

- [ ] **Step 3: Implement generation-based queries and comparisons**

Group active canonical rows by persisted `raw_data.freshnessProvenance.generationId`; compare the exact expected generation and count; require all observations to be at or after `generationStartedAt`. Keep inventory-only verification on the source-scope watermark and exact active count.

- [ ] **Step 4: Prove the full Python surface**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q
python3 -m py_compile cre_checkpoint_refresh.py cre_ingest.py cre_validate.py
```

Expected: every Python test passes and compilation exits 0.

## Task 4: Prevent stale-generation resume

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/cre_checkpoint_refresh.py`
- Test: `scripts/firecrawl-ops/cre_collector/tests/test_cre_checkpoint_refresh.py`

**Interfaces:**

- Consumes: manifest `started_at`, current UTC time, and `--max-resume-age-hours`.
- Produces: a default 24-hour maximum generation age and an actionable failure asking the operator to start a new run.
- Invariant: a resumed checkpoint cannot promote days-old observations as current.

- [ ] **Step 1: Add boundary tests**

Assert a 23-hour generation can resume, a generation exactly at the documented boundary is handled deterministically, and a 25-hour generation fails before collection or ingest.

- [ ] **Step 2: Implement timezone-aware age validation**

Parse the manifest start as an aware UTC datetime and reject negative, naive, malformed, or over-age generations.

- [ ] **Step 3: Run focused and full checkpoint tests**

Run:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 -m pytest -q tests/test_cre_checkpoint_refresh.py
```

## Task 5: Commit an independently reviewed safety batch

**Files:**

- Modify: all files listed above.

**Interfaces:**

- Produces: one clean feature-branch commit, a pushed exact SHA, an updated draft PR, and Linear evidence.

- [ ] **Step 1: Run repository hygiene checks**

```bash
git diff --check
git status --short
```

- [ ] **Step 2: Obtain independent review**

Require one source-completeness review and one ingest/readback review. Resolve every confirmed P1/P2 and rerun the affected focused tests.

- [ ] **Step 3: Commit and push**

```bash
git add docs/superpowers/plans/2026-07-29-cre-complete-freshness-refresh.md \
  scripts/firecrawl-ops/cre_collector
git commit -m "fix: prove CRE listing refresh freshness"
git push origin fix/cre-refresh-freshness
```

- [ ] **Step 4: Update durable tracking**

Add the exact SHA, test counts, review outcome, known source exceptions, and statement that no live data was written during code verification to draft PR #25 and AGENTIC-1229.

## Task 6: Run fresh checkpoint generations

**Files:**

- Runtime artifacts: `scripts/firecrawl-ops/cre_collector/out/checkpoint-refresh/2026-07-29T*/`
- Create: `tasks/2026-07-29-cre-complete-freshness-refresh/refresh-summary.md`

**Interfaces:**

- Consumes: clean pushed collector SHA, healthy local Firecrawl, free shared lock, safe database environment path, and the supported 20-source registry.
- Produces: source artifacts, validation reports, additive writes, and exact post-write readback.

- [ ] **Step 1: Reconfirm safety preflight**

Verify the feature worktree SHA, a clean tree, local Firecrawl health, no CRE launch-agent process, a free shared lock, database connectivity, and current source baselines without printing secrets.

- [ ] **Step 2: Run bounded source generations**

Run checkpoint groups sized so each generation remains below 24 hours. Each invocation must perform collection, artifact validation, dry-run ingest, aggregate coverage gates, additive live ingest, and post-write validation. A failed source receives a new clean generation after repair; do not resume a stale or semantically invalid artifact.

- [ ] **Step 3: Verify exact database readback**

For every source require:

- expected generation ID equals the database generation ID;
- active generation row count equals staged canonical count;
- scope-active inventory-only count equals staged inventory-only count;
- minimum observation time is at or after generation start;
- no identity, duplicate, detail, completeness, or aggregate gate failure.

- [ ] **Step 4: Compute current deltas**

Query listing events and source-index state over the refresh window. Report total active listings, current supported-source coverage, new listings, price changes, status changes, reappeared listings, disappeared candidates, preserved-detail exceptions, and unsupported active rows.

## Task 7: Final operational reconciliation

**Files:**

- Modify: `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- Modify: `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- Create: `tasks/2026-07-29-cre-complete-freshness-refresh/refresh-summary.md`

**Interfaces:**

- Produces: an operator-readable source matrix, exact proof links, limitations, rollback state, and next steps.

- [ ] **Step 1: Document the strict refresh contract**

Explain source observation versus artifact time, generation identity, `maxAge: 0`, child-collection preservation, additive ingest, 24-hour resume expiry, and exact database readback.

- [ ] **Step 2: Record every source outcome**

For all 20 supported source keys, record collection finish, raw/staged/inventory-only counts, new/changed counts, strict or non-strict status, readback result, and any scoped exception.

- [ ] **Step 3: Reconcile adjacent systems**

State explicitly that GetCREdata market/OM freshness is a separate producer gate. If its documented runtime remains unavailable, record the exact preflight blocker rather than claiming market data is fresh.

- [ ] **Step 4: Restore recurring operations**

After no supervised collector process remains and all source generations are terminal, verify the four CRE launch-agent labels are still not loaded and pause or remove the temporary checkpoint heartbeat. Do not install or load recurring agents without separate named recovery approval.

- [ ] **Step 5: Update PR and Linear**

Add final refresh evidence to PR #25 and AGENTIC-1229. Keep the PR draft until review-ready; do not merge or mark Linear Done without the required founder gate.
