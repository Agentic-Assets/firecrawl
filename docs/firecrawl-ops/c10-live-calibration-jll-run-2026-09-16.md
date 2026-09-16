# C10 live calibration, JLL admission run attempt (2026-09-16)

Phase 3 of the JLL admission live-run sequence
(`docs/firecrawl-ops/c10-jll-admission-lane.md`,
`docs/firecrawl-ops/c10-jll-admission-closeout-2026-09-16.md`,
`docs/firecrawl-ops/c10-live-calibration-phase2-2026-09-16.md`). **No live
network request against JLL was made.** The bounded admission action never
reached sidecar startup: every attempt failed while acquiring the canonical
CRE lock, before any Docker Compose, browser, or provider work began.

## What was attempted

1. Verified HEAD (`9dcfb4300`) matches the commit that last touched the
   sidecar image and C10/collector code, so the images built in Phase 2
   (`firecrawl-playwright-service-c10:local` `a5225c40abd6`,
   `firecrawl-c10-linux-runner:local` `7f492878e1dc`) were still valid without
   a rebuild.
2. Brought up the generic Linux runner (`run_linux_controller.sh up`),
   confirmed `node_modules` and Docker-socket-to-host reachability, and ran
   the offline `collect-jll` dry-run inside it to confirm intent
   (`selection_rule: jll-canonical-url-lexicographic-v1`, 16 members,
   `external_calls: false`) before touching the live path.
3. Provisioned a fresh owner-0700 receipt root:
   `tasks/tmp/c10-live-calibration/receipts-run-20260916T100930/` (gitignored,
   verified with `git check-ignore`).
4. Wrote a one-off driver script (`tasks/tmp/c10-live-calibration/run_live_admission.py`,
   not part of the reviewed collector surface, gitignored) that calls
   `capacity_c10.production.execute_jll_admission_collection` with a fresh
   operator-supplied provenance binding (real SHA-256 digests derived from
   descriptive labels, not placeholders) and
   `adapter_implementation_sha256 = repository_implementation_sha256("jll")`
   (`1371211b54bc73d72b45c48daf241abf9588a7f20ec5dee76df567bcb065e390` on this
   checkout).
5. Ran the script inside the Linux runner (`cwd=/workspace/firecrawl`, the
   repo bind mount).

## Attempts and outcome

| # | Result | Where it failed | Notes |
| --- | --- | --- | --- |
| 1 (script bug) | Failed in ~14 µs | Python call itself (`TypeError: missing adapter_implementation_sha256`) | Driver script bug; never reached `lock.acquire()`, receipt root untouched (verified empty). Not counted as a live attempt: no lock, sidecar, or network activity occurred. |
| 1 (real) | Failed in ~0.4 s | `lock.acquire()` inside `execute_jll_admission_collection`, before `arm_benchmark`, before any Docker Compose or sidecar work | `LockHeldError: CRE lock authority is malformed`. Root cause below. |

No second or third live attempt was made: the blocking condition is a
pre-existing environment state issue, not a code defect in the JLL admission
path, and retrying without fixing it would fail identically every time.

## Root cause

`scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock.authority` (the
canonical, persistent CRE `SharedLock` authority file shared by every C10 and
checkpoint-refresh caller, per `LOCK_AUTHORITY_RECOVERY.md`) currently holds a
truncated two-field record (`40557 <token>`, 50 bytes, timestamped
2026-09-15T07:17 — matches `os.stat` mtime `Sep 15 03:17` local time) instead
of the expected three-field legacy or five-field v1 format. The sibling
`.cre.lock/` directory carries matching
`capacity-benchmark-active.json` / `capacity-benchmark-quarantine.json`
markers recording `"reason": "candidate_baseline_rollback_failed"` from a
`pytest-of-caymanseagraves` run (`test_counterbalanced_pair_step0`, profile
`bold-jll-128`) — i.e. this is exactly the **historic pytest quarantine
residue** scenario `LOCK_AUTHORITY_RECOVERY.md` describes: a test run wrote
into the real canonical lock path instead of an isolated fixture, crashed
before completing rollback, and left a "legacy two-field authority sibling"
requiring operator-governed recovery. It predates this session and this
branch; it is not something Phase 1/2 of this work introduced, and PID 40557
is confirmed dead (`ps -p 40557` → no such process).

`LOCK_AUTHORITY_RECOVERY.md` names the single supported repair:
`cre_capacity_runtime.py recover-quarantine` (dry-run by default,
`--execute` to archive). It ran and reported the exact stop condition to use
next (`quarantine recovery requires an exact idle baseline runtime`). Manual
inspection of the underlying `checks` dict (`cre_quarantine_recovery.evaluate_state`)
showed 23 of 25 checks passing, including every safety-relevant one:
`api_queue_idle`, `active_crawls_idle`, `rabbitmq_idle`, `nuq_idle`,
`collector_idle`, `orb_running`, `docker_usable_memory`, `browser_cpu`,
`browser_pages`, `browser_pids`, `browser_port`, `browser_network`,
`browser_security`, `api_*`. The two failing checks were `browser_memory` and
`browser_shm`: the recovery tool always validates the live runtime against
the `bold-jll-128` experiment profile's `runtime_baseline`
(`browser_memory_bytes: 17179869184` / 16 GiB, `browser_shm_bytes:
8589934592` / 8 GiB, exact-equality checks against `firecrawl-playwright-service-1`'s
live cgroup limits), and the currently running `firecrawl-playwright-service-1`
— the ordinary, shared, general-purpose Firecrawl playwright sidecar used by
all self-hosted scraping on this Mac, not a C10-specific container — is
configured with 4 GiB memory / ~15.68 GiB shm (`docker inspect`:
`Memory=4294967296`, `ShmSize=16834887680`), neither of which matches the
required exact values.

## Why this was not forced through

Reconfiguring `firecrawl-playwright-service-1`'s memory/shm limits to match
the `bold-jll-128` baseline (and restarting it) to satisfy this check would
be a disruptive change to shared, actively-serving production Firecrawl
infrastructure — well outside "one bounded, read-only live JLL admission run
plus root-cause fixes." It is not a JLL-specific hack and not a weakening of
a fail-closed refusal, but it is a consequential infrastructure change this
session is not authorized to make unilaterally, and the host (31.36 GiB
Docker memory total, ~3 GiB already committed to other running containers)
would be materially more memory-constrained with a 16 GiB + 8 GiB reservation
added for a container that would otherwise sit idle. No file under
`out/daily/.cre.lock*` was modified, read past its already-known content, or
deleted; `LOCK_AUTHORITY_RECOVERY.md` explicitly forbids ad hoc deletion as a
repair step, and this session honored that.

## What is unaffected

- No provider (JLL) request was made — `external_calls` never left the
  dry-run `false` state for this session.
- No database/cache/listing/scheduler write occurred.
- No authority file was installed or edited.
- No C10 sidecar (`playwright-service-c10`) container was ever created this
  session (`docker ps --all` shows none); the generic Linux runner container
  (`firecrawl-c10-runner-c10-linux-runner-1`, the non-C10-specific shell used
  to reach Linux-only `PrivateReceiptStore`) was brought up for the preflight
  check and the live-run attempt and was torn down afterward
  (`run_linux_controller.sh down`, confirmed removed).
- The canonical lock and its authority/quarantine markers are byte-identical
  to their pre-session state (verified via `stat`/`md5` before and after).
- No collector/capacity_c10 source file was changed. No test gate reruns were
  needed.

## Recommended next step (operator decision)

Recovering the canonical lock requires either:

1. Operator approval to temporarily reconfigure
   `firecrawl-playwright-service-1` to the `bold-jll-128` baseline
   (`mem_limit`/`memswap_limit` 16 GiB, `shm_size` exactly 8 GiB), run
   `cre_capacity_runtime.py recover-quarantine --execute` once idle-verified,
   then restore the container to its prior configuration; or
2. Running the recovery from a host/session where
   `firecrawl-playwright-service-1` is already sized to that profile (e.g. an
   active C10 P0/P1 benchmark session), rather than the ordinary self-hosted
   dev stack; or
3. A separately reviewed change to `cre_quarantine_recovery`'s baseline
   selection so historic-residue recovery does not require the *exact*
   benchmark profile that happened to be active when the residue was
   created — out of scope for this session (it is exactly the kind of
   "weakening a fail-closed refusal" this task was told never to do without
   review).

Once the canonical lock is recovered, the exact sequence in
`docs/firecrawl-ops/c10-live-calibration-phase2-2026-09-16.md` section 4
remains valid and unchanged: fresh 0700 root, `execute_jll_admission_collection`,
`build-jll-bundle`, `render-jll-authority` (never install), reviewed pin PR.

## Files this session added (gitignored, not committed)

- `tasks/tmp/c10-live-calibration/receipts-run-20260916T100930/` (empty)
- `tasks/tmp/c10-live-calibration/run_live_admission.py`
- `tasks/tmp/c10-live-calibration/attempt1.log`
