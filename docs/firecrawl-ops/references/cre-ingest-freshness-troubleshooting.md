Parent: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895). Provenance fix: [AGENTIC-4194](https://linear.app/agenticassets/issue/AGENTIC-4194). Corrective replay: [AGENTIC-4195](https://linear.app/agenticassets/issue/AGENTIC-4195).

# Diagnose ingestion freshness failures

Collection, SQL execution and acceptance are separate checks. A valid source
artifact and an ingest exit code of zero do not establish that every expected
identity retains its original freshness proof in the database. Keep the final
generation readback and absolute-quality result visible.

Compare the artifact's flat listing count, its normalized unique identity count,
and the database's exact-generation count. Sale and lease passes can merge, so
the flat count is not the expected database count. A generation-count gap can
come from missing identities, source classification, soft deletion, or lost
provenance. Test these explanations instead of lowering the expected count.
The current `freshness_generations` query excludes soft-deleted rows; it does
not restrict listings to `status='active'`. Preserved sold or leased labels
alone do not explain a gap.

Source validation runs before Python staging. Privacy projections and merge
logic must preserve valid `inventoryObservedAt`, `detailObservedAt` and
`freshnessProvenance.generationId` evidence through `to_row` and `merge_rows`.
Withheld JLL pricing remains redacted. Retain the original validated timestamp
strings and bounded non-monetary generation token; do not generate a new clock
or copy arbitrary provider prose into freshness fields. For merged passes,
verify each pass's evidence and the persisted-freshness consumer's selection.

## Source visibility is not missing data

An explicit instruction to withhold a value takes precedence over ordinary
sparse-value retention. A transient parse miss should keep a previously good
price; a validated current withholding control must prevent that price from
surviving through the normalized columns, sale/lease merge, current raw data,
Markdown or child display titles. Trace the actual source, staging, generated
SQL and database readback instead of checking source omission alone.

The contracts are source-specific. Avison's `hide_price` accepts an absent
control or the exact numeric values `0` and `1`; strings, booleans, null and
contradictions refuse. Its `avisonPriceVisibility` envelope retains only the
versioned control, never a hidden amount or a matching hint. The ingestor
independently validates the direct source provenance and actual brokerage.
A marker nested in unrelated metadata cannot grant destructive authority.
Either withheld sale/lease pass wins their merge; a later valid public detail
observation can restore a public asking price.

Avison's current normalized clear is limited to `sale_price_usd` and
`sale_price_per_sf`. Do not copy JLL's broader financial projection: retain
independently provided lease terms, rent, cap rate, NOI, GRM, per-unit/per-acre
facts, original area units and useful nonprice metadata. Unfamiliar pricing
with no established sale-versus-lease meaning remains an explicit ambiguity;
harmless new metadata is not a reason to reject a record.

The source-owned projection removes asking aliases and sanitizes classified
prose and child titles. It uses a bounded financial-clause grammar, not an
assumption that every number or currency expression is asking price. Preserve
explicit rent bases, independent financial labels, addresses, phones,
licenses, source IDs and resource URLs. Do not regex-rewrite serialized JSON
or discard a whole description because one clause contains asking price.
This is not a claim to classify every possible unlabelled numeric sentence.

For a degraded detail observation, apply the projection to the payload the
existing retention rules select. Preserve its useful prior detail facts and
original proof clocks while retaining the current safe inventory control.
Withholding is not evidence that the detail request succeeded. Retained child
IDs, URLs and membership stay intact; title cleanup must run after additive
writes so old labels cannot return through `COALESCE`.

Use producer-to-staging tests, both merge orders and actual generated-SQL
acceptance for newly withheld values, visible recovery, malformed controls,
foreign markers and retained raw/Markdown/child paths. Source code proof
does not establish fresh brokerage acceptance or authorize historical cleanup,
lifecycle, baseline, OM, schema, scheduling or Mac changes. The bounded Avison
fix and evidence belong to [AGENTIC-4330](https://linear.app/agenticassets/issue/AGENTIC-4330).

Use a genuine standalone no-hardlinks clone for offline staging comparisons.
Large artifacts still consume the same host's memory, so bound the comparison's
CPU and memory and avoid overlapping full suites with production collection.
Compare normalized identities and business/privacy fields before and after a
fix. A read-only expected-ID database join can then distinguish persisted
identities from incomplete provenance without returning prices or raw bodies.

## Recover without inventing freshness

Preserve the failed manifests, controls, guard logs, validation and original
artifact hash before any recovery. Ordinary same-SHA checkpoint resume reuses
a valid collected artifact, but skips replay of a source already recorded as
ingested with an admitted gate. A newer SHA or Linux memory-policy version
cannot resume that old generation. Do not edit saved states or receipts to
force replay.

A corrective replay needs a separately reviewed invocation with the canonical
filesystem lock, resource guards, original target binding, strict coverage and
SQL dry run, original freshness budget, and a check for newer targeted
observations. The ordinary ingest CLI does not provide all these protections.
Full-artifact replay can refresh every eligible listing and child collection
and advance database `updated_at`; it is not a metadata-only patch. Prove the
exact scope before writing, then obtain identity/provenance readback, privacy
and status checks, and quality comparison. Keep the old failed checkpoint
unchanged and record recovery evidence separately.

A successful recovery proves persistence of the saved observations. It does
not create a newly collected generation, resolve unrelated historical quality
findings, or authorize lifecycle changes or recurring scheduling. Follow the
[checkpoint guide](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md)
and the live Linear issue for the approved operation.

## Respect observation and lifecycle clock semantics

For a replay of already-present source-index rows, check `last_seen` and
`last_enumerated_at` against each saved row's original observation time.
Reject missing or invalid required observations and any newer targeted
observation. Preserve present/non-soft-deleted state and compare the full
source-index semantic fingerprint before and after writing.

`presence_changed_at` records a presence transition, rather than enumeration
freshness. The [lifecycle migration](../../../scripts/firecrawl-ops/sql/016_cre_listing_lifecycle.sql)
allows NULL and seeds presence state without backfilling that timestamp.
Canonical ingestion preserves the old transition timestamp when presence
remains true. Historical NULLs can therefore be valid. Check a transition
timestamp for invalid or newer values when it exists, retain NULL in the
semantic comparison, and never fabricate a transition to satisfy a recovery
check. An extra non-null requirement can reject healthy observations without
improving the replay's protection against overwriting newer data.

## Distinguish stale telemetry from memory pressure

`host_memory_telemetry_failed` means the required observer could not supply
valid, timely evidence. It does not establish high memory use. Check the
sample timestamps, phase, incident evidence and owned-process settlement
before changing resource limits or retrying.

Start memory monitoring immediately after its admission sample, before the
blocking CPU preflight, as the canonical runners do. A private recovery helper
initially waited through a two-second CPU preflight and then a two-second
memory sampling interval. Its first regular sample arrived four seconds after
admission, exceeding the unchanged three-second freshness budget. Moving
monitor startup fixed the ordering; an elapsed-time regression reproduced the
old failure. Instantaneous mocked CPU samplers alone did not catch it.

Keep the failed invocation and its inputs intact. Confirm guard shutdown,
positive owned-process settlement and canonical ownership state before a
separately admitted retry. Preserve the existing telemetry thresholds and
original observation clocks; a startup-order fix does not justify bypassing
the guard or reclassifying another incident as the same cause.
