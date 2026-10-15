# CRE performance evidence

The checkpoint pipeline saves optional performance evidence alongside its
existing manifest, source JSON, validation, ingest and readback artifacts.
Performance diagnostics never authorize a database write, weaken a freshness
gate, change a request format or replace required safety evidence.

## Read it

From `scripts/firecrawl-ops/cre_collector/`:

```bash
python3 cre_series_status.py --watch 5
python3 cre_performance_report.py
python3 cre_performance_report.py out/checkpoint-series/SERIES_ID --json
python3 cre_performance_report.py out/checkpoint-series/CURRENT_ID --baseline out/checkpoint-series/BASELINE_ID
```

The first command watches progress. The performance report is a read-only,
one-shot Terminal table or JSON object; omit its path to select the newest
series. It also accepts a single-source or multi-source checkpoint directory.
Checkpoint and series coordinators save `performance-report.json` and
`performance-report.md` on exit, including ordinary failure and interrupt paths.
To save an offline report explicitly, append `--out-dir REPORT_DIRECTORY`. No server,
browser dashboard, tracing vendor, database connection or scheduler is added.

The same report and comparison commands run on macOS and in Windows' WSL
checkout. Comparison reads saved evidence only. JSON preserves each code SHA,
scope, settings, measured source durations, staged counts, readback/quality
outcomes, phase command times and query client timings. Query comparisons show
the number of measured calls as well as summed client milliseconds, so fewer
audits are distinguishable from a faster individual query.

The comparison shows end-to-end wall time separately from summed command
durations. It supplies an observed elapsed reduction only for matching source
sets, transaction/freshness scope, staged counts, successful run/source outcomes
and recorded successful readback/no-regression outcomes. Known changed cache
work also prevents that matched-work percentage; observed cache/retry/backoff
counts remain visible in JSON. Changed code, configuration, runtime limits or
quality/work counts are visible context. The result is observational, not a
causal speedup claim; provider, cache and host conditions can still differ.
Unmatched runs remain useful side-by-side measurements and never receive a
whole-run speedup percentage. Older missing metrics stay unknown.

In opt-in pipeline runs, source transition timestamps record dispatch waiting,
collection, waiting for validation, ingestion and verification. These timestamp
intervals are wall observations; subprocess journals use monotonic elapsed
time. One shared child is read once even when several series source entries
refer to it. Shared DB-unit audits, machine resources and query timing frames
are attributed once to the owning run/unit rather than to every brokerage.
Unit before/post timings follow their declared paths and SHA bindings; changed
or missing reports become gaps. Reused initial/post snapshots are counted once,
even when the initial snapshot is copied into a unit's evidence directory.
Per-source rates use collection-to-verified wall time when available; the
separate acquisition rate retains its collection-only meaning.

## Saved evidence and meanings

| Evidence | Location within each checkpoint | What it measures |
|---|---|---|
| Command journal | `logs/COMMAND.performance.jsonl` | Start, terminal outcome and monotonic duration of collection, health, validation, gate and ingest subprocesses. Each invocation has a distinct ID, including repeated uses of the same log during resume. |
| Validation query timings | `pre-validation.json` and `validation.json` under top-level `query_timings` | Per-query psql client elapsed milliseconds, keyed by the existing validation query name. This includes client-side result handling and is not pure server execution time. |
| Scrape snapshot | `logs/COMMAND.INVOCATION.scrape-performance.json` | Logical helper calls, client attempts, retry/backoff, latency histogram, locally awaited concurrency, JLL detail cache, source outcomes and sampled Node resources. |
| Initial runtime configuration | `runtime-performance.json` | Known API/browser container IDs, image IDs and configured CPU/memory/PID/shared-memory/port limits, plus available host CPU/RAM configuration. One narrow read before the guard starts; not current usage. |
| Resource timeline | `resource-usage.jsonl` | Five-second platform-specific CPU/load, memory/pressure/swap, filesystem capacity, owned process RSS and selected Compose API/browser container usage, limits and OOM/restart snapshots. Each resume has a distinct session ID and monotonic clock. |
| Required guards | `logs/host-cpu-guard.jsonl`, `logs/host-memory-guard.jsonl` | Platform-selected CPU and memory-pressure stop decisions. Darwin uses Mach/sysctl; Linux uses `/proc`, PSI and visible cgroup evidence. Under WSL these cover the Linux VM, not aggregate Windows load. Memory warning defaults to 20 seconds; critical stops immediately; missing/stale required telemetry fails closed. |
| Incident evidence | `logs/cpu-incidents.jsonl` | Existing resource-stop context. This remains separate from optional performance diagnostics. |
| Business evidence | Manifest-linked source JSON, gate, dry-run and readback artifacts | Source observations, retained structured detail and admission/ingestion proof. Never substitute request counts for these artifacts. |

Snapshots are atomic replacements at most every 10 seconds as events occur,
plus a final flush on normal completion/error unwinding. Scrape snapshots use their existing event-driven writer. Resource observations
use a separate diagnostic thread; no signal handler is added. A stalled request can leave an older snapshot; abrupt process
termination may leave no terminal record. The reader labels that uncertainty.
Terminal means that snapshot was finalized, not that collection succeeded.

The JSON snapshot preserves the fixed latency histogram, finite error categories
and bounded HTTP status counts for later analysis. It does not contain URLs,
page bodies, broker/contact records, exception messages, command-line secrets
or environment values. Sensitive raw business artifacts stay in their existing
governed artifact locations, not in the optimization log.

Runtime configuration resolves the selected Compose project and services,
checks the API loopback endpoint binding and reads narrow Docker inspect fields,
never environment or secret fields. A stopped or unbound stack is unavailable,
not evidence from a different default stack. Endpoint selection follows the
collector's `FIRECRAWL_API_URL` setting; `API_URL` alone does not redirect the
collector. A `localhost` endpoint requires compatible bindings for both address
families; a single-family stack can be measured with its explicit loopback IP. An unavailable sample
does not stop collection. Resuming a generation preserves its initial sample;
the report explicitly does not claim that it describes the resumed runtime.
The 64 KiB inspect limit validates output after subprocess capture; it is not a
strict cap on capture-buffer memory. The fixed two-container projection keeps
normal output small. Browser page capacity is not inferred from CPU limits.

Request success means a shared Firecrawl helper returned usable content. It is
not a valid-property, field-accuracy, ingestion or freshness count. JSON parsing
can fail after a successful HTTP/helper attempt. Retries scheduled to make
another attempt and the existing final-attempt sleep are recorded separately.
Backoff is scheduled delay, not an assertion that an interrupted sleep finished.

Validator query results and timings use separate psql output channels inside the
same repeatable-read, read-only transaction. Result framing and TSV shape remain
fail-closed. Missing, malformed, duplicated, or structurally invalid optional
timing output becomes a bounded `unavailable_code` and does not invalidate an
otherwise complete result frame. Timing metadata never stores SQL, raw psql
output, credentials, connection strings, or provider bodies.

Request latency excludes backoff. Histogram percentiles are approximate upper
bounds; overflow has no finite upper bound and is unknown in the summary.
Throughput uses monotonic elapsed time, not the sum of parallel request durations.
`max_active_locally_awaited` is client concurrency, not Docker/server concurrency.
The existing request deadline does not cancel its SDK promise, so its possible
remote continuation is recorded as unknown rather than assumed settled.

JLL cache hits mean an accepted cached detail document was returned. Misses and
explicit refresh bypasses are separate. Cache age/freshness still comes from
the existing source/generation contracts. Other source caches, direct provider
HTTP timing and API queue wait remain explicitly uninstrumented.

Node RSS is a current sample and the maximum of those samples, not a true peak
and not browser/API container memory. Process CPU is cumulative Node user/system
microseconds. The report's CPU distribution uses at most the final 4 MiB of the
guard log. A count of high samples is not a contiguous high-CPU duration.


## Resource interpretation and older trials

A phase manifest changes when the coordinator records a phase transition; it
is not a continuous process heartbeat. During a long detail pass, compare fresh
scrape counters, command/process evidence and required guard timestamps before
calling a run stuck. A detail counter includes cache and error outcomes and
does not establish completed or ingested listings.

`CRE resource usage diagnostics degraded` means an optional observation missed
one or more fields or its sampling deadline. Inspect `unavailable_fields` and
later samples in `resource-usage.jsonl`, independently of the required CPU and
memory guard journals. Missing optional Docker VM statistics do not mean that
the required guards failed or that a healthy collection should be restarted.

When successful request latency dominates and client concurrency is one,
additional RAM alone will not shorten the pass. Evaluate a small concurrency
increase on a new immutable run, retaining the rendered-page path, cancellation
and provider admission checks. JLL's `JLL_DETAIL_WAIT_MS=0` omits `waitFor` and
can change engine selection; it is not simply a faster equivalent rendering
setting. Direct-provider transport remains a separately admitted experiment
under [AGENTIC-2898](https://linear.app/agenticassets/issue/AGENTIC-2898).

The required memory guard is independent of optional diagnostics. It uses the
normalized pressure levels (1 normal, 2 warning, 4 critical), requires a
normal preflight, and shares only the existing first-trip signal/owned-process
cleanup protocol with the CPU watchdog. Required guard decisions begin cleanup;
they do not prove queued server work has stopped. Only the first watchdog
signals, preventing a simultaneous second trip from interrupting child reaping.
Both guards drain before terminal status is saved; a later memory trip retains
its recovery veto even when CPU sent the first signal. The memory policy and terminal reason are persisted; CPU-only automatic
recovery cannot resume a memory stop. Existing container hard limits and memory
usage remain separate diagnostics, not a predictive per-container memory stop.
The level mapping follows Apple's [XNU sysctl conversion](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_memorystatus_notify.c),
which emits dispatch pressure flags. This masked Darwin sysctl is not a promised
stable public API; a missing or changed response blocks the Darwin run. Linux
instead checks PSI, available memory/swap and visible cgroup limits/events;
missing required platform evidence also blocks the run. See
[the checkpoint safety contract](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md#cpu-and-memory-safety-and-evidence)
for the platform-specific thresholds and scope.

The resource sampler runs only while the foreground checkpoint owns its lock.
It uses bounded read-only commands and an owner-only, no-follow journal capped
at 16 MiB. Optional diagnostics never change a workflow return code or the
existing CPU guard. Sampling stops before ownership is released. The standalone
read-only check does not start Docker, collectors, database work or a scheduler:

```bash
python3 cre_resource_usage.py --out-dir /tmp/cre-resource-check --duration-seconds 20
```

CPU includes every application visible to the measured host; under WSL this
is the Linux VM, excluding unrelated Windows applications. Owned process CPU from `ps` is a lifetime
average, not interval CPU; RSS aggregates can count shared pages more than once.
macOS free, inactive, wired and compressed page categories are reported directly.
Free pages alone do not establish usable RAM, and host RAM is not Docker VM RAM.
Swap usage can predate a run. Pressure levels and swap counter changes provide
additional context. Missing platform metrics remain unknown.

Docker CPU percentage uses 100% per core; normalize against the measured CPU
quota when available. Container memory limits and host RAM are separate limits.
Network, block I/O, restart and process CPU counters are cumulative, not per-second rates. OOM-killed state and restart counts describe the observed container lifetime; they do not by themselves attribute an incident to this run.
Sampled maxima can miss short spikes. Summaries show available sample counts,
session bounds and gaps, and use duration weighting only across adjacent valid
observations within the same session. They do not bridge resumes or cooldowns. Coverage describes the first-to-last
observation window, without extrapolating the unsampled start or end. Series
reports show the currently bound checkpoint for each source; earlier failed or
recovery attempts remain in their own artifact directories and are not summed
into a full-series resource total.
Series-level machine data belongs to the coordinator; parallel source reports
do not each claim all shared CPU or wall time.

A bounded historical trial can be inspected without treating it as a canonical
checkpoint generation:

```bash
python3 cre_performance_report.py --trial-dir /path/to/canary --out-dir /tmp/cre-trial-report
```

The importer reads only numeric and enum projections of the existing artifacts.
It separates trial wall time, collector artifact timing and process polling
completion times; reports sampled concurrency overlap; and distinguishes raw,
staged, rejected and ingested counts. Missing historical host memory, request
latency or instrumentation cannot be recovered. An exit code of zero is not
quality or ingestion proof. A speedup requires a matched baseline with the same
scope, cache policy, code and quality outcome; otherwise it remains unknown.

## Compare improvements without sacrificing data

Compare the same source, transaction scope, formats and freshness policy. Keep
the collector SHA, configured concurrency/pacing, runtime resource limits and
cache policy with each experiment. Check request failures, latency and CPU
alongside listings staged, field/document/media preservation and exact database
readback. Do not claim a speed win by dropping content, using older cached
observations or counting a processed error as an enriched listing.

Brokerages publish different amounts of detail. A missing field may be genuinely
unpublished, inapplicable or unsuccessfully extracted; presence alone does not
distinguish these. Preserve raw source literals and provenance alongside shared
structured fields. Do not fill gaps with invented values. Separate internet or
address enrichment requires verified property identity, cited observation dates
and conflict handling before it can influence canonical brokerage facts.

## Bounds and failure behavior

Command records are capped at 4 KiB and journals at 1 MiB; snapshots at 128 KiB.
An offline report reads at most 1,024 journals and 16 MiB of journal content per
checkpoint, retaining parsed measurements and reporting a gap if the byte
budget is exhausted. This accommodates multi-source runs without multiplying
the same journal scan by the number of brokerages.
Query timing reads separately allow up to 1,024 unit entries and 16 MiB of
report content per checkpoint. Retried preparation units can outnumber sources;
they do not erase known audit timings. Exhausted budgets retain measurements
already read and add an explicit evidence gap.
Regular-file checks, no-follow opens and nonblocking journal locking prevent
special files from blocking collection. Command journals are owner-only. A
diagnostic write failure emits a fixed warning and preserves the original
return code, exception and process-cleanup ownership. Required logs and resource
safety evidence retain their stricter existing behavior.

The report reads only bound generation/SHA evidence and allows only fixed numeric
and enum projections. Missing, oversized, malformed, stale and mismatched files
are evidence gaps. It does not silently treat them as successful work or zeros.
Inline Python work is not all timed; command totals are not full source wall time.

This addition does not delete or rotate existing business artifacts, logs or
historical generations. Bounded metrics remain in the gitignored checkpoint
artifact directory for later comparison; they are not uploaded or committed.
Older runs without instrumentation remain unknown. No historical timings are
fabricated from output line counts.

### CBRE Deal Flow inventory retry

`GetFilters` and `GetListingsHtml` are the only direct CBRE provider reads that
may make one additional idempotent POST attempt. The retry is limited to a
transport/body-read failure or HTTP 429, 500, 502, 503, or 504. It does not
retry malformed JSON, a semantic `success:false` response, 401/403, other
client failures, or any inventory-quality/identity gate. Existing page, total,
freshness, identity, and child-preservation checks are unchanged.

When a retry is scheduled, the collector records it through the existing
`http_helper` retry counters. The provider-facing log includes only the
endpoint label, HTTP/transport category, attempt, and delay. Saved telemetry
contains aggregate `http_helper` retry/backoff counters only. Neither surface
contains a query token or provider body. `Retry-After` is honored for a local
delay of at most 30 seconds and only while the helper's 250-second deadline
remains. A longer provider instruction fails closed. This is a helper-local
boundary: the outer whole-source/checkpoint runner can still make a separately
governed future source run, so it is not a global provider embargo.

The 2026-09-13 incident context included about 282 seconds of duplicate work
in a repeated Dealflow sale pass after a transient inventory failure. That is
not a benchmark and no future time saving has been measured. Compare later
runs from the saved retry counters and source artifacts before making an
optimization claim.

## Verification on 2026-09-13

Before publication, the full Python collector/resource-profile suite passed
2,549 tests with four subtests and one existing fixture-dependent skip. The skip
needs an absent gitignored Cushman repair artifact. New Python telemetry/report
statement coverage was 86% combined (87% command/runtime, 85% report). TypeScript
typecheck and the full collector suite passed; the implementation lane reported
829 tests. Independent review passed 104 Python and 74 focused TypeScript tests,
including two real TypeScript-writer to Python-reader artifact checks. Counts
overlap and are not an additional total.

The review found and fixed pre-existing-temporary-file symlink/FIFO hazards,
cleanup after partial writes, and invalid resource measurements being converted
to false zeros. Actual special-file cases and null/degraded snapshots now have
regression coverage. No acquisition format, retry count, delay, cache admission,
canonical listing payload, schema or production-write policy was changed.

Local diagnostic-only microbenchmarks measured a median batch mean of 0.333 ms
per command across 500 no-op commands (332,164 journal bytes). A separate
50,000-attempt counter test with real atomic writes measured 0.810 microseconds
per attempt and roughly 1.9 KiB per final snapshot. One narrow runtime inspect
completed in 260 ms. These are low-overhead evidence, not measured full-pipeline
overhead percentages or production throughput. Local raw proof remains under
`tasks/tmp/cre-performance-2026-09-13/`.
