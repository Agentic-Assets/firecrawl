Parent: [AGENTIC-1972](https://linear.app/agenticassets/issue/AGENTIC-1972). Implementation: [AGENTIC-4271](https://linear.app/agenticassets/issue/AGENTIC-4271).

# Buildout current-inventory admission

The explicit current-inventory option accepts one reviewed registered Buildout
source per invocation. It reuses the existing additive checkpoint transaction
and preserves the original `first_seen` result. It neither seeds a baseline
nor treats an unproven feed as complete. Ordinary runs retain the historical
baseline gate, and the existing historical freshness certificate rejects this
different admission scope.

The shared public coordinates are in
`scripts/firecrawl-ops/cre_collector/data/cre-buildout-sources.json`. The
TypeScript registry, Python admission and Windows option parity checks bind the
same 25 source definitions. Registration is engineering support; each brokerage
still requires its own fresh provider proof, historical identity audit and
guarded database readback before operational acceptance. SVN, Lee & Associates
and Franklin Street are outside this new admission allowlist.

## What the proof establishes

Both sale and lease passes retain the same complete feed, with transaction
labels. The receipt records plugin/source binding, stable query order, every
page's provider total/limit, exact index and endpoint, original observation
time, generation, transport and accurately named parsed-JSON digest. The digest
describes `UTF8(JSON.stringify(parsed provider response))`, not original HTTP
bytes, a provider signature or independent database evidence. The Firecrawl
fallback records the requested Buildout coordinate; its JSON helper does not
expose the final redirect URL. The receipt does not attest that final URL.
A validated same-generation page cache retains its original observation and provenance;
old cache entries without this proof cannot acquire it merely by being read.

Provider `meta.total` counts all raw feed rows, including closed listings and
both transactions. Admission proves unique native IDs and exact disjoint
closed, nonclosed sale and nonclosed lease partitions. Missing or malformed
flags do not become assumed false values for admission. A zero-length
transaction must follow from that complete partition. An entirely empty active
source is refused. Missing proof can leave ordinary collection output usable;
it cannot pass the opt-in admission gate.

Emitted feed IDs and staged property identities are different counts. Existing
`cre_ingest.to_row` normalizes the listing URL's `propertyId` and removes its
sale/lease suffix. Admission calls that real transform and merge: a property
may have one sale and one lease native row, but unexplained same-mode or larger
collisions refuse. Missing URLs, foreign listing hosts and rejected staged rows
also refuse. No ID is guessed, aliased or migrated by this path.

The receipt proves current authoritative inventory, including additive card
fields. It does not prove freshly rendered property detail or refreshed
historical contacts. Existing child collections are preserved. Status
activation, mark-missing, baseline updates, OM extraction and DDL remain off.

## Run only after the source's identity audit

Deploy a clean reviewed SHA to the existing runtime, verify its database target
and sole-writer state, and inspect the source's historical identity namespace
before its first live write. For one qualified source, for example:

```powershell
pwsh -NoProfile -File scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 `
  -Tier series -Source faris-lee -AdmitCurrentInventorySource faris-lee `
  -ExpectedSha <full-reviewed-deployed-sha> -WorkDeadlineSeconds 3600 `
  -AcknowledgeDatabaseWrites
```

This performs additive database writes; it is not a dry-run command. A different
source, `all`, a source list or a transaction subset cannot share this opt-in.
Source and aggregate gates independently recheck exact artifact, generation,
code, shared source coordinates, policy, approved database target and raw live
baseline absence. If a baseline appears or a binding drifts, admission refuses.

Require SQL dry-run success, the completed additive transaction, full quality
no-regression, generation-exact staged-identity readback and settled ownership.
Begin with one small complete feed, then bounded waves of at most five firms
under AGENTIC-1972. A successful source does not qualify the other feeds or the
full 51-source union. The separately scoped Windows scheduling and cooperative
stop procedure remains in the [guarded tier guide](guarded-incremental-tiers.md).
