export const meta = {
  name: 'cre-enrichment-worker',
  description: 'Build the Tier-B enrichment-queue worker + cadence restructure (monitor 2x/day, full re-scrape weekly, retire daily), test, adversarially review, document. Live launchd cutover only with args.cutover=true; never loads the weekly soft-delete tier.',
  whenToUse: 'Run after the design in ENRICHMENT_WORKER_DESIGN_2026-06-15.md is approved. Plain run does all code+test+review+docs and renders plists but does NOT touch the live scheduler. Pass {cutover:true} to also apply the additive SQL and load the safe tiers.',
  phases: [
    { title: 'Spec', detail: 'opus: re-derive authoritative contracts, write IMPL_SPEC.md' },
    { title: 'Build', detail: 'parallel disjoint groups: collect.ts targeted detail + enrichers; cre_enrich.py + SQL 010; launchd cadence' },
    { title: 'Test', detail: 'write + run pytest, TS unit, syntax checks, tiny e2e dry run' },
    { title: 'Review', detail: 'opus adversarial: soft-delete invariant, race safety, dead-letter, partial-ingest safety; bounded fix loop' },
    { title: 'Docs', detail: 'update CLAUDE.mds / START_HERE / design status; emit cutover runbook' },
    { title: 'Cutover', detail: 'GATED on args.cutover: apply SQL 010, reload monitor 2x/day, load enrich, unload daily; NEVER weekly' },
  ],
}

const ROOT = '/Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector'
const SQLDIR = '/Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/sql'
const DESIGN = `${ROOT}/ENRICHMENT_WORKER_DESIGN_2026-06-15.md`

const CONTRACTS = `
AUTHORITATIVE CONTRACTS (verify against code; do not regress full/monitor paths):
- Queue table credeals.cre_enrichment_queue (sql/007_cre_change_tracking.sql:96): columns
  id, brokerage_id, source_key, external_id, url, reason CHECK(in 'new','changed'),
  priority DEFAULT 100, enqueued_at, claimed_at, done_at, attempts DEFAULT 0, last_error;
  UNIQUE NULLS NOT DISTINCT (brokerage_id, external_id, reason);
  drain index (priority, enqueued_at) WHERE done_at IS NULL.
- Monitor enqueues (cre_monitor.py ~line 707) columns (brokerage_id, source_key, external_id,
  url, reason) ON CONFLICT (brokerage_id, external_id, reason) DO NOTHING. No reader exists yet.
- collect.ts has NO per-listing mode today; each source is src<Name>(tx,max,monitor). The new
  --enrich-input mode MUST leave the full and monitor paths byte-identical when the flag is absent.
- colliers-main is the reference: sources/colliers-main.ts exports scrapeColliersMainDetailDoc(url)
  (line 62) and parseColliersMainDetail(entry, doc) (line 215) and colliersMainJsonLd(rawHtml) (201).
- Shared scrape/HTML helpers: lib/scrape.ts, lib/html.ts. Artifact schema + SourceResult: types.ts.
- cre_ingest.py consumes {runMeta, sources, listings[], brokers[], totalListings}; partial artifacts
  are safe with --no-mark-missing (upsert keyed on (brokerage_id, external_id); L1 COALESCE keeps prior
  prices; M1 folded-coverage blocks mark-missing on partial data; status-flip breaker inert while status
  activation off).
- SOFT-DELETE INVARIANT (must hold): only the weekly tier passes --mark-missing (cre_run_tier.sh weekly
  branch), triple-gated by dispatcher + cre_gate.py --strict downgrade + cre_ingest.py per-brokerage
  eligibility. The enrich worker is additive by construction: ALWAYS --no-mark-missing, NEVER
  --activate-status. Moving full re-scrape daily->weekly does NOT move soft-delete authority.
- Standing rules: no em dashes; never the words genuinely/honestly/straightforward; never print or commit
  POSTGRES_URL/DATABASE_URL; do NOT commit or push (the owner opens PRs manually); do NOT apply prod DDL
  or load any launchd tier outside the gated Cutover phase; NEVER load the weekly tier.
`

const TEST_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['pyCompile', 'tscNoEmit', 'bashSyntax', 'pytestPassed', 'pytestFailed', 'tsUnitPassed', 'tsUnitFailed', 'e2eRan', 'e2eResult', 'failures', 'summary'],
  properties: {
    pyCompile: { type: 'boolean' },
    tscNoEmit: { type: 'boolean' },
    bashSyntax: { type: 'boolean' },
    pytestPassed: { type: 'integer' },
    pytestFailed: { type: 'integer' },
    tsUnitPassed: { type: 'integer' },
    tsUnitFailed: { type: 'integer' },
    e2eRan: { type: 'boolean' },
    e2eResult: { type: 'string' },
    failures: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}

const REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'softDeleteInvariantHeld', 'statusActivationNeverOn', 'partialIngestSafe', 'claimCompleteRaceSafe', 'deadLetterWorks', 'fullMonitorPathsUnchanged', 'blocking', 'nonBlocking', 'summary'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'needs-fix'] },
    softDeleteInvariantHeld: { type: 'boolean' },
    statusActivationNeverOn: { type: 'boolean' },
    partialIngestSafe: { type: 'boolean' },
    claimCompleteRaceSafe: { type: 'boolean' },
    deadLetterWorks: { type: 'boolean' },
    fullMonitorPathsUnchanged: { type: 'boolean' },
    blocking: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['file', 'issue', 'why'], properties: { file: { type: 'string' }, issue: { type: 'string' }, why: { type: 'string' } } } },
    nonBlocking: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['file', 'issue'], properties: { file: { type: 'string' }, issue: { type: 'string' } } } },
    summary: { type: 'string' },
  },
}

// ---------------------------------------------------------------------------
phase('Spec')
const spec = await agent(
  `You are the lead engineer for the CRE Tier-B enrichment worker. Read the design at ${DESIGN} in full, then read the cited code to confirm every contract: sql/007_cre_change_tracking.sql (queue table), cre_monitor.py enqueue block, collect.ts (flag parsing + runSource), sources/colliers-main.ts (exported detail fns), lib/scrape.ts, lib/html.ts, types.ts (artifact + SourceKey), and cre_ingest.py main flow (partial-artifact safety).
${CONTRACTS}
Produce an authoritative implementation spec and WRITE it to ${ROOT}/out/enrich/IMPL_SPEC.md (create the dir). The spec must pin down, with exact file:line anchors: (1) the precise SourceKey union and where to register ENRICHERS; (2) the exact signatures to factor out for jll-investor and cbre detail parsing and which inline blocks to extract; (3) the exact claim/complete SQL the worker will run; (4) the exact list of files each build group will create/edit, partitioned so the three groups touch DISJOINT files (Group A: collect.ts + lib/enrich.ts + sources/{colliers-main,jll-investor,cbre}.ts + types.ts; Group B: cre_enrich.py + sql/010_cre_enrichment_ops.sql + sql/000_run_all.sql; Group C: launchd/* + cre_status.sh); (5) any contract surprise that changes the plan. Do NOT write production code yet. Return a concise summary of the spec plus any decision that diverges from the design doc.`,
  { label: 'spec:contracts', model: 'opus' }
)

// ---------------------------------------------------------------------------
phase('Build')
const build = await parallel([
  // Group A: targeted-detail scraping core (collect.ts + enrichers). B1 then B2 (shared files).
  async () => {
    const b1 = await agent(
      `Implement the collect.ts targeted-detail mode per ${ROOT}/out/enrich/IMPL_SPEC.md and the design ${DESIGN}.
${CONTRACTS}
Create lib/enrich.ts with the SourceEnricher interface, EnrichItem type, the ENRICHERS registry, and a GENERIC fallback enricher that scrapes a URL via lib/scrape.ts and extracts JSON-LD/metadata via lib/html.ts (best-effort). Add a new --enrich-input=<path> flag to collect.ts that reads {items:[{sourceKey,externalId,url,transaction?}]}, groups by sourceKey, dispatches each group to its registered enricher (generic fallback when none), and emits the standard artifact with runMeta.mode='enrich'. CRITICAL: when --enrich-input is absent, the full and monitor code paths must be byte-identical to before. Wire colliers-main into the registry using its already-exported scrapeColliersMainDetailDoc + parseColliersMainDetail as the reference enricher. Run 'npx tsx -e' or tsc --noEmit to confirm it type-checks. Return the list of files changed and the exact registry shape.`,
      { label: 'build:collect-enrich-mode', phase: 'Build', model: 'opus' }
    )
    const b2 = await agent(
      `Building on the enricher registry just added (see lib/enrich.ts and collect.ts; details: ${b1}). Add bespoke enrichers for jll-investor and cbre to the ENRICHERS registry per ${ROOT}/out/enrich/IMPL_SPEC.md. Factor each source's existing inline detail parse into an exported function (parseJllInvestorDetail extracting from __NEXT_DATA__; fetchCbreDetail via the internal JSON API) and reuse it from the enricher, so there is ONE parser per source. Do not change enumeration/monitor behavior. Confirm tsc --noEmit passes. Return files changed and any source whose detail could not be cleanly factored (note it for the weekly backstop).`,
      { label: 'build:source-enrichers', phase: 'Build' }
    )
    return { b1, b2 }
  },
  // Group B: the worker + additive SQL (disjoint files).
  () => agent(
    `Implement cre_enrich.py (the queue worker) and sql/010_cre_enrichment_ops.sql per ${ROOT}/out/enrich/IMPL_SPEC.md and ${DESIGN} Sections 4 and 6.
${CONTRACTS}
cre_enrich.py mirrors cre_monitor.py/cre_ingest.py conventions: argparse (--batch default 200, --dry-run), load_db_url(CRE_ENV_FILE) precedence, psql shell-out, never prints the URL. Flow: claim a batch transactionally (WITH ... FOR UPDATE SKIP LOCKED, reclaim claimed_at<now()-1h, skip attempts>=5, increment attempts on claim) -> if empty, log+exit 0 -> write out/enrich/claim_<stamp>.json -> subprocess 'npx tsx collect.ts --enrich-input=claim.json --out=enriched.json' -> subprocess 'python3 cre_ingest.py --in enriched.json --no-mark-missing' -> complete: external_ids present in enriched.json get done_at=now(); absent ones leave done_at NULL (retry until attempts>=5 dead-letter) with last_error set. It must ALWAYS pass --no-mark-missing and NEVER --activate-status. sql/010 adds two additive idempotent views: v_cre_enrichment_queue_pending (done_at IS NULL AND attempts<5) and v_cre_enrichment_dead (done_at IS NULL AND attempts>=5); no table change; wire 010 into sql/000_run_all.sql after 009. Do NOT apply any SQL to prod. Confirm py_compile passes. Return files changed.`,
    { label: 'build:worker-and-sql', phase: 'Build', model: 'opus' }
  ),
  // Group C: launchd cadence restructure (disjoint files).
  () => agent(
    `Implement the cadence restructure per ${DESIGN} Section 5.
${CONTRACTS}
Changes: (1) launchd/ai.agentic.cre-monitor.plist.template -> replace the 8-entry StartCalendarInterval array with TWO entries at 06:10 and 18:10. (2) NEW launchd/ai.agentic.cre-enrich.plist.template -> 6-entry array (00:30/04:30/08:30/12:30/16:30/20:30), label ai.agentic.cre-enrich, ProgramArguments invoking cre_run_tier.sh enrich, EnvironmentVariables = PATH(__BIN_PATH__) + __ENV_EXTRA__ only (NO CRE_MONITOR_APPLY, NO status-flip var), WorkingDirectory __COLLECTOR_DIR__, logs out/daily/cre-enrich.{out,err}.log. (3) launchd/install_launchd.sh -> add 'enrich' to TIERS + label_for + render; keep --load gating; add an explicit guard that --load NEVER loads weekly. (4) launchd/cre_run_tier.sh -> add an enrich) case running 'python3 cre_enrich.py --batch \${CRE_ENRICH_BATCH:-200}', write last_run_enrich.json marker; keep the daily) case for rollback but note it is retired/unscheduled. (5) cre_status.sh -> add enrich to stale_threshold (6h), newest_artifact (out/enrich), and the three 'for tier in' loops; change the monitor stale_threshold from 4.5h to 18h. Validate each plist template with plutil and 'bash -n' each shell script. Do NOT run launchctl or install/load anything. Return files changed.`,
    { label: 'build:launchd-cadence', phase: 'Build' }
  ),
])

// ---------------------------------------------------------------------------
phase('Test')
const test = await agent(
  `Write and run the tests for the enrichment worker per ${DESIGN} Section 8. Working dir ${ROOT}.
Add pytest cases under tests/ (reuse existing psql-mock/fixture patterns): cre_enrich.py claim idempotency, stale-claim reclaim, dead-letter at attempts>=5, complete marks done only for external_ids present in the artifact and retries the absent ones, and a guard test asserting the worker's ingest invocation contains --no-mark-missing and never --activate-status / --mark-missing. Add a TS unit test for --enrich-input grouping + artifact shape and the generic fallback extraction on a fixture. Then RUN: 'python3 -m py_compile' on changed .py; 'bash -n' on changed .sh; 'npx tsc --noEmit' (or the repo's TS check); the pytest suite (report pass/fail counts); the TS unit tests. If the local Firecrawl stack at http://localhost:3002 is healthy, run a tiny e2e: seed 2 synthetic colliers-main queue rows in a scratch artifact path (do NOT mutate prod queue unless safe and reversible) and exercise collect.ts --enrich-input -> cre_ingest.py --dry-run, asserting the artifact shape and that no mark-missing/status path fires; if the stack is down, set e2eRan=false and say so. Do not commit. Return the structured test result.`,
  { label: 'test:suite', schema: TEST_SCHEMA }
)

// ---------------------------------------------------------------------------
phase('Review')
let review = await agent(
  `Adversarially review the full working-tree diff for the enrichment worker (git diff against the branch base). Working dir ${ROOT}.
${CONTRACTS}
Default every safety verdict to FALSE unless you can prove it from the code. Specifically verify: (1) softDeleteInvariantHeld - the worker can never reach a --mark-missing path and only weekly passes it; (2) statusActivationNeverOn - the worker never sets --activate-status / CRE_ACTIVATE_STATUS; (3) partialIngestSafe - a small claimed batch cannot trip the status-flip breaker or mis-fire folded-coverage; (4) claimCompleteRaceSafe - claim/complete is transactional, stale-claim reclaim works, no lost/double work; (5) deadLetterWorks - attempts>=5 rows leave the drain set and surface in v_cre_enrichment_dead; (6) fullMonitorPathsUnchanged - collect.ts full and --monitor outputs are byte-identical when --enrich-input is absent. List any blocking issue with file + why. Return the structured verdict.`,
  { label: 'review:adversarial', model: 'opus', schema: REVIEW_SCHEMA }
)

if (review.verdict === 'needs-fix' && review.blocking.length) {
  log(`Review found ${review.blocking.length} blocking issue(s); running one fix pass.`)
  await agent(
    `Fix exactly these blocking review findings, minimally, without regressing other behavior or the soft-delete invariant. Working dir ${ROOT}. Findings: ${JSON.stringify(review.blocking)}. After fixing, re-run py_compile / bash -n / tsc --noEmit and the affected tests to confirm green. Return what you changed.`,
    { label: 'review:fix', model: 'opus' }
  )
  review = await agent(
    `Re-review ONLY the items previously flagged blocking to confirm they are resolved and nothing new broke: ${JSON.stringify(review.blocking)}. Re-derive all six safety verdicts from the code. Return the structured verdict.`,
    { label: 'review:reverify', model: 'opus', schema: REVIEW_SCHEMA }
  )
}

// ---------------------------------------------------------------------------
phase('Docs')
const docs = await agent(
  `Update documentation to match the shipped enrichment worker + cadence restructure. Working dir ${ROOT}.
Update: ENRICHMENT_WORKER_DESIGN_2026-06-15.md status line to IMPLEMENTED (note any divergence); launchd/CLAUDE.md + launchd/README.md tier table (monitor 2x/day, new enrich 4h, weekly full+mark-missing, daily retired); START_HERE.md run model + status matrix; sql/CLAUDE.md (note 010; and correct the now-stale line claiming 009 is NOT YET APPLIED - 009 was applied to prod on 2026-06-15); the parent firecrawl/CLAUDE.md CRE section key-components list to mention cre_enrich.py and the enrich tier. Honor the never-delete-without-backup rule for any file you replace wholesale. Do NOT commit or push. Then produce the exact operator cutover runbook (the launchctl + psql commands from Design Section 9) as the return value, clearly marking the held items (weekly mark-missing, status activation, consumer board-gate) as still gated.`,
  { label: 'docs:sync-and-runbook' }
)

// ---------------------------------------------------------------------------
phase('Cutover')
const cutover = (args && args.cutover === true)
let cutoverResult
if (!cutover) {
  cutoverResult = 'SKIPPED (no args.cutover). Plists were rendered/validated but NOT installed or loaded; SQL 010 NOT applied. Live cutover steps are in the runbook above; re-run this workflow with {cutover:true} to apply the SAFE tiers, or run the runbook manually. The weekly soft-delete tier is never loaded by this workflow.'
  log(cutoverResult)
} else {
  cutoverResult = await agent(
    `Perform ONLY the SAFE, additive live cutover per ${DESIGN} Section 9 steps 3-7. Working dir ${ROOT}.
ALLOWED: apply sql/010_cre_enrichment_ops.sql to prod (additive views only) via psql or Supabase MCP (project fhqycqubkkrdgzswccwd); render+install the monitor/enrich/weekly plists with 'bash launchd/install_launchd.sh monitor enrich weekly' (install only); reload the monitor plist at the new 2x/day cadence (launchctl unload then load -w); load enrich with 'bash launchd/install_launchd.sh --load enrich'; unload the daily plist (launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-daily.plist). FORBIDDEN: do NOT load the weekly tier; do NOT pass --mark-missing anywhere; do NOT enable status activation; do NOT commit or push. After cutover run 'bash cre_status.sh' and confirm monitor=2x/day, enrich=4h present, weekly held/not-loaded, daily gone, queue health probe non-erroring. Return exactly what you applied and the heartbeat verdict.`,
    { label: 'cutover:safe-tiers', model: 'opus' }
  )
}

return {
  spec: typeof spec === 'string' ? spec.slice(0, 600) : spec,
  buildGroups: build.map((b, i) => (b ? `group${i}: ok` : `group${i}: FAILED`)),
  test,
  review,
  cutover: cutoverResult,
  reminders: [
    'No commit/push performed - owner opens the PR manually.',
    'Held for explicit go-ahead: weekly --mark-missing tier, first live status activation, consumer board-gate deploy.',
    cutover ? 'Safe-tier cutover attempted (see cutover field).' : 'Live scheduler untouched - runbook printed for manual cutover.',
  ],
}
