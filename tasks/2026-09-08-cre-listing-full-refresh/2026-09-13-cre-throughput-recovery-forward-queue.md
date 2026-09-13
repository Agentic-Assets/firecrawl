# CRE throughput follow-up menu

Checked 2026-09-13. Linear owns the actionable queue; this is an evidence-linked
menu, not another scheduler or work ledger. Implementation proof is in the
[closeout](2026-09-13-cre-throughput-recovery-closeout.md).

## Required acceptance

- **Full immutable run and database proof** (P1, verified remaining work;
  [AGENTIC-2902](https://linear.app/agenticassets/issue/AGENTIC-2902)). Run all 51
  sources from the admitted pushed SHA, retain exact per-source readbacks and
  publish completeness only when every required source succeeds. Track failed,
  interrupted, stale and unavailable sources separately. The old 2/51 run is
  not completion proof and must not have its generation clocks rewritten.
- **Visible continuation ownership** (P1, explicit user request; AGENTIC-2902).
  After merge and local main synchronization, create the local Terra 5.6 high
  task and retarget the paused 40-minute heartbeat. Preserve the instruction to
  message the root task on problems and stay quiet on unchanged healthy state.

## Evaluation and data fidelity

- **Classify supplemental rendered JLL differences** (P2, verified measurement
  gap; [AGENTIC-2898](https://linear.app/agenticassets/issue/AGENTIC-2898)). Required
  native assets and documents match; some extra rendered links/images do not.
  The later three-page probe did not reproduce a delta. Capture a bounded
  reproducible difference and establish listing ownership before changing
  harvesting or deleting any existing child rows.
- **Measure whole-source C4 throughput** (P2, verified scope limit;
  [AGENTIC-2900](https://linear.app/agenticassets/issue/AGENTIC-2900)). The successful
  32-page proof does not establish whole-source or full-registry duration.
  Record queue wait, fresh/cache counts, CPU high windows, p95 latency, provider
  retries and sampled memory/PIDs throughout the full run. Do not claim 10x/20x.
- **Preparation/ingest separation for true source parallelism** (P2, hypothesis;
  AGENTIC-2900). The production series deliberately keeps one source generation
  active. A separately reviewed preparation lane could overlap slow reads while
  retaining exact per-source identity, freshness and serial database commit.
  Benchmark before implementing; multiple independent writers are not an option.

## Simplification and integration

- **Extract parent recovery evidence plumbing if it earns its keep** (P3,
  maintainability hypothesis; [AGENTIC-2897](https://linear.app/agenticassets/issue/AGENTIC-2897)).
  The parent now has extensive admission and serialization code. After live
  acceptance, consider a small typed evidence module to remove duplication,
  preserving the new adversarial tests and manifest compatibility. Do not
  refactor while an immutable series is running in this checkout.
- **Isolated upstream update** (P2, verified integration complexity;
  [AGENTIC-2901](https://linear.app/agenticassets/issue/AGENTIC-2901)). The fetched
  candidate has 23 conflicts across a large fork divergence. Integrate on a
  separate coherent branch with protected custom-path comparisons and relevant
  API tests; do not merge it merely to increase CRE speed without evidence.
- **Model transition and second ordered fallback** (P3, explicit desired setting
  but not this HTML bottleneck). Root env and running API routing differ. Use
  the human-reviewed operator handoff for a model transition; a second ordered
  backup needs explicit implementation and tests, not a comment. Keep separate
  from the non-LLM full listing run and shared API stability.

## 2026-09-13 accuracy and retained-evidence follow-ups

- **Define raw-evidence retention precisely** (P1, verified code gap;
  AGENTIC-2902). `raw_data` is a normalized/pruned listing; source caches and
  selected nested payloads do not provide a universal original-response archive.
  Inventory exact source evidence, sizes, retention and business relevance before
  expanding storage. Keep original literals and extraction provenance without
  duplicating credentials or irrelevant page chrome into consumer facts.
- **Distinguish missing from explicitly false** (P1, verified code behavior;
  AGENTIC-2902). `lib/util.ts:prune` drops false/null/empty values, so emitted
  absence is ambiguous. Do not change that generic helper blindly: SQL existence
  checks treat flags such as `detailError` by presence. Evaluate field-specific
  preservation with complete source/ingest fixtures before altering payloads.
- **Measure field yield without inventing completeness** (P2, evaluation;
  AGENTIC-2902). Start from retained artifacts, segment exact source/transaction/
  mode/generation, and label counts `emitted_nonempty` with missing/pruned unknown.
  Source-native expected fields and applicability need their own reviewed
  contract. The new performance reporter exposes known detail-error and
  inventory-only counts but does not establish field completeness.
- **Source-stated address candidates before internet fan-out** (P2, verified
  extraction gap plus proposed design; AGENTIC-2902). Generic JSON-LD enrichment
  does not extract PostalAddress. Test exact listing-bound address candidates,
  per-component provenance, conflict preservation and country/state/ZIP
  consistency before admission. Stated country/county/address2/listing_date are
  not all staged by current ingest; review both live consumers before changes.
  Keep Census-derived geography distinct. Internet/geocoder enrichment should
  then be evaluated on a bounded sample with identity precision and incremental
  valid field yield, never silently overwrite authoritative brokerage facts.
- **Broaden resource/request measurement only when useful** (P3, coverage gap;
  AGENTIC-2900). Current snapshots cover shared Firecrawl helpers, JLL detail
  cache and sampled Node resources, not direct-provider requests, queue wait or
  browser/API usage. Keep future measurement out of the critical CPU-guard
  sampling loop and avoid a separate persistent telemetry service.
