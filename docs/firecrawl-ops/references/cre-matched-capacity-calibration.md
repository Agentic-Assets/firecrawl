Parent: AGENTIC-2895. Calibration issue: AGENTIC-2900. Linux/C2 engineering: AGENTIC-4169 (under AGENTIC-3757).

# Matched CRE capacity cells

`scripts/firecrawl-ops/cre_collector/sources/matched-capacity.ts` provides a
bounded, no-write cell adapter for the real Marcus & Millichap native detail
enricher and the real JLL rendered detail enricher. It does not change ordinary
collection, default concurrency, the strict 128-detail benchmark, or C10.
No calibration has been completed by adding this module. A live matrix and
reviewed adoption decision remain required.

## Measurement contract

Use one fresh, privately retained source inventory to select 8–128 immutable
members per source. Use 16 for the initial shared JLL matrix (the paired audit
caps at 24), and retain the
inventory response hash and each member's `sampleId`, native `providerId`,
`canonicalUrl` and complete `baseRow`. The base row preserves transaction,
price-withholding and other source controls. Hash the complete ordered cohort.
The original private inventory file is required: its exact bytes must hash to
`inventorySha256`, and every selected full row must match its original ID/URL
and source. Marcus accepts a real `collect.ts --monitor` artifact, never a full
or enriched listing artifact with detail fields stripped. JLL's ordinary monitor
intentionally emits no rows. Its optional guarded preparation uses the actual
`fetchJllSearchPage` adapter for one property-type/transaction/page 1 request,
retaining the original native response, parsed rows and clocks in a bounded
`cre_matched_inventory` artifact. It selects 8–24 members (default 16), and fails
if the page lacks that many; it never requests another page to fill the sample.
Preparation and workers independently reject altered native row projections.
`--max-inventory-age-hours` defaults to 2 and cannot exceed 24; each cell rechecks
the original clocks without relabeling them.

The JLL paired transport audit must share this cohort and the rendered capture;
do not make another rendered request just to obtain an audit document.

By default, run C1, C2, C4 and C8 against the same cohort in separate owned
worker processes, each with a new generation and empty private cache directory.
For a reviewed bounded C1/C2 experiment, set `max_concurrency: 2` in the private
matched profile and pass `--max-concurrency 2`. Both must match. An absent profile
ceiling preserves the legacy ceiling 8; the only supported explicit ceilings
are 2 and 8. The runner refuses unsupported or mismatched ceilings before
runtime admission or worker dispatch. Each execution exclusively writes
`plan.json` with the selected `concurrency_sequence`, ceiling, review reference
and project before dispatch, and repeats the sequence and ceiling in
`result.json`. A C2-only matrix cannot dispatch C4 or C8. The plan is private
experiment evidence, not a production concurrency adoption decision. The explicit cell
scheduler bypasses the ordinary CLI's six-client cap without changing it.
`peakActiveEnrichments` measures locally awaited real enrichment operations.
It does not measure browser pages, API jobs, provider requests, or host threads.
For example, eight clients against a four-job/four-page runtime may mostly wait
in a queue. Record actual API queue and browser-page peaks separately.

Each cell reports fresh accepted rows/minute, per-member enrichment latency,
nearest-rank p50/p95/p99/maximum, requested and actual enrichment width, the
cohort hash, generation and coordinator-supplied runtime/load evidence.
Normalized fidelity compares the complete listing, including prose, prices,
withholding controls, gallery ordering, documents, media and contacts. Only the
detail observation clock and generation are excluded. Broker references are
resolved to broker records before comparison. Any changed content stops the
matrix; a genuine provider update therefore produces an inconclusive matrix
instead of being accepted as a capacity win.

The isolated production-capture wrapper records client HTTP dispatches and
statuses and forwards the cell AbortSignal to the real HTTP path. A retry,
failed dispatch, response other than HTTP 200, redirect, response URL mismatch,
missing freshness/identity, cache hit or
fidelity mismatch stops new scheduling and aborts in-flight peers. One client
dispatch does not prove one physical provider request: API retries, engine
fallbacks, redirects and origin cache state remain unknown without separate
server evidence. JLL requests must demonstrably carry `maxAge: 0` and the real
parser must verify native property ID and canonical URL. Marcus copies base
identity fields in its ordinary parser, so this isolated wrapper additionally
requires exactly one matching canonical link in the actual consumed HTML body.
Missing canonical identity excludes the page; copied base fields cannot pass.

`onCaptured(member, detail)` supplies the actual normalized listing, broker
records and source-written JLL cache document to a private audit sink. The
document retains its original flattened `detailObservedAt`, `generationId` and
`cachedAt`. A cache-file read is not a second fresh acquisition. An offline
direct-document parser projection must retain `generation_cache` provenance
and must never contribute to fresh acquisition throughput.

## Foreground coordinator requirements

`cre_matched_capacity.py` supplies a foreground runner. It:

1. Hold the canonical CRE lock so a live full refresh or another calibration
   cannot overlap. Coordinate the window with the active full-series owner.
2. Admit fresh normal host memory and CPU below 90%, then run the production
   CPU guard at 90%/10 seconds/sample 2s and memory guard at warning 20s/sample 2s,
   with immediate critical-pressure stop. Guard telemetry failure stops work.
3. Resolves the selected Compose project, exact container IDs, API loopback
   binding and actual CPU/memory/PID/shared-memory limits. Record observed API
   queue cap and browser page cap with every cell. A profile name is insufficient.
4. Saves the existing five-second resource journal and required guard logs per
   trial, with cell/source/generation association, timing gaps, CPU,
   memory pressure/swap and owned-process/container usage. Darwin qualification
   retains its compressor counter requirement. Linux qualification requires
   available and total memory, consistent swap counters and the exact Linux
   telemetry scope and pressure-policy basis; unsupported Darwin counters are
   not synthesized. Required degraded fields, stale coverage, wrong container
   IDs and missing acquisition overlap still reject qualification. Record
   competing host work as unknown when it cannot be attributed safely.
5. Owns a bounded child process group and wall deadline, and retains cleanup
   uncertainty through lock release. AbortSignal forwarding is cooperative;
   it cannot prove a native call returned or that remote browser/queue work
   stopped. Require fresh queue-empty/browser-idle settlement and owned-runtime
   cleanup evidence after any failed or cancelled arm before proceeding.
6. Saves private full output for strict SQL dry-run with no database writes.
   Matched hashes alone do not prove the collector's full schema/cardinality,
   whole-inventory completeness, lifecycle behavior or database acceptance.

`runMatchedCapacityCell` is the real scheduler. `withProductionMatchedCapture`
connects it to the existing source enrichers. `runMatchedCapacityMatrix`
coordinates widths through a caller-supplied owned-cell function and stops at
its first rejection. The coordinator is responsible for resource, settlement,
wall-time and predeclared tail-latency acceptance. The foreground runner enforces
all of these before starting another cell. It records required two-second
queue/browser activity samples and sampled high-water lower bounds, read timing,
sample gaps and coverage limitations. These periodic samples can miss peaks
between observations. A failed activity read or evidence write stops later cells.

## Linux and Windows deployment boundary

The observer and required guards select the existing platform sampler. Linux
resource journals cover the Linux kernel/WSL VM and visible cgroups, not all
Windows applications. They cannot prove whole-Windows CPU/RAM peaks or continuous
Windows host safety. If an experiment makes Windows host claims, retain matching
Windows admission/observation evidence separately. Reusing the existing Windows
launcher admission sampling can establish start-time CPU/RAM headroom only; it
is not continuous observation and the guarded-tier launcher does not dispatch
this matched coordinator. No new resident observer is required by this change.

The owned runtime must already be reviewed and running in its own
`firecrawl-cre-*` Compose project. The ordinary shared `firecrawl` runtime remains
excluded. Within WSL use the explicitly reviewed local Unix Docker context and
pin its endpoint, daemon ID, container IDs, images, epochs, limits and queue/page
caps through the existing identity/profile contracts. A context name alone is
not daemon identity. `.wslconfig` is intended VM configuration; actual Docker
memory and runtime limits require readback. These changes do not start, build,
reconfigure or deploy any runtime, alter the legacy OrbStack profile documents,
or grant sealed C10 authority.

AGENTIC-4169 is offline engineering readiness. A real WSL preflight and rendered
provider-backed C1/C2 calibration must wait for the active full-series owner to
settle and for a separately reviewed deployment/window. Windows foreground
capture work (AGENTIC-4168) and independent exact-head Mac regression proof remain
separate prerequisites; adding these options is not a completed benchmark.
Direct JLL transport remains a separate admission under AGENTIC-2898, including
its strict parser/fidelity requirements. Do not enable the paired direct arm as
part of a rendered-only C1/C2 pilot.

## Foreground invocation

From `scripts/firecrawl-ops/cre_collector`, first validate a plan without runtime
calls by omitting `--execute`. The identity file uses `cre_owned_runtime.py`'s
private exact-container/daemon/epoch contract. The private profile must contain
`schema_version:1`, `kind:cre_matched_capacity_profile`, the exact project and a
`review_reference`, `api_queue_cap`, `browser_page_cap`, and `services` entries
for `api` and `playwright-service`. Each entry pins all five actual fields:
`cpu_limit`, `memory_limit_bytes`, `memory_swap_limit_bytes`, `pids_limit`,
`shm_size_bytes`. Names alone do not admit settings, and this command never
starts, rebuilds or reconfigures a container.

```sh
python3 cre_matched_capacity.py --prepare-jll --members 16 \
  --identity /private/owned-runtime.json --profile /private/reviewed-profile.json \
  --api-url http://127.0.0.1:3302 --browser-url http://127.0.0.1:3303 \
  --out /private/2026-10-12T120000Z-abcdefabcdef \
  --max-p95-ratio 1.5 --replicates 1 --cell-seconds 300 --total-seconds 1800
```

Replace the example paths/run ID with fresh private paths and predeclare the latency ratio
appropriate to the experiment; the example ratio is not an adoption decision.
For a C1/C2-only plan, add `--max-concurrency 2` and the matching private profile
field described above. Omit `--execute` to validate without creating output or
making runtime calls; execution saves the exclusive plan before dispatch.
Use `--cohort /private/cohort.json --inventory /private/original-inventory.json`
instead of `--prepare-jll` to reuse a fresh bound artifact. `--prepare-marcus`
uses the real sale monitor adapter: two inventory POSTs and one lightweight map
expansion per selected member, with an actual two-request cap and no heavy HTML
details. Marcus preparation accepts 8–128 members; JLL accepts 8–24. JLL preparation
defaults to sale/office and accepts explicit `--jll-transaction` and
`--jll-property-type` selections. `--execute` authorizes this invocation's network
work only after the canonical lock and reviewed runtime admission pass.

One or two replicates are exploratory; three cold replicates are required for
qualification review. The runner stops at the first worker, status, fidelity,
strict SQL dry-run, activity, guard, wall-budget or predeclared p95 regression.
Cell work defaults to 300 seconds (maximum 900); whole experiment work defaults
to 1800 seconds (maximum 3600). Bounded local/remote cleanup and guard quiescence
can extend the work deadline by up to 75 seconds when calls return; this does
not claim a hard deadline on native calls. Resource/timeout/unknown cleanup
retains the existing operator recovery fence. Failed runs write private typed
`failure.json`, child settlement records and exact-ID runtime stop evidence.
Success leaves the already-owned runtime running with fresh queue/browser-idle
readback and records that state. It does not activate a profile or write a DB.

`--paired-jll` invokes the included short-lived paired worker for one direct
arm after each completed rendered cell, using the recorded cell/doc clocks and
generation for `auditJllCapturedPair`. It does not replay rendering or add direct
work to the rendered cell's elapsed time. Missing paired worker, non-JLL source,
or any failed pair stops the experiment. The optional direct arm shares the
cell work deadline and canonical authority; no concurrent benchmark bypass is
provided.

## Runtime comparisons and adoption

First repeat the current cautious configuration without changing it: API 2 CPU,
8GiB, queue 4; browser 2 CPU,16GiB,4 pages,PID 384,8GiB shared memory. Treat these as
intended settings until exact runtime readback confirms them. A possible second
profile is the earlier trial's API 2 CPU/8GiB/queue 8 and browser 4 CPU/16GiB/8 pages/
PID 768/8GiB shared memory. That candidate requires a newly reviewed explicit
profile and isolated runtime identity; this document does not activate it.
Preserve pinned images, model/OCR/provider credentials, security and proxy
configuration. No build, pull, model handoff, OrbStack memory change or shared
runtime transition is implied.

Use at least three cold replicates before selecting a budget; counterbalance
the two reviewed profiles with baseline anchors to expose host/provider drift.
Stop the entire experiment at the first reliability/resource regression or
predeclared tail-latency rejection. Keep direct and rendered rates separate.
Only after single-provider qualification, test independent-provider pairing
as a separate factor using the checkpoint scheduler's provider lanes. Colliers
remains exclusive, provider pacing remains, and ingestion remains serial.

The old four-source PR #90 trial is not adoption proof: Transwestern failed in
both parallel and serial comparisons, so its strict no-write dry-run rejected
the cohort. CBRE's strict snapshot pins its own two-client width and a bounded
single inventory page cannot establish C1/2/4/8 detail scaling. Model/OCR
handoff work in AGENTIC-2280 and general Compose hygiene in AGENTIC-2261 remain
separate from this ordinary non-LLM acquisition experiment.


The canonical lock is durably armed with its existing benchmark interlock before
inventory preparation or the first detail worker. Abrupt coordinator death
therefore prevents stale-owner reclamation while children or remote work may
survive. The marker is cleared only after settled local children, freshly idle
or verified-stopped exact runtime, quiescent guards and durable terminal proof.
Unknown cleanup retains the existing operator recovery fence.

Calibration additionally requires saved usable host CPU/memory and exact-owned
API/browser CPU/memory samples, with read duration at most four seconds and
observed gaps at most ten seconds. Missing or degraded core evidence stops later
cells. Three-replicate qualification requires a saved sample read window that
overlaps each actual acquisition arm, including an opted-in direct paired arm.
Exploratory one/two-replicate runs may finish with usable lifecycle measurements
but no acquisition overlap; they explicitly report `measurement_incomplete`
and cannot support qualification or adoption. Short requests are not held or
retimed to fabricate sampling coverage. Periodic samples can still miss peaks.
