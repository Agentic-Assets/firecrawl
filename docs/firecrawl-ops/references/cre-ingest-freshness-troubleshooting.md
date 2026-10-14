Parent: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895). Provenance fix: [AGENTIC-4194](https://linear.app/agenticassets/issue/AGENTIC-4194).

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
