Linear parent: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895); implementation: [AGENTIC-4151](https://linear.app/agenticassets/issue/AGENTIC-4151).

# CRE validation profiles

The checkpoint runs a database audit before collection and a full audit after
ingest. The before-collection profile executes the eight query families its
consumers need, avoiding repeated diagnostics that are only used after ingest.
Both profiles use one repeatable-read, read-only transaction. Credentials,
database target binding and query-result parsing follow the same path.

| Profile | Use | Evidence |
|---|---|---|
| `full` (default) | Standalone validation, post-ingest audit and recovery | All normal validation queries, historical child diagnostic and optional exact artifact-job query |
| `checkpoint-preflight` | Checkpoint before collection only | Lifecycle schema contract, source counts, duplicates, bad child URLs, primary child conflicts, orphans, quality by source and child counts |

The retained SQL is identical between profiles. Lifecycle admission, unsupported
active-source detection and before/after regression comparisons still receive
their required query families. Inventory fingerprints, freshness generations,
queue health, search smoke, totals and historical diagnostics remain in the
full audit. A preflight report cannot replace that audit or certify freshness.

From `scripts/firecrawl-ops/cre_collector/`, inspect the explicit profile with:

```bash
python3 cre_validate.py --env-file "$HOME/.config/cre/equire.env" \
  --profile checkpoint-preflight --format json

# Normal standalone and post-ingest validation retain the full default.
python3 cre_validate.py --env-file "$HOME/.config/cre/equire.env" --format json
```

The checkpoint selects the reduced profile automatically only for its initial
pre-validation call. Do not manually substitute a reduced report into final or
recovery evidence. JSON declares `validation_profile` and `audit_scope`; reduced
reports use `checkpoint_before_collection_only`. Missing required query families
are rejected. Existing hash-bound full preflight snapshots remain compatible.

[AGENTIC-4186](https://linear.app/agenticassets/issue/AGENTIC-4186) optimizes
the shared source-attribution SQL to avoid reading raw JSON for non-folded brokerages when the governed
brokerage mapping has exactly one source key equal to its legacy slug fallback.
Multi-source brokerages retain prefix and nested-payload precedence. This changes
neither query families nor snapshot, target, quality or freshness requirements.
The opt-in `tests/run_source_key_postgres_contract.py --out <new-private-file>`
compares a frozen pre-optimization SQL oracle with current attribution and grouped
validator queries on synthetic data, then measures three counterbalanced runs.
It requires the pinned cached PostgreSQL image and uses an isolated, bounded
container with no ports or network; synthetic timings are not production gains.

[AGENTIC-4281](https://linear.app/agenticassets/issue/AGENTIC-4281) reuses one
per-row decompressed JSON root in the full freshness-generation query. Its
empty-path JSONB extraction and lateral fences retain the existing payload
precedence, SQL/JSON null handling, timestamp errors and complete output. The
fingerprint query, validation profiles and single read-only snapshot are
unchanged. Synthetic PostgreSQL parity and timings do not establish a production
speedup; compare a later admitted run before making that claim.

## Measure the effect

Compare `query_timings` and command duration from saved reports. Timings are
psql client elapsed values, including result handling. Profile selection does
not change individual SQL text or promise an identical query plan, cache state
or elapsed time on a subsequent run.

The first two pre-validation reports in Windows series
`2026-10-13T183401Z` recorded 153.63 and 135.60 seconds of query timings.
The seven omitted diagnostics accounted for 76.79 and 71.21 seconds respectively.
These are historical full-profile measurements, not a measured reduced-profile
speedup. Benchmark a later immutable deployment before claiming wall-clock gains;
do not change an active series or run competing database benchmarks during it.

For rollback, deploy the previous tested commit after the active run has settled.
Never edit an active runtime checkout or rewrite its validation artifacts.
