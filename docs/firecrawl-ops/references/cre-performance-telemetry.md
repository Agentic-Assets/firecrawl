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
```

The first command watches progress. The performance report is a read-only,
one-shot Terminal table or JSON object; omit its path to select the newest
series. It also accepts a single-source checkpoint directory. No server,
browser dashboard, tracing vendor, database connection or scheduler is added.

## Saved evidence and meanings

| Evidence | Location within each checkpoint | What it measures |
|---|---|---|
| Command journal | `logs/COMMAND.performance.jsonl` | Start, terminal outcome and monotonic duration of collection, health, validation, gate and ingest subprocesses. Each invocation has a distinct ID, including repeated uses of the same log during resume. |
| Scrape snapshot | `logs/COMMAND.INVOCATION.scrape-performance.json` | Logical helper calls, client attempts, retry/backoff, latency histogram, locally awaited concurrency, JLL detail cache, source outcomes and sampled Node resources. |
| Initial runtime configuration | `runtime-performance.json` | Known API/browser container IDs, image IDs and configured CPU/memory/PID/shared-memory/port limits, plus available host CPU/RAM configuration. One narrow read before the guard starts; not current usage. |
| Host guard | `logs/host-cpu-guard.jsonl` | Existing sampled host CPU and guard decisions, including unrelated applications. |
| Incident evidence | `logs/cpu-incidents.jsonl` | Existing resource-stop context. This remains separate from optional performance diagnostics. |
| Business evidence | Manifest-linked source JSON, gate, dry-run and readback artifacts | Source observations, retained structured detail and admission/ingestion proof. Never substitute request counts for these artifacts. |

Snapshots are atomic replacements at most every 10 seconds as events occur,
plus a final flush on normal completion/error unwinding. There is no new timer
or signal handler. A stalled request can leave an older snapshot; abrupt process
termination may leave no terminal record. The reader labels that uncertainty.
Terminal means that snapshot was finalized, not that collection succeeded.

The JSON snapshot preserves the fixed latency histogram, finite error categories
and bounded HTTP status counts for later analysis. It does not contain URLs,
page bodies, broker/contact records, exception messages, command-line secrets
or environment values. Sensitive raw business artifacts stay in their existing
governed artifact locations, not in the optimization log.

Runtime configuration uses one two-second read-only Docker inspect of the two
known containers, never environment or secret fields. An unavailable sample
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
