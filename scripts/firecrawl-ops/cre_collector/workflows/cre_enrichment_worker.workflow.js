export const meta = {
  name: 'cre-enrichment-worker',
  description: 'Build the Tier-B enrichment-queue worker + cadence restructure (monitor 2x/day, full re-scrape weekly + additive, retire daily), test, adversarially review, document. Live launchd cutover only with args.cutover=true; never enables soft-delete (--mark-missing / CRE_WEEKLY_MARK_MISSING).',
  whenToUse: 'Run after the design in ENRICHMENT_WORKER_DESIGN_2026-06-15.md is approved. Plain run does all code+test+review+docs and renders plists but does NOT touch the live scheduler. Pass {cutover:true} to also apply the additive SQL and load the safe tiers (monitor, enrich, additive weekly).',
  phases: [
    { title: 'Spec', detail: 'opus: re-derive authoritative contracts, write IMPL_SPEC.md' },
    { title: 'Build', detail: 'parallel disjoint groups: collect.ts targeted detail + enrichers; cre_enrich.py + SQL 010; launchd cadence' },
    { title: 'Test', detail: 'write + run pytest, TS node:test unit, syntax checks, tiny e2e dry run' },
    { title: 'Review', detail: 'opus adversarial: soft-delete invariant, URL-match, attempts accounting, dead-letter, partial-ingest safety; bounded fix loop' },
    { title: 'Docs', detail: 'update CLAUDE.mds / START_HERE / design status; emit cutover runbook' },
    { title: 'Cutover', detail: 'GATED on args.cutover: apply SQL 010, reload monitor 2x/day, load enrich, unload daily, load ADDITIVE weekly; NEVER enable mark-missing' },
  ],
}

const ROOT = '/Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector'
const SQLDIR = '/Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/sql'
const DESIGN = `${ROOT}/ENRICHMENT_WORKER_DESIGN_2026-06-15.md`

const CONTRACTS = `
AUTHORITATIVE CONTRACTS (verified against code 2026-06-15; do not regress full/monitor paths):
- Queue table credeals.cre_enrichment_queue (sql/007_cre_change_tracking.sql:96): columns
  id, brokerage_id, source_key, external_id, url, reason CHECK(in 'new','changed'),
  priority DEFAULT 100, enqueued_at, claimed_at, done_at, attempts DEFAULT 0, last_error;
  UNIQUE NULLS NOT DISTINCT (brokerage_id, external_id, reason);
  drain index (priority, enqueued_at) WHERE done_at IS NULL.
- Monitor enqueues (cre_monitor.py ~line 707) columns (brokerage_id, source_key, external_id,
  url, reason) ON CONFLICT (brokerage_id, external_id, reason) DO NOTHING. external_id is the
  FOLDED/PREFIXED ingest key (main:/investor:/dealflow:), because cre_monitor reuses cre_ingest.to_row.
- collect.ts has NO per-listing mode today; each source is src<Name>(tx,max,monitor); flags parse in
  lib/config.ts via parseArgs. The new --enrich-input mode MUST leave the full and monitor paths
  byte-identical when the flag is absent.
- ID FACTS: (a) the artifact carries the NATIVE source id (colliers-main.ts:233 id:entry.id); ingest
  re-applies the prefix. So each enricher MUST strip its SOURCE_TO_BROKERAGE prefix off EnrichItem.externalId
  to rebuild the native id, else re-ingest double-prefixes (main:main:...) and the row dead-letters.
  (b) Completion is matched by URL: every enricher echoes EnrichItem.url onto listing.url, and the worker
  marks a claimed row done iff its url is in the artifact. URL is verbatim in both queue and artifact.
- ENRICHERS: colliers-main (reuse exported scrapeColliersMainDetailDoc(url):62 + parseColliersMainDetail(entry,doc):215,
  building ColliersMainEntry={url,lastmod:null,id:nativeId}) and jll-investor (factor jllInvestorNextData:38 into
  parseJllInvestorDetail). CBRE is ENUMERATION-ONLY (cbre.ts:51, listings-api JSON == full output): NO cbre enricher;
  cbre rides the weekly backstop. Generic fallback: scrapeDoc (lib/scrape.ts) + jsonLdObjects/firstJsonLd (lib/html.ts).
- cre_ingest.py: argparse has --in (repeatable), --mark-missing, --mark-missing-floor, --activate-status, --dry-run,
  --env-file, --keep-artifacts. THERE IS NO --no-mark-missing FLAG; additive is the default. The worker ingest call is
  ALWAYS ["--in", enriched.json] and NEVER --mark-missing / --no-mark-missing / --activate-status. Partial artifacts are
  safe additively (upsert keyed on (brokerage_id, external_id); L1 COALESCE keeps prior prices; status-flip breaker inert
  while status activation off).
- WORKER (cre_enrich.py) = pure builders + thin run(): build_claim_sql (no attempts++ at claim; FOR UPDATE SKIP LOCKED;
  reclaim claimed_at<now()-1h; attempts<5), build_collect_argv, build_ingest_argv (--in only), select_done_and_retry
  (URL-keyed), build_complete_sql (DELETE done rows; queue is ephemeral, cre_listing_events is the audit), build_release_sql
  (whole-run failure: SET claimed_at=NULL, attempts untouched). Increment attempts ONLY on claimed-but-absent rows AFTER a
  successful collect. If collect exits nonzero or enriched.json missing/invalid/empty: release claims, set last_error, exit
  nonzero, do NOT ingest. psql SQL uses sql_lit + standard_conforming_strings/ON_ERROR_STOP pins like cre_monitor.build_write_sql;
  never f-string a url; never print the DB url. cre_run_tier.sh (not the worker) writes the verdict marker; worker uses exit codes.
- SOFT-DELETE INVARIANT (must hold): --mark-missing is produced ONLY by the weekly tier and ONLY when CRE_WEEKLY_MARK_MISSING=1
  (default additive), triple-gated by dispatcher + cre_gate.py --strict downgrade + cre_ingest.py per-brokerage eligibility. The
  enrich worker is additive by construction. The weekly tier is additive by default and safe to load; loading it does NOT enable
  soft-delete.
- Standing rules: no em dashes; never the words genuinely/honestly/straightforward; never print or commit
  POSTGRES_URL/DATABASE_URL; do NOT commit or push (the owner opens PRs manually); do NOT apply prod DDL
  or load any launchd tier outside the gated Cutover phase; NEVER enable mark-missing (do not set CRE_WEEKLY_MARK_MISSING,
  do not pass --mark-missing).
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
  required: ['verdict', 'softDeleteInvariantHeld', 'statusActivationNeverOn', 'ingestArgvInOnly', 'urlMatchCorrect', 'attemptsAccountingSafe', 'partialIngestSafe', 'claimCompleteRaceSafe', 'deadLetterWorks', 'fullMonitorPathsUnchanged', 'blocking', 'nonBlocking', 'summary'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'needs-fix'] },
    softDeleteInvariantHeld: { type: 'boolean' },
    statusActivationNeverOn: { type: 'boolean' },
    ingestArgvInOnly: { type: 'boolean' },
    urlMatchCorrect: { type: 'boolean' },
    attemptsAccountingSafe: { type: 'boolean' },
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
  `You are the lead engineer for the CRE Tier-B enrichment worker. Read the design at ${DESIGN} in full, then read the cited code to confirm every contract: sql/007_cre_change_tracking.sql (queue table), cre_monitor.py enqueue block + how external_id is folded, collect.ts + lib/config.ts (flag parsing) + runSource, sources/colliers-main.ts (exported detail fns + ColliersMainEntry), sources/jll-investor.ts (jllInvestorNextData), sources/cbre.ts (confirm enumeration-only), lib/scrape.ts, lib/html.ts, types.ts (artifact + SourceKey), and cre_ingest.py main flow + argparse (confirm NO --no-mark-missing flag; load_db_url; sql_lit; to_row prefixing).
${CONTRACTS}
Produce an authoritative implementation spec and WRITE it to ${ROOT}/out/enrich/IMPL_SPEC.md (create the dir). The spec must pin down, with exact file:line anchors: (1) the SourceKey union and where to register ENRICHERS; (2) the exact prefix-strip per source (SOURCE_TO_BROKERAGE main:/investor:/dealflow:) to rebuild native ids, and the exact signature to factor out for jll-investor detail parsing; (3) the exact claim / complete(DELETE) / release SQL the worker will run, plus the pure-builder function list (build_claim_sql, build_collect_argv, build_ingest_argv, select_done_and_retry, build_complete_sql, build_release_sql); (4) the exact files each build group creates/edits, partitioned DISJOINT (Group A: collect.ts + lib/config.ts + lib/enrich.ts + sources/{colliers-main,jll-investor}.ts + types.ts; Group B: cre_enrich.py + sql/010_cre_enrichment_ops.sql + sql/000_run_all.sql; Group C: launchd/* + cre_status.sh); (5) any contract surprise that changes the plan. Do NOT write production code yet. Return a concise summary plus any divergence from the design doc.`,
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
Add --enrich-input=<path> to lib/config.ts parseArgs options. Create lib/enrich.ts with the SourceEnricher interface, EnrichItem type, the ENRICHERS registry, and a GENERIC fallback enricher that scrapes a URL via scrapeDoc (lib/scrape.ts) and extracts JSON-LD via jsonLdObjects/firstJsonLd (lib/html.ts), best-effort. In collect.ts, when --enrich-input is present, read {items:[{sourceKey,externalId,url,transaction?}]}, group by sourceKey, dispatch each group to its registered enricher (generic fallback when none), and emit the standard artifact with runMeta.mode='enrich'. EVERY enricher MUST echo EnrichItem.url onto its output listing.url. CRITICAL: when --enrich-input is absent, the full and monitor code paths must be byte-identical to before. Wire colliers-main into the registry: strip the 'main:' prefix off externalId to rebuild the native id, build ColliersMainEntry={url,lastmod:null,id:nativeId}, then reuse scrapeColliersMainDetailDoc + parseColliersMainDetail. Run npm run typecheck to confirm it type-checks. Return files changed and the registry shape.`,
      { label: 'build:collect-enrich-mode', phase: 'Build', model: 'opus' }
    )
    const b2 = await agent(
      `Building on the enricher registry just added (see lib/enrich.ts and collect.ts; details: ${b1}). Add the jll-investor bespoke enricher to ENRICHERS per ${ROOT}/out/enrich/IMPL_SPEC.md: factor the inline __NEXT_DATA__ detail parse into an exported parseJllInvestorDetail (reusing jllInvestorNextData) and call it from the enricher; strip the 'investor:' prefix to rebuild the native id; echo the input url. Do NOT add a cbre enricher (cbre is enumeration-only; it rides the weekly backstop). Do not change enumeration/monitor behavior. Confirm npm run typecheck passes. Return files changed and any source whose detail could not be cleanly factored (note it for the weekly backstop).`,
      { label: 'build:source-enrichers', phase: 'Build' }
    )
    return { b1, b2 }
  },
  // Group B: the worker + additive SQL (disjoint files).
  () => agent(
    `Implement cre_enrich.py (the queue worker) and sql/010_cre_enrichment_ops.sql per ${ROOT}/out/enrich/IMPL_SPEC.md and ${DESIGN} Sections 4 and 6.
${CONTRACTS}
cre_enrich.py mirrors cre_monitor.py/cre_ingest.py conventions: argparse (--batch default 200, --dry-run), reuse cre_ingest.load_db_url precedence, psql shell-out, never prints the URL. Structure it as PURE BUILDERS plus a thin run(): build_claim_sql(batch) [WITH ... FOR UPDATE SKIP LOCKED, reclaim claimed_at<now()-1h, attempts<5, NO attempts++ at claim, RETURNING id,source_key,external_id,url,reason], build_collect_argv, build_ingest_argv (returns ["--in", path] ONLY), select_done_and_retry(claimed_rows, enriched_listings) [URL-keyed: done = rows whose url is in the artifact; retry = the rest], build_complete_sql(done_urls) [DELETE FROM cre_enrichment_queue WHERE url IN (...) using sql_lit; the queue is ephemeral so a later change re-enqueues], build_release_sql(claimed_ids) [SET claimed_at=NULL, attempts untouched]. run(): claim -> if empty exit 0 (no subprocess) -> write out/enrich/claim_<stamp>.json -> subprocess collect.ts --enrich-input -> if returncode!=0 OR enriched.json missing/invalid/empty: release claims, set last_error, exit nonzero, do NOT ingest -> else subprocess cre_ingest.py --in enriched.json (NEVER --mark-missing/--no-mark-missing/--activate-status) -> complete: DELETE done urls, increment attempts ONLY on the claimed-but-absent set, set last_error on them. All SQL uses sql_lit + SET LOCAL standard_conforming_strings=on + -v ON_ERROR_STOP=1 like cre_monitor.build_write_sql; never f-string a url; never print the DB url. The worker writes NO verdict marker (cre_run_tier.sh owns it); it communicates via exit code. sql/010 adds two additive idempotent views: v_cre_enrichment_queue_pending (done_at IS NULL AND attempts<5) and v_cre_enrichment_dead (done_at IS NULL AND attempts>=5); no table change; wire 010 into sql/000_run_all.sql after 009. Do NOT apply any SQL to prod. Confirm py_compile passes. Return files changed.`,
    { label: 'build:worker-and-sql', phase: 'Build', model: 'opus' }
  ),
  // Group C: launchd cadence restructure (disjoint files).
  () => agent(
    `Implement the cadence restructure per ${DESIGN} Section 5.
${CONTRACTS}
Changes: (1) launchd/ai.agentic.cre-monitor.plist.template -> replace the 8-entry StartCalendarInterval array with TWO entries at 06:10 and 18:10. (2) NEW launchd/ai.agentic.cre-enrich.plist.template -> 6-entry array (00:30/04:30/08:30/12:30/16:30/20:30), label ai.agentic.cre-enrich, ProgramArguments invoking cre_run_tier.sh enrich, EnvironmentVariables = PATH(__BIN_PATH__) + __ENV_EXTRA__ only (NO CRE_MONITOR_APPLY, NO status-flip var), WorkingDirectory __COLLECTOR_DIR__, logs out/daily/cre-enrich.{out,err}.log. (3) launchd/install_launchd.sh -> add 'enrich' to TIERS + label_for + render; keep --load gating. (4) launchd/cre_run_tier.sh -> add an enrich) case running 'python3 cre_enrich.py --batch \${CRE_ENRICH_BATCH:-200}'; change the weekly) case so mark-missing is conditional: MM="--no-mark-missing"; [ "\${CRE_WEEKLY_MARK_MISSING:-0}" = "1" ] && MM="--mark-missing"; then bash cre_daily_update.sh "\$MM". Keep the daily) case for rollback but note it is retired/unscheduled. Preserve the verdict-marker write for enrich (last_run_enrich.json). (5) cre_status.sh -> replace the three 'for tier in monitor daily weekly' loops (:103/130/288) with 'monitor enrich weekly'; add enrich) echo $((6*3600)) to stale_threshold and change monitor from 4.5h to 18h; add out/enrich to newest_artifact. Validate each plist template with plutil and 'bash -n' each shell script. Do NOT run launchctl or install/load anything. Return files changed.`,
    { label: 'build:launchd-cadence', phase: 'Build' }
  ),
])

// ---------------------------------------------------------------------------
phase('Test')
const test = await agent(
  `Write and run the tests for the enrichment worker per ${DESIGN} Section 8. Working dir ${ROOT}.
Add pytest cases in tests/test_cre_enrich.py (no-DB, pure-builder style like test_monitor.py / test_ingest_mark_missing.py / test_env_discovery.py): claim SQL shape (FOR UPDATE SKIP LOCKED, attempts<5, no attempts++ at claim, RETURNING url), stale-claim reclaim, idempotent claim with GUC pins and no DB url, select_done_and_retry marks done only urls in the artifact, URL match works when external_id is folded but the artifact id is native, build_complete_sql DELETEs done rows (sql_lit) and never sets done_at, dead-letter at attempts>=5 leaves the drain set, whole-run collect failure releases claims without incrementing attempts and skips ingest, empty/missing/invalid enriched.json marks nothing done, the ingest argv is exactly ["--in", path] and never --mark-missing/--no-mark-missing/--activate-status, collect argv targets --enrich-input, empty claim exits 0 with no subprocess, DB url never printed, env discovery reuses cre_ingest.load_db_url, and the Phase-1 enricher set is exactly {colliers-main, jll-investor} (excludes cbre). Add TS node:test units (run via 'npm run test:unit') for --enrich-input grouping + artifact shape (runMeta.mode==='enrich', every listing echoes its input url) and the generic fallback extraction on a fixture, plus a colliers native-id reconstruction test. Then RUN: py_compile on changed .py; bash -n on changed .sh; npm run typecheck; the pytest suite (report pass/fail counts); npm run test:unit (report pass/fail). If the local Firecrawl stack at http://localhost:3002 is healthy, run a tiny e2e: seed 2 synthetic colliers-main queue rows in a scratch artifact path (do NOT mutate the prod queue), exercise collect.ts --enrich-input -> cre_ingest.py --dry-run, asserting the artifact shape and that no mark-missing/status path fires; if the stack is down set e2eRan=false and say so. Do not commit. Return the structured test result.`,
  { label: 'test:suite', schema: TEST_SCHEMA }
)

// ---------------------------------------------------------------------------
phase('Review')
let review = await agent(
  `Adversarially review the full working-tree diff for the enrichment worker (git diff against the branch base). Working dir ${ROOT}.
${CONTRACTS}
Default every safety verdict to FALSE unless you can prove it from the code. Verify: (1) softDeleteInvariantHeld - the worker can never reach a --mark-missing path; only the weekly tier passes it and only under CRE_WEEKLY_MARK_MISSING=1; (2) statusActivationNeverOn - the worker never sets --activate-status / CRE_ACTIVATE_STATUS; (3) ingestArgvInOnly - the worker's ingest argv is exactly --in <path>, never --mark-missing/--no-mark-missing/--activate-status (the last would crash argparse); (4) urlMatchCorrect - completion matches claimed rows to enriched listings by URL, enrichers strip the fold-prefix to rebuild native ids and echo the input url, so re-ingest does not double-prefix; (5) attemptsAccountingSafe - attempts is not incremented at claim, only on claimed-but-absent rows after a successful collect, and a whole-run failure releases claims; (6) partialIngestSafe - a small claimed batch cannot trip the status-flip breaker or mis-fire folded-coverage; (7) claimCompleteRaceSafe - claim is one atomic statement with FOR UPDATE SKIP LOCKED, stale-claim reclaim works, the crash-after-ingest-before-delete window is idempotent; (8) deadLetterWorks - attempts>=5 rows leave the drain set and surface in v_cre_enrichment_dead, and delete-on-done lets a later change re-enqueue; (9) fullMonitorPathsUnchanged - collect.ts full and --monitor outputs are byte-identical when --enrich-input is absent. List any blocking issue with file + why. Return the structured verdict.`,
  { label: 'review:adversarial', model: 'opus', schema: REVIEW_SCHEMA }
)

if (review.verdict === 'needs-fix' && review.blocking.length) {
  log(`Review found ${review.blocking.length} blocking issue(s); running one fix pass.`)
  await agent(
    `Fix exactly these blocking review findings, minimally, without regressing other behavior or the soft-delete invariant. Working dir ${ROOT}. Findings: ${JSON.stringify(review.blocking)}. After fixing, re-run py_compile / bash -n / npm run typecheck and the affected tests to confirm green. Return what you changed.`,
    { label: 'review:fix', model: 'opus' }
  )
  review = await agent(
    `Re-review ONLY the items previously flagged blocking to confirm they are resolved and nothing new broke: ${JSON.stringify(review.blocking)}. Re-derive all nine safety verdicts from the code. Return the structured verdict.`,
    { label: 'review:reverify', model: 'opus', schema: REVIEW_SCHEMA }
  )
}

// ---------------------------------------------------------------------------
phase('Docs')
const docs = await agent(
  `Update documentation to match the shipped enrichment worker + cadence restructure. Working dir ${ROOT}.
Update: ENRICHMENT_WORKER_DESIGN_2026-06-15.md status line to IMPLEMENTED (note any divergence); launchd/CLAUDE.md + launchd/README.md tier table (monitor 2x/day, new enrich 4h, weekly full ADDITIVE backstop + CRE_WEEKLY_MARK_MISSING escalation, daily retired); cre_collector/CLAUDE.md (the enrichment design row + the Files row for cre_enrich.py); START_HERE.md run model + status matrix; sql/CLAUDE.md (note 010 is now written/wired); the parent firecrawl/CLAUDE.md CRE section key-components list to mention cre_enrich.py and the enrich tier. Keep edits factual and lean; no em dashes; never the words genuinely/honestly/straightforward. Honor the never-delete-without-backup rule for any file you replace wholesale. Do NOT commit or push. Then produce the exact operator cutover runbook (the launchctl + psql commands from Design Section 9, including loading the ADDITIVE weekly backstop) as the return value, clearly marking the held items (CRE_WEEKLY_MARK_MISSING soft-delete escalation, status activation, consumer board-gate) as still gated.`,
  { label: 'docs:sync-and-runbook' }
)

// ---------------------------------------------------------------------------
phase('Cutover')
const cutover = (args && args.cutover === true)
let cutoverResult
if (!cutover) {
  cutoverResult = 'SKIPPED (no args.cutover). Plists were rendered/validated but NOT installed or loaded; SQL 010 NOT applied. Live cutover steps are in the runbook above; re-run this workflow with {cutover:true} to apply the SAFE additive tiers, or run the runbook manually. Soft-delete (CRE_WEEKLY_MARK_MISSING / --mark-missing) is never enabled by this workflow.'
  log(cutoverResult)
} else {
  cutoverResult = await agent(
    `Perform ONLY the SAFE, additive live cutover per ${DESIGN} Section 9 steps 3-8. Working dir ${ROOT}.
ALLOWED: apply sql/010_cre_enrichment_ops.sql to prod (additive views only) via psql or Supabase MCP (project fhqycqubkkrdgzswccwd); render+install the monitor/enrich/weekly plists with 'bash launchd/install_launchd.sh monitor enrich weekly' (install only); reload the monitor plist at the new 2x/day cadence (launchctl unload then load -w); load enrich with 'bash launchd/install_launchd.sh --load enrich'; unload the daily plist (launchctl unload ~/Library/LaunchAgents/ai.agentic.cre-daily.plist); load the ADDITIVE weekly backstop with 'bash launchd/install_launchd.sh --load weekly' (safe because CRE_WEEKLY_MARK_MISSING is unset, so it runs --no-mark-missing). FORBIDDEN: do NOT set CRE_WEEKLY_MARK_MISSING; do NOT pass --mark-missing anywhere; do NOT enable status activation; do NOT commit or push. After cutover run 'bash cre_status.sh' and confirm monitor=2x/day, enrich=4h present, weekly=additive loaded, daily gone, queue health probe non-erroring. Return exactly what you applied and the heartbeat verdict.`,
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
    'Held for explicit go-ahead: CRE_WEEKLY_MARK_MISSING soft-delete escalation, first live status activation, consumer board-gate deploy.',
    cutover ? 'Safe additive-tier cutover attempted (see cutover field); mark-missing never enabled.' : 'Live scheduler untouched - runbook printed for manual cutover.',
  ],
}
