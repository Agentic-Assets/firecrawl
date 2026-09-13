# CRE throughput and recovery evaluation

Work plan: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).
Checked on 2026-09-13. This is an evaluation record, not a live completeness claim.

## Baseline and failure

Series `2026-09-13T113641Z` at collector
`7053a8e7c89066bc103105452e5d1da25581f915` completed CBRE and CBRE Deal Flow,
then stopped three times on JLL. Each interruption used the configured 75%
total-host CPU threshold sustained for 10 seconds. At the first inspection no
collector process remained. JLL retained 1,224 unique generation-cache details;
those retain their original observation clocks and are not fresh-fetch benchmark
results.

The host reports an Apple M5 Max, 18 logical CPUs and 128 GiB RAM. Capacity is
shared: at 10:39 ET `top` showed about 67% CPU use and 36 GiB compressed memory;
at 10:43 ET CPU was about 45%. Physical unused memory and the macOS
`memory_pressure` free percentage measure different things. Neither total RAM
nor one quiet snapshot establishes a sustainable browser concurrency.

The baseline API and Playwright sidecar each had a one-CPU cap, with 8 GiB and
4 GiB memory limits respectively. The browser permitted one active page. API port
3102 and browser port 3103 are the CRE stack; port 3002 belongs to a separate
Corbis process.

## Decisions and experiments

| Candidate | Evidence and decision |
| --- | --- |
| Raise source workers immediately | Rejected. The real offline scheduler launched Transwestern twice while its first attempt was active, deleted its temporary artifact, and reported success. Existing 308 checkpoint tests had not covered this trace. Fix under AGENTIC-2896 before calibration. |
| Recover temporary CPU pressure | Implement opt-in bounded cooldown under AGENTIC-2897. Retain the watchdog, exact child resume and typed failure reasons. Prove low-CPU hysteresis, persisted budgets, cancellation and single parent ownership offline. |
| Direct JLL HTTP | Initial Python transport test: 32/32 HTTP 403 responses over C1/2/4/8, eight URLs per round. Not admitted. Error response speed is not successful listing throughput. |
| More concurrent Firecrawl calls | Measure cold identical samples. One browser page can serialize work despite many clients. Compare request limits and browser capacity independently. |
| Larger browser pool | Evaluate a disposable, resource-capped copy of the existing browser image before changing the shared service. Preserve original runtime and pending model settings. |
| Multiple live checkpoint processes | Rejected. They compete for the canonical lifecycle lock. A full parallel series needs an explicit preparation/commit split preserving per-source generations and serial ingestion. |
| New LLM and backups | Root `.env` contains `meta/muse-spark-1.3-contributor` with `zai/glm-5.3-flash` structured fallback; the running API still reports the preceding DeepSeek model. The ordinary CRE path requests non-LLM formats, so this mismatch does not explain its throughput. A second ordered backup requires code/profile support, not a comment in `.env`. |
| Broad upstream sync | Fetch/rehearsal only. Upstream `cc06662bdc1bde321a40f5884c7c01b61bb170dd`, base `e72fe3acac88651c31fc2ac8398926d7fa2fcdd3`, fork origin `7053a8e7c89066bc103105452e5d1da25581f915`: 391 fork-only and 214 upstream-only commits; 524 changed paths and 23 merge conflicts. No demonstrated benefit to the observed HTML bottleneck. Keep integration separate under AGENTIC-2901/2218. |

### Fresh local API sample

Eight identical JLL URLs per round, `maxAge=0`, all non-LLM collector formats,
1,000 ms wait, 20-second per-request bound, no retries or production writes.
The production TypeScript JLL parser and child-channel harvester evaluated the
responses. Results are a small acquisition benchmark, not a full source gate.

| Client concurrency | Wall seconds | Pages/second | Median latency seconds | HTTP/property parse success |
| --- | ---: | ---: | ---: | ---: |
| 1 | 20.255 | 0.395 | 2.442 | 8/8 |
| 2 | 19.339 | 0.414 | 4.861 | 8/8 |
| 4 | 20.419 | 0.392 | 9.424 | 8/8 |
| 8 | 19.843 | 0.403 | 11.197 | 8/8 |

More clients did not increase successful throughput on this runtime. The
one-page browser pool was a candidate bottleneck. The later browser-only
experiment isolated it. The queue-status response's `maxConcurrency: 2` is a
team fallback value, not a demonstrated hard limit on synchronous scrapes;
observed scaling above two clients refuted that earlier hypothesis.
At concurrency eight, p95 latency was 19.825 seconds, close to the test bound.
Host CPU briefly reached 81.57% in one two-second sample but did not sustain
the 75% threshold for ten seconds. Queue active/waiting counts returned to zero.

Property ID, page URL, broker count and native asset counts matched the historic
comparator in all 32 fresh results. Harvested-channel counts matched 7/8 at
concurrency 1, 2 and 4, and 6/8 at 8. These variations must be investigated
before treating the sample as full field-fidelity proof or admitting a new
transport. The historic cache parsed 8/8 using the production parser; an earlier
Python regular-expression probe's zero parse count was a test-harness error,
not evidence of missing JLL data.

### Browser-only capacity experiment and 32-page replication

A disposable copy of the exact existing browser image, capped at two CPUs and
4 GiB RAM with four page slots, completed eight native-HTML requests 3.32 times
as fast at four clients as at one. It was removed after the experiment. That
test justified a reversible browser-only recreation of the shared sidecar.
The API container ID, start time and image remained unchanged; pending model
settings were not applied. The candidate retained 4 GiB memory/swap limits,
loopback port 3103, the prior shared-memory size, security and proxy settings,
with a PID cap of 384.

The corrected replication used 32 distinct cached property identities as a
comparison set, but fetched every response freshly through the full local API
with `maxAge=0`. All ordinary collector formats were requested. Each request
had a 120-second bound, and every round had a 240-second bound. No listing
database or generation-cache writes occurred.

| Client concurrency | Wall seconds | Pages/second | Gain over C1 | Property parse success |
| --- | ---: | ---: | ---: | ---: |
| 1 | 96.863 | 0.330 | 1.00x | 32/32 |
| 2 | 50.837 | 0.629 | 1.91x | 32/32 |
| 4 | 29.760 | 1.075 | 3.26x | 32/32 |
| 8 | 26.386 | 1.213 | 3.67x | 32/32 |

All 128 responses were HTTP 200 without request errors. Required native
gallery/floorplan assets remained present, and media, documents and brochures
matched the comparator in all responses. Supplemental rendered links and
images did not match the historic cache in every response: exact equality was
27/32, 23/32, 10/32 and 18/32 respectively. Those differences remain a separate
fidelity investigation; this is not proof of every harvested field matching.

Eight clients added only 12.8% throughput over four while raising p95 request
latency from 4.794 to 7.284 seconds, a 51.9% increase. Aggregate CPU monitoring
tripped the 75%-for-ten-seconds watchdog after all response rounds, with a
maximum observed 82.19%. That sequential run does not isolate the responsible
round or distinguish collector load from other host work. Eight clients are
not admitted. Observed container peaks were 3.017 GiB / 143 PIDs for the
API and 885.3 MiB / 158 PIDs for the browser; these are sampled peaks, not
continuous maxima. The queue drained after the experiment.

A subsequent isolated C4 run first observed CPU strictly below 60% for thirty
seconds, then freshly fetched the same 32 pages in 27.528 seconds (median
3.257 seconds, p95 4.588 seconds). All 32 were HTTP 200 with valid property
data, native image/floorplan preservation and exact media/document/brochure
matches. The guard completed without a stop, including thirty seconds of
post-round monitoring; the maximum sampled host CPU was 73.09%, and the queue
was idle afterward. Supplemental rendered link/image equality was 14/32 and
remains separately classified rather than represented as perfect parity.
This admits four browser slots and JLL request concurrency four for a
supervised run with the unchanged watchdog and all source/database gates.
It does not admit eight requests or establish long-running 51-source capacity.

The named `browser-balanced` profile now records those three browser settings
in root `.env` without restarting anything. A byte comparison excluding only
those three allowlisted lines verified that all unrelated configuration stayed
unchanged, including API, model and credential settings. Its separate ignored
rollback file is
`tasks/tmp/firecrawl-cre-resource-profile/cre-browser-balanced-2026-09-13.state`;
the older conservative-profile rollback file is preserved. The running API
remains the original container. A later three-page diagnostic found matching
rendered channels and no native asset loss, so it did not reproduce or explain
the earlier supplemental differences; those remain explicitly unclassified.

### Reliability and progress changes

The working implementation now excludes active source identities from cohort
selection, isolates each attempt artifact, and refuses collection over
ambiguous ingest states. Independent tests reproduced logging-failure cleanup
and a nested-process timeout race. Command, cohort and series cleanup now have
ordered 15/30/45-second grace bounds. A real offline process harness confirmed
the nested child was fully reaped after 15.31 seconds, without forcing the
outer worker to exit before its cleanup finished.

CPU recovery is opt-in and bounded. It requires typed terminal CPU evidence,
exact generation/SHA/configuration/database-target binding, reaped processes,
and a continuously observed low-CPU window. Missing telemetry, uncertain
writes, gates and validation failures are never converted into routine retries.
Cooldown counts and elapsed budgets survive a same-generation manual resume.
Neither generation timestamps nor cache observation clocks are rewritten.

The Terminal viewer follows the exact child path reserved before its first
launch, reports per-attempt detail counters and cooldown evidence, and never
treats processed/cache/error counters as successful listing completion. It
reads bounded regular files, excludes raw exception bodies and terminal control
characters, and preserves source-level failures alongside other progress.
Saved parent/child manifests, bounded linked logs and recovery projections
provide the audit trail. These local checks do not establish production
refresh completion; final verification and exact-SHA readbacks remain required.

## Proof path and task ownership

1. [2896](https://linear.app/agenticassets/issue/AGENTIC-2896): source ownership,
   attempt isolation and uneven-completion regression.
2. [2897](https://linear.app/agenticassets/issue/AGENTIC-2897): bounded foreground
   recovery, typed causes and exclusive parent ownership.
3. [2898](https://linear.app/agenticassets/issue/AGENTIC-2898): JLL transport and
   complete field/identity/document/media fidelity evaluation.
4. [2899](https://linear.app/agenticassets/issue/AGENTIC-2899): readable terminal
   progress and durable recovery/settings evidence, building on PR47.
5. [2900](https://linear.app/agenticassets/issue/AGENTIC-2900): measured resource
   and concurrency profile. Record successes/minute, p50/p95 latency, CPU
   distribution and high windows, memory pressure, retries and queue settlement.
6. [2901](https://linear.app/agenticassets/issue/AGENTIC-2901): upstream decision
   and preservation proof, separate from the critical repair.
7. [2902](https://linear.app/agenticassets/issue/AGENTIC-2902): pushed immutable
   candidate, no-write canary, full 51-source refresh and exact DB readbacks.

Small deterministic tests and bounded cold samples precede adoption. Reject
settings that reduce fidelity, introduce errors or breach resource limits.
Report cache-assisted resume gains separately. A 10x or 20x improvement is a
hypothesis until measured on successful equivalent work.

Database ownership, exact target binding, identity and freshness gates remain.
No OM writer, lifecycle status activation, mark-missing, baseline update,
production DDL or hidden scheduler is introduced by this plan. A canary or code
merge is not proof that all 51 sources are current.

Local-only raw evidence: `tasks/tmp/cre-performance-2026-09-13/` and the named
checkpoint-series directory. Keep credentials, scraped bodies and full process
arguments out of shared reports. Portable summary and code proof belong here
and in the linked PR; Linear owns actionable work state.
