# C10 Phase 4: multi-brokerage live read-only calibration (2026-09-16)

## Scope and boundary

Phase 3 established that the JLL-only C10 admission lane cannot run safely on
this Mac (`docs/logs/subagents/2026-09-16/c10-live-calibration/phase3-jll-live-run.md`):
`fcntl.flock` does not coordinate across the macOS host / OrbStack container
boundary, so the Linux runner cannot safely share the canonical `SharedLock`
with host collectors. This phase does not use the admission lane, the Linux
runner, or any P0/P1 arm. It runs a bounded, sequential, polite, read-only
live calibration across a broad group of brokerages, using only the existing
`collect.ts` CLI, to (a) gather live operating data across sources rather than
JLL alone, and (b) look for places the C10 capacity design is overfit to JLL
in ways the data can justify generalizing.

No database, cache, listing-table, scheduler, launchd, authority-pin, or
runtime-container write occurred. `cre_capacity_runtime.py apply/rollback`
was never invoked and no `firecrawl-*` container was recreated.

## Survey: what exists and what is actually runnable read-only

| Path | Verdict |
|---|---|
| `capacity_c10/` (`admission.py`, `production.py`, `jll_admission.py`, `host_*.py`, `runner.py`, `compare.py`, `policy.py`, `authority.py`) | **Not usable for live calibration.** By explicit design (`capacity_c10/README.md`), this is an offline, sealed admission/evidence protocol. No component makes a live provider request on its own; `production.py` requires the canonical `SharedLock`, a built `firecrawl-playwright-service-c10:local` image, and P0/P1 runtime preflight — all out of scope here. |
| `capacity_c10/inventory/*.py`, `capacity_c10/strict_detail_*.py`, `capacity_c10/receipts/*.ts` | **Not usable.** Explicitly "sealed-evidence validators" / "no-write request descriptors," `fully_verified=False`, no concrete transport wired in. Reviewing them (task 1) confirmed they do not fetch. |
| `cre_capacity_experiment.py`, `cre_capacity_multisource_v1.py`, `cre_capacity_benchmark.py` | **Not usable for live requests.** These are offline planners/cohort binders over already-collected receipts (`cre_capacity_multisource_v1.py`'s own docstring: "does not scrape, invoke an adapter, or start an experiment"). Useful only as *reference* for how the 20-source panel is already parameterized (see Overfit analysis). |
| `platform_access_probe.py` / `cre_access_matrix.py` | Historical probe scripts against `/v2/scrape`; superseded by the production `collect.ts` adapters per `docs/firecrawl-ops/references/cre-access-matrix.md`. Not adapter-aware; skipped in favor of the real adapters. |
| **`collect.ts --source=<key> --transaction=sale --max-items=8 --concurrency=1 --out=<path>`** | **Used.** This is the collector's own supported, read-only, no-DB-write path (`cre_collector/CLAUDE.md` "Quick start" gives this exact shape). It calls each source's real production adapter in `sources/*.ts`, writes a JSON artifact only, and never touches `cre_ingest.py`, the canonical `.cre.lock`, or any container. Default per-request timeout is already 90000ms (`lib/scrape.ts`), matching the requested bound. |

No new framework was built. A small bash driver (not committed; ran from the
scratchpad) invoked `collect.ts` once per brokerage, sequentially, with a 5s
gap between brokerages and `--concurrency=1` within each. Raw artifacts and
logs are under `tasks/tmp/c10-live-calibration/phase4/` (gitignored, verified
with `git check-ignore`).

Brokerages covered: CBRE, Cushman & Wakefield, Colliers (`colliers-main`),
Newmark, Marcus & Millichap, Avison Young, JLL, plus SVN (Buildout) as the
"any other supported source." Each run: 1 enumeration pass + `--max-items=8`
detail fetches, `--concurrency=1` (never parallel), 90s per-request timeout
(adapter default), `--transaction=sale` only (to keep the run inside the
requested 1+8 budget per brokerage rather than doubling it for lease).

## Per-brokerage results

| Brokerage (source key) | Reachable | Enumeration total | Detail success | Wall time (1 enum + 8 detail) | Response shape | Challenge/anti-bot signal |
|---|---|---:|---|---:|---|---|
| CBRE (`cbre`) | Yes | 6,155 (1 page) | 8/8 | 5s | JSON API | None |
| Cushman & Wakefield (`cushman-wakefield`) | Yes | 2,874 | 0/8 pre-fix (false challenge); 8/8 after fix | 69s | JSON API (list) + HTML (detail) | **8/8 detail fetches tripped the `assertCushmanDetailDoc` challenge regex** (see Overfit finding below) |
| Colliers (`colliers-main`) | Yes | 15,985 (sitemap) | 8/8 fetched, 0 errors, 0 deferred; collapsed to 3 unique listings | 73s | XML sitemap + HTML detail (`RealEstateListing` JSON-LD) | None; runtime canary passed |
| Newmark (`newmark`) | Yes | 1,308 | 8/8 | 5s | Algolia-style JSON | None |
| Marcus & Millichap (`marcus-millichap`) | Yes | 3,159 | 8/8 | 8s | JSON (map API) + HTML detail | None |
| Avison Young (`avison-young`) | Yes | 752 (from a cached 2,152-row SharpLaunch feed) | 8/8 | 59s | JSON feed + HTML detail | None |
| JLL (`jll`) | Yes | 1,854 (9 property-type GraphQL filters) | 8/8 | 19s | GraphQL + `__NEXT_DATA__` HTML detail | None |
| SVN (`svn`, Buildout) | Yes | 5,572 (186 enumeration pages) | 8/8 | 166s (dominated by paginated enumeration, not detail) | Buildout `inventory.json` | None |

Body sizes: artifacts ranged from 12.8 KB (svn, thin Buildout fields) to
314 KB (avison-young) and 288 KB (colliers-main, richer HTML-derived fields).
No BOM or non-UTF-8 encoding issue was observed in any of the 8 brokerages'
JSON output; `lib/scrape.ts`'s `parseJsonBody` calls `JSON.parse` directly
with no BOM-stripping, which is a latent risk only if a provider ever emits a
BOM-prefixed body (see Overfit analysis; not currently justified by data).

Raw artifacts/logs: `tasks/tmp/c10-live-calibration/phase4/{source}.json`,
`{source}.log`, `manifest.jsonl`, `driver.log` (gitignored, not committed;
metrics only, no page bodies reproduced here or in git).

## Overfit finding (data-justified fix applied)

**Cushman & Wakefield's detail-page challenge regex false-positives on every
live sale detail page.** `sources/cushman-wakefield.ts`'s
`assertCushmanDetailDoc` used:

```
/just a moment|checking your browser|verify you are human|captcha|access denied|cf-chl-|.../i
```

Live calibration hit this 8/8 times. Direct probe of one flagged URL
(`https://www.cushmanwakefield.com/.../7190-watkins-road/...`) via the local
Firecrawl API confirmed the page is a normal 195 KB property detail page: the
"captcha" match came from the page's ordinary "Request Info" lead-gen form,
which embeds a Google reCAPTCHA v2 widget
(`gstatic.com/recaptcha/...`, `fxt-captcha`, `g-recaptcha-response`) on every
property page, not an anti-bot challenge shell.

This is exactly the same class of bug `sources/hanley.ts` already fixed for
its own source (`hanleyChallenge`'s comment: "Legitimate Hanley pages load
Google reCAPTCHA scripts, so bare 'captcha' text is not challenge evidence").
Cushman inherited the naive bare-word pattern instead. The listings were not
lost in this run (API-level fields still populate the row; only detail-page
enrichment silently failed 8/8 times), but this materially degrades detail
completeness for every Cushman & Wakefield sale run.

**Fix applied** (`sources/cushman-wakefield.ts`): dropped bare `captcha` from
the regex, keeping only markers that cannot appear on an ordinary page
(`just a moment`, `checking your browser`, `verify you are human`,
`access denied`, `cf-chl-`, the 404/500/503 markers). This mirrors Hanley's
existing fix shape rather than inventing new machinery. JLL's own challenge
handling was not touched (JLL uses `__NEXT_DATA__`/GraphQL-shape checks, not
this regex, and is unaffected).

**Test added** (`tests/ts/sources/cushman-wakefield.test.ts`): a new case
reproducing the exact live HTML pattern (reCAPTCHA script tag + `fxt-captcha`
field + `g-recaptcha-response` input on an otherwise normal property page)
and asserting `assertCushmanDetailDoc` no longer throws. The existing
"rejects challenge, error, and wrong-property shells" test (which does not
rely on bare `captcha`) still passes unchanged, so real Cloudflare/"Just a
moment" challenge detection is unaffected.

**Same-pattern risk documented, not changed (no live data yet):** the
identical bare `/captcha/i` pattern also exists in `sources/avison-young.ts`,
`sources/marcus-millichap.ts`, `sources/transwestern.ts`,
`sources/daum-commercial.ts`, `sources/foundry-commercial.ts`,
`sources/lyon-stahl.ts`, and `sources/pyramid-brokerage.ts`. None of these
tripped in this run's live sample (avison-young and marcus-millichap
detail-enriched 8/8 with no failures), so there is no live evidence yet that
they have the same lead-form-with-reCAPTCHA shape as Cushman. Flagging for a
future calibration pass rather than making a blind fix across seven files
without reproduction.

## Other overfit-to-JLL review (task 3)

Reviewed every C10 admission-lane hard-coded `"jll"` branch
(`jll_admission.py`, `admission_controller.py`, `host_registry.py`,
`strict_detail_jll.py`) and the fixed member count (`JLL_MEMBER_COUNT = 16`).
**No change made.** These are not accidental overfitting: the admission lane
is explicitly, deliberately scoped to JLL only and separate from the 20-source
panel by design —
`docs/firecrawl-ops/c10-jll-admission-lane.md`: "This lane is a separate,
JLL-only experiment contract... A 20-source authority cannot authorize this
lane and a JLL authority cannot authorize the panel." Generalizing the
admission lane's selection rule, member count, or `sourceKey == "jll"` checks
would contradict its own documented threat model, not fix a bug.

Separately, the **20-source compatibility layer is already generalized**, not
JLL-specific: `cre_capacity_multisource_v1.json` parameterizes
`calibration_per_source` (8), `core_per_source` (24), `provider_family`,
`hosts`, `exclusive`, and `not_found_classifier` per source, and
`capacity_c10/adapters.py`'s `candidate_registry()` already registers
per-source candidate adapters (JLL, JLL Investor, Colliers, Colliers Main,
Marcus & Millichap, Avison Young) rather than hard-coding JLL paths into
shared logic. So the "member count 16," "challenge regex," and "selection
rule" items named in the task prompt as things to check for JLL-overfit are,
on inspection, either (a) deliberately JLL-scoped admission-lane internals
(no fix warranted) or (b) already parameterized per-source at the
`cre_capacity_multisource_v1` config layer (no fix needed). The one concrete,
live-data-justified generalization opportunity found was the challenge-regex
false positive above, which is not part of the C10 admission-lane code at all
— it lives in the ordinary `sources/*.ts` collector adapters shared by every
brokerage.

BOM handling: the only BOM-tolerant decode path in the repo is the narrow
JLL admission child's enumeration-body decoder
(`tests/ts/capacity_c10/jll_admission_receipts.test.ts`). The shared
`parseJsonBody` in `lib/scrape.ts` (used by every `sources/*.ts` adapter,
including JLL's own regular collector adapter) has no BOM stripping at all.
This is not JLL-overfitting in the sense of something that should generalize
*from* JLL — it's an admission-lane-only feature that happens not to exist in
the main pipeline for any source. No live brokerage in this run emitted a
BOM-prefixed body, so there is no data to justify adding BOM tolerance to
`parseJsonBody`; noting it as a latent gap for a future calibration run that
happens to hit a provider that does.

## Gates run

From `cre_collector/`:

- `npx tsc --noEmit` — clean, no output.
- `npm run test:unit` — 978 tests, 977 passed, 1 skipped (pre-existing skip,
  unrelated to this change), 0 failed.
- `python3 -m pytest tests/ -q -p no:cacheprovider` — 3473 tests, 3472 passed,
  1 skipped (pre-existing), 0 failed.
- No Python files were changed, so `uvx ruff check` / `uvx ruff format
  --check` were not required; not run.
- `apps/playwright-service-ts` was not touched; its gates were not run.

Verified `out/daily/.cre.lock*` file sizes and mtimes were identical before
and after the full run (`collect.ts` never opens the canonical lock; only
`cre_checkpoint_refresh.SharedLock` does, and that path was never invoked).

## Remaining risks / not done

- Only `--transaction=sale` was calibrated per brokerage (to keep each
  brokerage inside the requested 1 enum + 8 detail budget); lease-side
  challenge/latency behavior was not separately sampled.
- The seven other sources sharing Cushman's bare-`captcha` pattern
  (avison-young, marcus-millichap, transwestern, daum-commercial,
  foundry-commercial, lyon-stahl, pyramid-brokerage) were not fixed —  no live
  reproduction was captured for them in this run, and a blind regex change
  across seven files without reproduction would risk silently weakening real
  challenge detection.
- `sources/matthews.ts` has the same class of risk in a different shape: a
  bare `\bg-recaptcha\b` check (alongside its own bare `\bcaptcha\b`) that
  would false-positive on an ordinary reCAPTCHA-bearing lead-gen form the
  same way Cushman's did. Matthews was not part of this run's live sample,
  so there is no reproduction to fix against; flagging for the same future
  sampling pass rather than a blind change.
- No latency percentile (p50/p90) breakdown per individual HTTP request is
  reported: `collect.ts` does not emit per-request timestamps to stdout, so
  the wall-clock durations above are aggregate (1 enumeration pass + up to 8
  detail fetches) rather than per-request. A future pass could add
  `--verbose`-style per-request timing to `lib/performance.ts` if finer
  latency data is wanted.
- JLL admission-lane live execution remains blocked by the Phase 3 host/
  container `flock` boundary issue; unrelated to this phase and not
  attempted here.
