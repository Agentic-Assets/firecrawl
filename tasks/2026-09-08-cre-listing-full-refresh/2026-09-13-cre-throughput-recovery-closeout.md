# CRE throughput and recovery implementation closeout

Checked 2026-09-13. This closes out the code-repair work, not the outstanding
full production refresh.

- Branch: `codex/cre-throughput-recovery`.
- Main baseline: `7053a8e7c89066bc103105452e5d1da25581f915`.
- Included progress commit: `9bb56ab65069a2d21737e87bd30d7ca0ef36a04f` (PR 47).
- Implementation: `b8df6e093b7c824130a56790eec1ddd8093adbe9`.
- Work ledger: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895),
  with children AGENTIC-2896 through AGENTIC-2902.
- State at capture: implementation committed locally; PR publication and merge
  pending. No full production run started from this implementation.

## Implemented and verified

- Prevent duplicate active cohort sources and isolate each attempt artifact.
  Preserve serial ingest and refuse collection over ambiguous write states.
- Clean up every owned process even when log writes fail. Command/cohort/series
  grace periods are ordered 15/30/45 seconds to allow nested children to be
  reaped before an outer worker is killed.
- Add opt-in bounded CPU cooldown with persisted budgets, exact child identity,
  strict telemetry, a continuous sampled low-CPU window and exclusive parent
  ownership. Never retry uncertain writes or quality gates as CPU incidents.
- Bind the child run before its first launch. Repeated entry after ambiguous
  startup interruption preserves the reservation and refuses automatic replay.
- Show source phase, per-attempt counters and cooldown evidence in Terminal.
  Bound file reads and watch intervals; withhold raw errors and terminal controls.
- Add the reversible browser-only `browser-balanced` profile, preserving API,
  model, credential and port settings and prevalidating rollback state.

Local verification of the frozen implementation, repeated after commit
`b8df6e093b7c824130a56790eec1ddd8093adbe9`:

- 2,482 Python tests passed, one skipped, four subtests passed, including the
  full collector and resource-profile suites. The new pure recovery module
  has 206/206 covered statements (100%) using pytest-cov 7.0.0 / pytest 9.1.0.
  The skip is a reviewed, gitignored Cushman repair artifact absent on this clone.
- TypeScript typecheck and 814 TypeScript tests passed. The test runner used
  `--test-concurrency=2` to bound host contention.
- Changed-file Ruff, Python compilation, shell syntax and `git diff --check`
  passed. Normal commit hooks ran `pnpm knip --cache` successfully; its one
  pre-existing configuration hint was not an error. No hook bypass was used.
- Real offline duplicate-worker and nested-process reproducers passed. The
  nested process was fully reaped in 15.30 seconds without an outer forced kill.
- Independent Astra review: 524 focused tests passed; no open consequential
  finding. Separate visible Sol review: 503 focused tests passed; no remaining
  finding in its progress/binding scope. Counts overlap and are not additive.

Local review and experiment details are under ignored
`tasks/tmp/cre-performance-2026-09-13/`; portable evidence and limitations are
in [the evaluation record](../../docs/firecrawl-ops/references/cre-throughput-recovery-2026-09-13.md).
CI and actual production readback are separate proof layers, not established
by these tests.

## Measured runtime decision

The old one-page browser serialized JLL detail work. A four-page browser at two
CPUs and a PID cap of 384 gave a 3.26x improvement on equivalent fresh 32-page
work. An isolated C4 replication completed 32/32 valid pages in 27.528 seconds,
p95 4.588 seconds, with no CPU guard trip and an idle queue after thirty seconds
of trailing monitoring. Peak sampled host CPU was 73.09%.

Admit JLL C4 only with the watchdog and all existing gates. Do not admit C8:
its modest throughput gain came with substantially worse latency, and the
combined matrix hit the sustained host guard. Direct JLL HTTP returned 403 in
32/32 trials and is rejected. Supplemental rendered link/image differences
remain unclassified; required native assets and document channels were preserved.

The browser sidecar was recreated with its existing image and unchanged
security/proxy/port/shared-memory settings. The API container is unchanged:
`949b37d83a9393632c4a4c16d96b294163e64e9cf46e56499558b6e74f29ef5d`,
started `2026-09-13T07:27:57.312831177Z`. The balanced profile subsequently
changed only its three allowlisted root `.env` resource lines; a byte fingerprint
confirmed all unrelated configuration remained unchanged. Separate rollback
state was preserved; see the runbook for the exact state path and restore step.

## Remaining required work and boundaries

The old series remains stopped at 2/51 complete with 1,224 preserved JLL cache
details. It is not resumed under a different SHA. A pushed immutable candidate,
merge gate, new full-registry run and exact per-source database readbacks remain
required under AGENTIC-2902. No production DDL, OM writes, status activation,
mark-missing, baseline updates or new scheduler were performed.

Broad upstream integration remains separate: rehearsal found 23 conflicts and
no demonstrated fix for the measured bottleneck. Pending model settings were
not applied by restarting the API; normal CRE HTML acquisition is non-LLM.

The 40-minute heartbeat remains paused. After verified merge and a clean,
fast-forwarded local main, the requested new Terra 5.6 high task should own it
on this project without a worktree and contact the root task if issues arise.
Automation activation is not a substitute for the full refresh acceptance proof.

See the [forward menu](2026-09-13-cre-throughput-recovery-forward-queue.md).

## 2026-09-13 optimization telemetry extension

Committed and pushed `a2eac761d6dcd5ac97a40bb39334a82960e528df` to
`codex/cre-throughput-recovery` / [PR48](https://github.com/Agentic-Assets/firecrawl/pull/48).
The PR remains the review surface, not proof of merge or full data freshness.

The checkpoint runner now saves per-command start/end/interruption journals,
per-invocation bounded scraper snapshots and initial runtime resource settings.
The one-shot `cre_performance_report.py` shows slowest measured phases and
structured requests/retries/cache/latency/resource/quality signals. It reads
only local bound evidence, never the database. Source payloads, formats,
retry/backoff values, freshness and production-write gates are unchanged.

Repeated after that implementation commit: 2,549 Python tests passed, one
existing Cushman artifact-dependent skip, four subtests passed. TypeScript
typecheck and full suite passed before commit; implementation-lane total 829.
New command/runtime and report statement coverage is 87% and 85% respectively
(86% combined). Root Ruff, Python compilation, shell syntax and diff checks
passed. Normal knip commit hook passed, without bypass.

Independent telemetry review passed 104 Python and 74 focused TypeScript tests
and two actual TypeScript-writer/Python-reader integrations. Confirmed issues
were fixed: temporary symlink/FIFO hazards, cleanup after partial writes, and
nonfinite measurements incorrectly becoming zeros. Missing readings remain
null/degraded. No remaining confirmed P0/P1/P2 in the reviewed telemetry slice.

Measured optional logging cost: median batch mean 0.333 ms per command across
500 offline no-op commands; 0.810 microseconds per simulated scrape attempt
across 50,000 events with real atomic snapshots. Final snapshots were about
1.9 KiB; the narrow runtime inspect took 260 ms. These are microbenchmarks,
not pipeline-speedup or all-source capacity proof.

Definitions and limitations are in
[the telemetry runbook](../../docs/firecrawl-ops/references/cre-performance-telemetry.md).
Docker's 64 KiB inspect limit applies after buffering, not as a hard capture
memory bound. Initial configuration is not current or resumed usage. Request
throughput and emitted fields are not correctness/completeness certificates.

Cayman's accuracy-first clarification is recorded in
[AGENTIC-2902](https://linear.app/agenticassets/issue/AGENTIC-2902/prove-improved-cre-refresh-throughput-and-complete-all-source#comment-28a6711c).
`raw_data` preserves normalized/pruned collector rows, not uniform original
provider responses. Brochure/media rows store URLs, not downloaded contents.
Address-based research remains a separately evaluated, provenance-linked layer;
no enrichment search, new schema, OM write or canonical overwrite was added.
The old full run and automation gates remain as described above.
