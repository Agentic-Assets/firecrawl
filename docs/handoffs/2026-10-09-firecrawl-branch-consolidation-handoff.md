# Firecrawl fork handoff: branch consolidation, hardening, upstream sync (2026-10-09)

Linear ledger: [AGENTIC-3730](https://linear.app/agenticassets/issue/AGENTIC-3730). Full subagent reports are attached
there as comments: C10 stack, older branches, LaunchAgents, main health, PR #74, PR #65, and the deleted-branch SHAs.
This file is a self-contained brief for the next agent. It lives on the unmerged branch
`docs/handoff-2026-10-09-branch-consolidation`, so it stays out of `main` under the repo's Linear-first rule. Read it, do
the work, and post proof to Linear and the PRs.

Repo: `Agentic-Assets/firecrawl`, a fork of `firecrawl/firecrawl`. **Always pass `--repo Agentic-Assets/firecrawl` to
`gh`.** Without it, `gh` resolves to upstream. `main` was at `a8f9d5e70` when this was written.

## Read first: safety rules that bit us

1. **Never run `scripts/firecrawl-ops/cre_collector` Python tests from a git worktree.** The canonical lock lookup
   resolves to the main checkout's live `out/daily/.cre.lock`; this caused the 2026-09-15 quarantine incident. Run them
   from a separate `git clone --no-hardlinks <repo> <dir>` and confirm the lock resolves inside the clone.
   TypeScript-only packages such as `apps/playwright-service-ts` are fine in a worktree.
2. **Hosted GitHub Actions are disabled on the fork.** CI gives no signal; local verification is the only proof. Record
   exact commands and pass counts in each PR body.
3. **Merge policy** (`$AA_CONTEXT_ROOT/policies/git.md`):
   - Use the verified self-merge lane. The branch must be current with `main`, verified at its exact head, and have a
     reconciled local `adversarial-pr-review` (finder plus skeptic).
   - Run `python3 $AA_CONTEXT_ROOT/scripts/check_pr_body.py --body-file <f>`.
   - Merge with a merge commit (`gh pr merge N --merge --match-head-commit <sha>`). No `--auto`, no force-push, no
     direct push to `main`.
4. **No live writes.** No production Supabase (`credeals`) writes, no launchd loads, and no restarts of the live
   Docker stack on :3002 unless Cayman explicitly approves.
5. **apps/api host install:**
   - `pnpm install --frozen-lockfile` fails on macOS because the `foundationdb` native build lacks `fdb_c.h`. Use
     `--ignore-scripts`, then run `pnpm build` in `apps/api/native` before `tsc`. Docker images build it in-container.
   - Harness snips need Redis, RabbitMQ and nuq-postgres. Run throwaway copies under compose project `fc-sync-scratch`
     on ports 16379 / 15672 / 15433. The exact working command is in the PR #76 comment.

## What is done (verified)

| Item | Result |
|---|---|
| CRE LaunchAgents | Six `ai.agentic.cre-*` plists had no `automations/registry.yaml` row. Two were loaded (RunAtLoad), running from the stale checkout `firecrawl-cre-refresh-freshness` (601 commits behind). With Cayman's approval, both were booted out and all six plists moved to `~/Library/LaunchAgents.disabled-2026-10-09/`. To undo, move them back. |
| Verifier gap | Context Engineering `verify-install.sh` ignored `ai.agentic.*`. Fixed on its `main` at `1eb7422`. |
| PR #74 (Stace, series-child reconciliation helper) | Two rounds of adversarial review, fixes `6889962bd`, `b66e46731`, `b5dc35c87`. **Merged** as `a8f9d5e70`. Full cre_collector suite: 3503 passed, 18 skipped. Running `--apply` stays gated on AGENTIC-3045. Closeout posted on AGENTIC-2902. |
| PR #69 (C10 final hardening) | **Closed** as superseded by #67. All 14 C10 branches were already on `main` in equal or stronger form. |
| Branch cleanup | 33 remote branches deleted (merged, superseded or stale). SHAs are in the AGENTIC-3730 comment. `stace-june20` is archived as tag `archive/stace-june20`. |
| Xcode | Cayman accepted the Xcode 27 license. `/usr/bin/python3` and `git` work again, which also fixed the 2 capacity-benchmark test failures. |

## Open work, in order

### 1. PR #77: playwright sidecar deadline and DNS fixes (`fix/playwright-scrape-deadline-dns`)
Ports the still-valuable half of draft PR #43:
- a single `/scrape` deadline, with 503 `SCRAPE_ADMISSION_TIMEOUT` / 504 `SCRAPE_WORK_TIMEOUT`;
- exactly-once permit release, held until the browser context is closed;
- DNS outage returns a retryable 503 `TARGET_DNS_UNAVAILABLE` instead of an empty 403;
- an opt-in pacer, `SCRAPE_START_INTERVAL_MS`, default off.

The adversarial review was **blocking**. Findings are in the PR #77 comment. **All five findings are now fixed, at
head `448656a21`, pushed.** The one partial item is the route-level `api.ts` tests from F5.
- `pnpm test` passes 87/87 (6 consecutive runs); tsc and build are clean; the loopback smoke matrix passes.
- Bracketed IPv6 private literals now return 200 with pageStatusCode 403 on /scrape and 400 on /browser-batch-fetch.
- `timeout` and `wait_after_load` are capped at 24 h (400 above). apps/api has no max on `timeout`.
- The fixer's full note is reproduced at the end of this file and posted on PR #77.

**What is left for #77:**
1. Rerun the adversarial review on `448656a21`.
2. Optionally run `pnpm knip` and prettier, and add route-level tests (that needs `api.ts` to stop calling `start()`
   on import).
3. Mark ready and merge.

The original finding list is kept below for reference.
- **F1 (blocking):** IPv6 literal URL hostnames keep their brackets (`[::1]`), so `target_dns.ts:31-34` looks them up
  and reports a DNS outage instead of a blocked private target. Then apps/api falls through to the `fetch` engine.
  Fix: strip one leading and one trailing bracket before `IPAddr.isValid`. Add tests built with
  `new URL(u).hostname` for `[::1]`, `[::ffff:127.0.0.1]`, `[fd00:ec2::254]`, and a public IPv6 literal.
- **F2:** a deadline-hit navigation sometimes returns 500 instead of 504. Fix: give Playwright `remaining + ~250 ms`,
  or map any error raised after `deadlineAt` to 504.
- **F3:** document that redirect-hop DNS failures surface from the SSRF proxy, not as `TARGET_DNS_UNAVAILABLE`.
- **F4:** cap `timeout` and `wait_after_load` below 2^31-1 ms, returning 400 above the cap. First check the apps/api
  maximum.
- **F5:** widen the 10-15 ms timing margins in `scrape_lifecycle.test.ts`.

Verify in `apps/playwright-service-ts` with `pnpm build`, `npx tsc --noEmit -p .` and `pnpm test` (the baseline was
83/83 before the fixes). Then rerun the loopback smoke matrix from the PR #77 body. Then mark the PR ready, run the body
check, and merge.

After merge:
- Close PR #43 (`codex/cre-scrape-throughput`) with a comment pointing to #77, and delete that branch.
- File an `Agent-Created` Backlog follow-up for the skipped JLL direct transport. Main's C10 JLL admission lane must
  admit it, and main's `cre_ingest.py` only accepts the `jll_detail` method.

Taking effect needs a `playwright-service` image rebuild, which is an operator step.

### 2. PR #76: upstream sync (`chore/sync-upstream-2026-10-09`, head `4c680e714`)
- **Status:** 461 upstream commits merged, with 41 conflicts resolved. The rationale for each is in the PR body.
- **Proof so far:**
  - tsc 7: 0 errors; `pnpm build` and knip clean.
  - apps/api unit tests: every failure also fails on plain upstream/main or is environmental.
  - playwright tests: 60/60.
  - cre_collector TS: 977 passed.
  - Harness snips on a scratch stack: **79 passed, 94 skipped (fire-engine, AI or proxy gated), 0 failed.**
- **Decisions already taken:**
  - Accept upstream's removal of the Postgres job log. Root `.env` has `USE_DB_AUTHENTICATION=false`, so the fork never
    wrote it.
  - Narrow the OCR-cache bypass to the local Docling adapter. The cache is inactive self-hosted.

Remaining:
1. After #77 merges: `git merge origin/main` into this branch. Expect a conflict in `apps/playwright-service-ts/api.ts`:
   keep #77's lifecycle plus upstream's final-URL reporting. Rerun playwright tests, apps/api typecheck, and the
   collector suites from a clone.
2. Adversarial review focused on the 41 conflict resolutions, especially:
   - `llmExtract.ts` / `extractSmartScrape.ts`: the fork's structured-output fallback;
   - `browser.ts`: Hangar plus the fork's 503;
   - the PDF / `firePDF.ts` typed OCR errors;
   - `generic-ai.ts`.
3. Mark ready, then merge.
4. Operator steps, in a maintenance window, with Cayman:
   - rebuild the api, playwright-service, go-html-to-md-service and nuq-postgres images;
   - on the existing nuq volume, apply `CREATE INDEX IF NOT EXISTS nuq_queue_scrape_group_failed_listing_idx ...` and
     its pg_cron reindex job (the SQL is in the PR body);
   - rename `BROWSER_SERVICE_*` to `HANGAR_URL`, only if a browser service is used;
   - clients must rename the `pageMarkdown` option to `pages`;
   - run `scripts/firecrawl-ops/firecrawl_healthcheck.sh` and `local_api_smoke_matrix.py` afterwards.

### 3. Dependency hardening (new branch off `main` after #76)
`pnpm audit --prod` findings from the main-health report on AGENTIC-3730:
- **apps/api:** 21 high and 1 critical. The critical is `proxy-addr` <2.0.8, pulled in via `@bull-board/express`. The
  highs include axios (7 advisories), multer, fast-uri, undici, brace-expansion, ip-address and source-map-js.
- **cre_collector:** 2 high (axios, undici).
- **playwright-service-ts:** 2 high and 1 critical (`proxy-addr`).

Many may be fixed by the upstream sync, so re-audit on the new `main` first. Also `apps/api/audit-ci.jsonc` allowlists
two ip-address advisories that expired on 2026-08-17; resolve or re-justify them.

### 4. PR #65 (Stace, industrial rent evidence): HOLD, founder decision
Do not merge. Merging changes production ingest:
- 0 of 17,057 replayed lease quotes parse, versus 7,457 today;
- values above 500 $/SF/yr reach the columns that EQUIRE reads;
- it conflicts with `run_final_validation`'s over-500 gate;
- the first monitor run would log spurious PRICE_CHANGE events.

Cayman and Stace need to decide whether `$` counts as USD (GetCREdata says yes), what replaces the 500 gate, and the
monitor baseline plan. The recommended restructure is to land the `rent_comp_evidence_v1` envelope alone, put the strict
column rules behind a default-off flag, and roll it out with AGENTIC-2866. The parity fix is on branch
`review/pr65-parity-fix`. Findings are in the PR #65 comment.

### 5. Small cleanups (file as `Agent-Created` Backlog children of AGENTIC-3730)
- C10 JLL admission Python tests fail when the node deps of `cre_collector` / `apps/playwright-service-ts` aren't
  installed. They should install their own deps or skip cleanly.
- `tests/test_cre_cohort_review_regressions.py::test_nested_dummy_process_is_reaped_before_cohort_worker_shutdown` is
  timing-flaky under load.
- `tests/test_conftest_lock_guard.py:87` fails only in worktrees, because the fallback and git-derived lock paths
  differ.
- `scripts/firecrawl-ops/tests/test_cre_resource_profile.py` expected the compose default `:-10` while compose has
  `:-4`. This is fixed on the #76 branch (`4c680e714`).
- The C10 listener passes `allowTestLocalTargets` as `createContext`'s `skipTlsVerification` argument. This is
  suspicious; investigate.
- The repo has no ruff config. With defaults, ruff 0.16 reports 659 findings, 62 of them E/F. Consider a config.
- 12 archived Savills scrape files under `cre_scrapers/brokers/savills/archive/` contain Savills' own `AIza...` page
  keys. They are not ours, but secret scanners will flag them.
- Codex worktree `~/.codex/worktrees/cre-c10-browser-execution` is still on the now-deleted
  `feat/c10-browser-execution-substrate`. It holds stale untracked HMAC-era notes. Remove it when Codex is idle.
- Local worktree `firecrawl-cre-refresh-freshness` (branch `fix/cre-refresh-freshness`, already merged) can be removed
  now that its LaunchAgents are gone.

## Scheduler state (for anyone touching CRE automation)
No CRE launchd job is installed on Cayman's MacBook. Reloading any tier needs gate 5 of
`tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md`, Cayman's explicit approval, and a
`disposition: keep` row in `$AA_CONTEXT_ROOT/automations/registry.yaml`. The verifier now fails unregistered
`ai.agentic.*` plists.


---

# Appendix: PR #77 fixer handoff note (verbatim)

## PR #77 review fixes: handoff (head 448656a21)

Branch `fix/playwright-scrape-deadline-dns`, pushed as a fast-forward 5bda64d49..448656a21. No force push. Tests were green at commit time.

### Status of the 5 review findings

1. **IPv6 literals reported as DNS outages (blocking): FIXED.**
   - `apps/playwright-service-ts/target_dns.ts:31-42`. `isInternalHost` strips exactly one leading `[` and one trailing `]`. The literal is classified without DNS. A bracketed value that is not valid IPv6 (`[[::1]]`, `[property.example]`, `[127.0.0.1]`, `[]`) is refused as internal.
   - Every hostname classification path goes through `isInternalHost`, so this one change covers all of them:
     - `assertSafeTargetUrl` (`api.ts:~96`) for the /scrape prepare step, the per-request route guard, and the /browser-batch-fetch bootstrap;
     - the SSRF proxy `prepareRequestFunction` (`api.ts:~119`). proxy-chain also derives `hostname` with WHATWG URL (`new URL("connect://" + target)`), so it also keeps the brackets.
   - The other hostname uses in `api.ts` (the ad-domain filter at `~339`) and the C10 files (`allowedHost` equality checks) do not classify IPs.
   - Tests: `scrape_lifecycle.test.ts:375`. They build hosts with `new URL(u).hostname` for `[::1]`, `[::ffff:7f00:1]`, `[::ffff:127.0.0.1]` and `[fd00:ec2::254]`, which must be blocked. They check that `[2606:4700:4700::1111]` is public, with a resolver that throws if DNS is called.
   - Mutation check: reverting `target_dns.ts` to HEAD~ fails exactly this test.

2. **Deadline sometimes returns 500 instead of 504: FIXED (both suggested approaches).**
   - `scrape_lifecycle.ts:42`: `PLAYWRIGHT_DEADLINE_GRACE_MS = 250`. `api.ts:926` passes `remainingMs() + PLAYWRIGHT_DEADLINE_GRACE_MS` to `scrapePage`, which covers `page.goto` and `waitForSelector`.
   - `scrape_lifecycle.ts:~150-162`: in `bounded()`, a work-phase error raised at or after `deadlineAt` maps to `ScrapeDeadlineError("work")`. `ScrapeDeadlineError`, `ScrapeClientGoneError` and `ScrapeResourceLeakError` are excluded, so leak quarantine still works. The mapping applies to the work phase only, so admission-phase security and DNS outcomes are not relabeled.
   - Tests:
     - `scrape_lifecycle.test.ts:398`: 10 runs where a Playwright-like operation times out at remaining + grace.
     - `scrape_lifecycle.test.ts:419`: deterministic. It starves the timer with a busy-wait, then throws. Removing the mapping fails exactly this test.

3. **Redirect-hop DNS failures: DOCUMENTED.**
   - `README.md:76-82`: `TARGET_DNS_UNAVAILABLE` covers only the requested URL. A DNS failure on a later hop is refused by the SSRF proxy (502) and surfaces as an ordinary navigation error. The hop is never fetched.
   - `README.md:54-60` also documents the caps, the grace, and IPv6 blocking (table row).

4. **timeout overflow above 2^31-1: FIXED.**
   - `scrape_lifecycle.ts:50`: `MAX_SCRAPE_INPUT_MS = 86_400_000` (24 h).
   - `scrape_lifecycle.ts:64`: `parseScrapeTiming()` replaces the inline checks in `api.ts:842`. Values above the cap return 400 for `timeout` and for `wait_after_load`. Defaults and the null-means-omitted behavior are unchanged.
   - The apps/api check:
     - v1 and v2 `timeout` is `z.int().positive().min(1000)` with **no max** (`apps/api/src/controllers/v2/types.ts:697`, `v1/types.ts:472`).
     - `waitFor` has `max(60000)` (`v2/types.ts:698`, `v1/types.ts:473`).
     - The playwright engine sends `meta.abort.scrapeTimeout()`, the remaining budget.
   - 24 h is far above any real scrape, and the API itself would overflow `setTimeout(timeout*0.667)` above about 2^31 ms. Strictly speaking, a user `timeout` above 24 h would now get 400 from the sidecar.
   - Test: `scrape_lifecycle.test.ts:436`.

5. **Fragile 10-15 ms margins: DONE (cheap widening).**
   - In `scrape_lifecycle.test.ts`:
     - the 9 `Date.now() + 10` deadlines became +50;
     - the queue-budget test now uses 200/50/300 ms instead of 80/40/60;
     - the pacing gap test uses 40 ms with an assertion of >= 38 (was 15/14);
     - pacing admission uses a 200 ms pacer and tick(220) (was 60/70);
     - mixed outcomes uses 60 ms and tick(30);
     - delivery uses tick(25).
   - Route-level tests for the `api.ts` status mapping (part of review item 5) were **not** added. `api.ts` starts the server on import. The new `parseScrapeTiming` gives unit coverage of the 400 path only.

### Last verification (in the worktree `apps/playwright-service-ts`)
- `npx tsc --noEmit -p .`: OK.
- `pnpm build` (tsc): OK.
- `pnpm test`: **87 tests, 87 pass, 0 fail**. This was 83 before; there are 4 new tests. It was run 6 times in a row, all green.
- Loopback smoke (`scratchpad/pr43-port-smoke/run2.sh`, output in `run2.out`). It used `node dist/api.js` on 39171 (guarded) and 39172 (`ALLOW_LOCAL_WEBHOOKS`), plus a python server on 127.0.0.1:39181. No external sites.

| Case | Result |
| --- | --- |
| `.invalid` DNS | 503 `TARGET_DNS_UNAVAILABLE` |
| 127.0.0.1 | 200 with pageStatusCode 403 |
| `/scrape` with `[::1]`, `[::ffff:127.0.0.1]`, `[::ffff:7f00:1]`, `[fd00:ec2::254]` | each 200 with pageStatusCode 403 (was 503) |
| `/browser-batch-fetch` with the same four hosts | each 400 "resolves to a private/internal address" |
| timeout=0 | 400 |
| timeout=86400001 | 400 |
| timeout=2147483648 | 400 |
| wait_after_load=86400001 | 400 |
| Happy path | 200 |
| Slow page, 1.5 s timeout, 8 runs | 8/8 504 `SCRAPE_WORK_TIMEOUT` |
| Missing check_selector, 1.5 s timeout, 5 runs | 5/5 504 |
| Admission queue | 503 `SCRAPE_ADMISSION_TIMEOUT` (holder 504) |
| Happy path again | 200 |
| /health | activePages 0 |
| `SCRAPE_START_INTERVAL_MS=9999` | exit 1 |

- Pacer smoke (`pacer.sh`, output in `pacer2.out`):
  - three scrapes finished at 0.16 s, 1.06 s and 2.07 s;
  - a paced request with a 300 ms deadline got 503 admission;
  - activePages was 0 afterward.
- `probe_dns.ts` re-run: every bracketed private literal returns `internal=true`, and `[2606:4700::1111]` returns `internal=false`, with no throw.
- The public IPv6 literal was unit-tested only. It was not smoke-tested, because a public classification would connect externally.

### What's left
1. Optional: add route-level tests for the `api.ts` status mapping. That needs an `app` export or a refactor so that importing `api.ts` does not call `start()`.
2. Optional: run `pnpm knip` and prettier. Prettier is not installed in this worktree; long lines were hand-wrapped to match the house style.
3. Re-run the adversarial review on 448656a21, then merge per `policies/git.md`.
4. After merge, rebuild the `playwright-service` image (and the C10 image) for the change to take effect.

### Gotchas
- WHATWG URL keeps the brackets on IPv6 `hostname`. This applies to non-special schemes too (proxy-chain's `connect://`). The fix in `isInternalHost` is therefore the single choke point. Do not add per-caller stripping.
- `new URL("http://[::ffff:127.0.0.1]/").hostname` normalizes to `[::ffff:7f00:1]`.
- The sidecar ignores SIGTERM on macOS. The smoke `trap ... kill` left one pacer sidecar running, which I killed with `kill -9`. Use `kill -9` in smoke cleanup.
- Within 24 h, the post-deadline mapping turns a work-phase InsecureConnectionError raised after the deadline into a 504. This matches what the lifecycle timer already did when it won the race. The request is refused either way.
- Separately from this PR (from the review): the C10 listener passes `allowTestLocalTargets` as `createContext`'s `skipTlsVerification` argument. Not touched here.
