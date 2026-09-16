# Phase 2 live-readiness fixes: C10 JLL controller bridge (PR #72)

- Branch: `feat/c10-jll-controller-bridge`. Reviewed head: `349a26d8a`.
- Fix commits: `39962fad1` (sidecar teardown, readiness, GraphQL errors),
  `a1f53bf25` (controller completion attestation, child reaping, collection
  bound), `44584a307` (admission lock, budgets, quarantine).
- Method: an independent Opus review of `349a26d8a` found the items below. The
  orchestrator checked each one against the code and designed the fixes. Four
  Sonnet workers wrote tests, each owning separate files. No provider request,
  root provisioning, Docker run against real services, DB/cache/scheduler write,
  merge, or ready-for-review happened.

## Findings

| # | Claim | Verdict | Fix | Tests |
|---|---|---|---|---|
| P1-a | A timeout leaks the lane sidecar | Confirmed. `stop(deadline)` raised in `_remaining` before `rm`, then cleared the env and project | `39962fad1`: teardown has its own 60 s budget with 3 retries and ignores the run deadline. `compose_project` is kept. An unproven cleanup stays reported on later stops. `44584a307`: if teardown cannot be proven, the lock is retained with its armed benchmark marker, a ledger record `.cre-c10-ledger-v1/jll-admission-<run>.quarantine` names the project, and the receipt root gets `jll-admission-quarantine.json` | `test_capacity_c10_sidecar_ipc.py` (expired deadline still runs `rm`/`ps`, retry, sticky unproven), `test_capacity_c10_jll_admission_production.py` (real `DockerComposeSidecar` still runs `rm`/`ps` after the controller deadline expires; quarantine branches, including the three-way note propagation) |
| P1-b | No sidecar readiness wait | Confirmed | `39962fad1`: shared `_poll_health` retries only connection refused or reset for up to 60 s, bounded by the run deadline. Any HTTP response goes straight to the unchanged strict signed verification | host orchestration: refused then ready, reset then ready, 503 fails after one request, bad signature fails after one request, bounded never-ready; loopback test calls `_verify_health` right after spawn with no ready-line wait |
| P2 | A 120 s total cap is too small | Plausible, accepted | Separate budgets: startup and health 180 s; collection 570 s (17 cards at 30 s each, sequential at capacity one and enforced by the sidecar including queue time, plus 60 s); teardown 60 s. Documented upper bound: 810 s. A run requires the prebuilt image `firecrawl-playwright-service-c10:local` (`--no-build --pull never`, image checked in the rendered config). This prebuilt image is required because every run uses a random compose project name, so without a pinned tag compose would build a new image inside each bounded run (P0/P1 had the same problem) | timeout bound tests in production and controller files |
| P2 | Offline builder accepts a root the controller rejected | Confirmed (reviewer probe reproduced) | `a1f53bf25`: the controller seals `jll-admission-completion-<sha>.sealed` only after verification. It binds the manifest artifact bytes, manifest digest, selection, adapter, session and run digests. The child cannot seal that stem. `build_jll_bundle`, `render_jll_authority` and `render_jll_plan` each require exactly one valid completion and refuse quarantined roots | reviewer probe as a test, plus copied, duplicate, tampered, name-mismatch, quarantine and reserved-stem tests |
| P3 | Adapter digest not checked up front | Confirmed | `44584a307`: rejected against `repository_implementation_sha256("jll")` before the lock or any sidecar work | production test |
| P3 | Killed child not reaped | Confirmed | `a1f53bf25`: `_reap_child` kills if the child is still running, waits up to 5 s, and closes the pipes | real child reap test |
| P3 | No P0/P1 exclusion | Confirmed. The canonical `SharedLock` exists and fits | `44584a307`: admission acquires the canonical lock and arms the benchmark marker (the crash boundary), then disarms only after teardown is proven | ordering and lock-held tests |
| P3 | `errors: []` treated inconsistently | Confirmed | `39962fad1`: absent or `[]` means no errors; any other value fails. This holds in `hasNoC10GraphqlErrors` (admission and P0/P1 sidecar checks) and in Python `_graphql_errors_absent`. The selection rule already matched | shared `tests/fixtures/c10_graphql_errors_vectors.json` (11 vectors) used by TS listener, TS selection and Python parity tests |

## Known live-run risks (unchanged by request)

- A UTF-8 BOM before an enumeration body is rejected by the sidecar and accepted
  by the Python decoder (`utf-8-sig`). The sidecar therefore fails closed on a
  BOM-prefixed body.
- The existing captcha and challenge regex is unchanged. A false positive
  rejects a real page; a false negative relies on the stricter per-member
  projection checks.

## Gates (code head `44584a307`)

- `cd scripts/firecrawl-ops/cre_collector && python3 -m pytest tests/ -q -p no:cacheprovider`: 3447 passed, 1 skipped, 3 warnings (baseline 3348/1).
- `npx tsc --noEmit`: exit 0. `npm run test:unit`: 977 tests, 976 pass, 1 skipped, 0 fail (baseline 964/1).
- `cd apps/playwright-service-ts && npx tsc --noEmit -p .`: exit 0. `npm test`: 60 pass, 0 fail (baseline 26).
- `uvx ruff check`, `uvx ruff format --check`, `python3 -m py_compile` on the 12 changed Python files: clean. The knip pre-commit hook ran on each commit.

## Residual

- Only same-content checks are enforced: the operator must rebuild the prebuilt
  image from the reviewed checkout. Signed health catches a missing lane, but
  not every other drift between the image and the source.
- The P0/P1 arm path still has a single run deadline of at most 120 s.
  Readiness polling, teardown budget and the prebuilt image apply to it, but
  its collection budget was not restructured.
- If a killed child fails to exit within 5 s, a zombie process remains. It is
  not recorded in quarantine.
- Operator recovery from a quarantine uses `docker ps --all --filter
  label=com.docker.compose.project=<project>`. The env file is deleted, so
  recovery cannot use compose file interpolation.
- Live admission remains gated on review and merge.
