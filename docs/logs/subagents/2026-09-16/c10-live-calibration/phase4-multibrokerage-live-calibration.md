# Phase 4: multi-brokerage live read-only calibration

Outcome: COMPLETED. No admission lane, Linux runner, DB write, or runtime
container mutation used. Full results, per-brokerage table, and overfit
analysis: `docs/firecrawl-ops/c10-live-calibration-multibrokerage-2026-09-16.md`.

Summary:

1. Surveyed `capacity_c10/`, `cre_capacity_multisource_v1.py`,
   `cre_capacity_experiment.py`, `cre_capacity_benchmark.py`, and the probe
   scripts. All of `capacity_c10/` is a sealed, offline, no-write admission
   protocol by design (`capacity_c10/README.md`) — none of it fetches live
   data. The only usable existing read-only path is the collector's own
   `collect.ts --source=<key> --transaction=sale --max-items=8
   --concurrency=1 --out=<path>` (documented in `cre_collector/CLAUDE.md`
   Quick start), which never calls `cre_ingest.py` and never opens the
   canonical `.cre.lock`.
2. Ran that path sequentially (5s gap between brokerages, `--concurrency=1`
   within each) against CBRE, Cushman & Wakefield, Colliers (`colliers-main`),
   Newmark, Marcus & Millichap, Avison Young, JLL, and SVN. Raw artifacts/logs
   under `tasks/tmp/c10-live-calibration/phase4/` (gitignored).
3. Found and fixed a live, data-justified bug: Cushman & Wakefield's detail
   challenge regex matched bare `captcha` and false-positived 8/8 times on the
   live sample against ordinary property pages carrying a Google reCAPTCHA
   "Request Info" form widget (confirmed via direct raw-HTML probe of a
   flagged URL). Fixed in `sources/cushman-wakefield.ts`
   (`assertCushmanDetailDoc`) to match Hanley's existing fix shape; added a
   regression test reproducing the exact live HTML pattern.
4. Reviewed the C10 admission lane's `"jll"`-specific branches and
   `JLL_MEMBER_COUNT = 16`. These are intentionally JLL-only by the lane's own
   documented design (`docs/firecrawl-ops/c10-jll-admission-lane.md`); no
   change made. The 20-source compatibility layer
   (`cre_capacity_multisource_v1.json`) is already source-parameterized, not
   JLL-hard-coded.
5. Gates: `npx tsc --noEmit` clean; `npm run test:unit` 977/978 pass (1
   pre-existing skip); `python3 -m pytest tests/ -q -p no:cacheprovider`
   3472/3473 pass (1 pre-existing skip). `out/daily/.cre.lock*` file sizes/
   mtimes unchanged before/after.

No provider request was made outside the 8 calibrated brokerages. No JLL
admission-lane or P0/P1 execution was attempted; the Phase 3 host/OrbStack
`flock` boundary blocker for that lane is unrelated to this phase and remains
open.
