# C10 live calibration, overall summary (2026-09-16)

Overall summary of the 2026-09-16 C10 live-calibration branch
(`fix/c10-live-calibration`). Links the phase docs rather than repeating
their detail:

- `docs/firecrawl-ops/phase1-budgets-merge.md` (Phase 1: budgets, PR #72)
- `docs/firecrawl-ops/c10-live-calibration-phase2-2026-09-16.md` (Phase 2: image + runner)
- `docs/firecrawl-ops/c10-live-calibration-jll-run-2026-09-16.md` (Phase 3: JLL admission attempt)
- `docs/firecrawl-ops/c10-live-calibration-multibrokerage-2026-09-16.md` (Phase 4: multi-brokerage calibration)
- `docs/logs/subagents/2026-09-16/c10-live-calibration/*.md` (per-phase subagent logs)

## Phase 1: budgets

PR #72 merged as `f965c5f3b`. Budgets, old -> new:

| Budget | Old | New |
|---|---:|---:|
| Startup/health | 180s | 600s |
| Per-request (Python + TS, incl. `receipts/strict_detail/jll.ts`) | 30s | 90s |
| Collection margin | 60s | 180s |
| Collection total (derived 17x90+180) | 570s | 1710s |
| Teardown | 60s | 180s |
| Readiness poll | 60s | 180s |
| P0/P1 lifecycle deadline | 120s | 600s |
| Worst-case total | 815s | 2495s |

Unchanged: 8 MiB reply cap (not time-based), 120s capability expiry cap
(still exceeds the new 90s request budget), 5s child reap.

## Phase 2: image rebuild

Both `firecrawl-playwright-service-c10:local` (the C10 sidecar) and
`firecrawl-c10-linux-runner:local` (the generic Linux runner added this
phase) were rebuilt from the reviewed checkout. All `c10_linux_preflight.py`
checks passed (`linux_platform`, `private_receipt_store`, `docker_socket`,
`c10_image_present`, `compose_config_renders`; a sixth,
`canonical_lock_domain`, was added this finalization pass -- see
"Generalization changes" below).

## JLL live admission: blocked, no request made

**No live network request against JLL was made anywhere in this branch.**
The bounded admission action never reached sidecar startup:

1. **Lock residue from a leaked test.** A pre-existing historic pytest
   quarantine residue in `out/daily/.cre.lock` / `.cre.lock.authority`
   (unrelated to this branch's code) blocked `lock.acquire()`. The
   orchestrator recreated `firecrawl-playwright-service-1` from the repo's
   governed `docker-compose.yaml` baseline (16 GiB mem/memswap, 8 GiB shm; it
   had drifted to 4 GiB memory / 15.7 GiB shm) so its live cgroup limits
   would match the recovery tool's validation profile, then ran
   `cre_capacity_runtime.py recover-quarantine --execute`. The first attempt
   failed pre-claim with `CpuTelemetryError: Darwin CPU tick counters did not
   advance` (a plausible sampler flakiness defect, not fixed this session --
   see "Next gated steps"); a bare retry of the identical command completed
   (`phase: "completed"`, archive
   `out/daily/.cre-quarantine-forensics/e68ea0ebacf8ab39237934443b13bdcfc6cd48bdf853ead2015d3dc1f92e5ba5`,
   receipt sha256
   `b5544a427bbc01b5f7d3c0259afff3727ed58dd7d3872e64b0548baa5d3b7692`).
2. **Cross-mount identity mismatch.** With the lock clear, the live-run
   attempt's recovery-guard identity check failed because the archived
   evidence's absolute paths and `(st_dev, st_ino)` pair (captured on the
   macOS host) do not survive OrbStack's virtiofs bind mount into the Linux
   runner container -- correctly fail-closed, not a bug in the identity
   check.
3. **The real, root-cause blocker: `fcntl.flock` does not coordinate across
   the macOS host / OrbStack container boundary.** A host process and a
   container process were both observed holding `LOCK_EX|LOCK_NB` on the
   same file at once. This makes the generic Linux runner unsafe for holding
   the canonical CRE lock on this Mac, independent of whether the
   cross-mount identity check is ever relaxed. See
   `docs/firecrawl-ops/c10-live-calibration-jll-run-2026-09-16.md` for the
   full finding and `LOCK_AUTHORITY_RECOVERY.md` for the lock protocol.

## Per-brokerage results (Phase 4, read-only, `collect.ts` only)

Phase 4 ran a bounded, sequential, polite, read-only calibration across
eight brokerages using only the collector's existing supported no-DB-write
path (`collect.ts --source=<key> --transaction=sale --max-items=8
--concurrency=1`), never the admission lane or the Linux runner. No
database, cache, listing-table, scheduler, launchd, authority-pin, or
runtime-container write occurred.

| Brokerage (source key) | Reachable | Enumeration total | Detail success | Wall time (1 enum + 8 detail) | Response shape | Challenge/anti-bot signal |
|---|---|---:|---|---:|---|---|
| CBRE (`cbre`) | Yes | 6,155 (1 page) | 8/8 | 5s | JSON API | None |
| Cushman & Wakefield (`cushman-wakefield`) | Yes | 2,874 | 0/8 pre-fix (false challenge); 8/8 after fix | 69s | JSON API (list) + HTML (detail) | 8/8 detail fetches tripped the `assertCushmanDetailDoc` challenge regex (fixed, see below) |
| Colliers (`colliers-main`) | Yes | 15,985 (sitemap) | 8/8 fetched, 0 errors, 0 deferred; collapsed to 3 unique listings | 73s | XML sitemap + HTML detail (`RealEstateListing` JSON-LD) | None; runtime canary passed |
| Newmark (`newmark`) | Yes | 1,308 | 8/8 | 5s | Algolia-style JSON | None |
| Marcus & Millichap (`marcus-millichap`) | Yes | 3,159 | 8/8 | 8s | JSON (map API) + HTML detail | None |
| Avison Young (`avison-young`) | Yes | 752 (from a cached 2,152-row SharpLaunch feed) | 8/8 | 59s | JSON feed + HTML detail | None |
| JLL (`jll`) | Yes | 1,854 (9 property-type GraphQL filters) | 8/8 | 19s | GraphQL + `__NEXT_DATA__` HTML detail | None |
| SVN (`svn`, Buildout) | Yes | 5,572 (186 enumeration pages) | 8/8 | 166s (dominated by paginated enumeration, not detail) | Buildout `inventory.json` | None |

Full detail, body sizes, and raw-artifact paths:
`docs/firecrawl-ops/c10-live-calibration-multibrokerage-2026-09-16.md`.

## Generalization changes

- **Cushman captcha false positive fixed.** `sources/cushman-wakefield.ts`'s
  `assertCushmanDetailDoc` matched bare `captcha`, which false-positives on
  the ordinary Google reCAPTCHA widget every Cushman property page's
  "Request Info" lead-gen form embeds. Dropped bare `captcha` from the
  regex, keeping only markers that cannot appear on an ordinary page (`just
  a moment`, `checking your browser`, `verify you are human`, `access
  denied`, `cf-chl-`, the 404/500/503 markers) -- mirroring the existing
  `sources/hanley.ts` fix shape. Test added in
  `tests/ts/sources/cushman-wakefield.test.ts`.
- **Seven other sources share the same bare-`/captcha/i` pattern**
  (`avison-young`, `marcus-millichap`, `transwestern`, `daum-commercial`,
  `foundry-commercial`, `lyon-stahl`, `pyramid-brokerage`), plus
  **`sources/matthews.ts` has the same class of risk in a bare
  `\bg-recaptcha\b` check**. None of these tripped in Phase 4's live sample
  (avison-young and marcus-millichap detail-enriched 8/8), so none were
  changed blind; flagged for a future live-sampling pass with reproduction
  before any fix.
- **JLL-only admission constants are intentional, not overfit.** The C10
  admission lane's hard-coded `"jll"` branches and `JLL_MEMBER_COUNT = 16`
  are a deliberate, separately documented JLL-only experiment contract
  (`docs/firecrawl-ops/c10-jll-admission-lane.md`), distinct from the
  already-generalized 20-source panel (`cre_capacity_multisource_v1.json`
  parameterizes per source; `capacity_c10/adapters.py`'s
  `candidate_registry()` already registers multiple per-source adapters).
  No change made.
- **New fail-closed lock-domain guard** (this finalization pass):
  `CRE_LOCK_DOMAIN_UNTRUSTED` is now set on the generic Linux runner
  service; `cre_checkpoint_refresh.SharedLock.acquire` raises `LockHeldError`
  whenever it is set, before any filesystem mutation; and
  `c10_linux_preflight.py` gained a sixth `canonical_lock_domain` check that
  fails whenever it is set, so preflight can never report `ok` while the
  runner is unsafe for lock-holding live work.

## Test-isolation guard

`tests/conftest.py`'s session-wide real-canonical-lock guard
(`_never_touch_real_canonical_cre_lock`) now attributes a detected real-lock
change to its owner pid (read from the authority record) before failing:
if that pid is not the pytest process or a descendant of it (e.g. a
legitimate external owner such as a launchd tier-dispatch run touching the
lock concurrently), the guard warns instead of failing the test. An
unattributable change (owner pid unreadable, or the change happened without
a readable authority record) still fails closed. The guard's `git
rev-parse`-derived real-lock-dir resolution also now falls back to the path
relative to the collector dir (`out/daily/.cre.lock`) if `git` is
unavailable at collection time, rather than aborting the whole test session;
verified to equal the git-derived path on this checkout.

Two import-time module constants (`cre_repair_cushman_identity.py`,
`cre_repair_newmark_nim.py`: `DEFAULT_LOCK = canonical_shared_lock_dir()`)
were converted to a lazy `default_lock()` function, so a test-time patch of
the module's `canonical_shared_lock_dir` binding actually takes effect
(previously the constant resolved the real production lock path at import
time, before any test guard could patch it).

## Remaining risks

- The CPU-idle sampler flakiness (`CpuTelemetryError: Darwin CPU tick
  counters did not advance`) that failed the first `recover-quarantine
  --execute` attempt is unfixed; a bare retry worked, but this is a
  candidate defect for a separately reviewed fix (see "Next gated steps").
- The cross-mount identity mismatch in the archived-evidence check
  (`(st_dev, st_ino)` not preserved across the OrbStack bind mount) is
  unfixed and correctly fails closed; it is now known to be a secondary
  symptom of the flock non-coordination, not the primary blocker.
- Seven `sources/*.ts` files plus `matthews.ts` share Cushman's bare-word
  challenge-regex risk shape and are unverified against live data.
- Lease-side challenge/latency behavior was not sampled in Phase 4 (sale
  only, to stay inside the 1 enum + 8 detail budget per brokerage).
- No per-request latency percentile breakdown; Phase 4's wall-clock numbers
  are aggregate per brokerage.

## Next gated steps

1. **Lock-domain decision (operator/founder gated).** Before any further
   live JLL admission attempt: choose a real Linux host, a host-side lock
   broker, or moving every lock holder into one kernel (see
   `c10-live-calibration-jll-run-2026-09-16.md` "Recommended next step").
   `CRE_LOCK_DOMAIN_UNTRUSTED` now blocks the unsafe path in code either
   way.
2. **The authority pin PR.** Once a trusted lock domain exists,
   `build-jll-bundle` / `render-jll-authority` / a separately reviewed pin PR
   remain the only path to install a JLL admission authority; still not
   attempted.
3. **A P0 dry run**, gated on the authority pin PR merging and explicit
   operator approval for P0/P1 arming (unchanged from prior phases'
   restrictions).
4. **The `CpuTelemetryError` sampling defect candidate**: a separately
   reviewed fix to `cre_capacity_runtime.py`'s CPU-idle sampler so a single
   non-advancing Darwin tick-counter read is retried with a widened window
   instead of raising, reducing operator friction the next time recovery
   must run on macOS.
