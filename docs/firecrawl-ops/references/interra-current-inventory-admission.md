Parent: [AGENTIC-1972](https://linear.app/agenticassets/issue/AGENTIC-1972). Engineering: [AGENTIC-4222](https://linear.app/agenticassets/issue/AGENTIC-4222).

# Interra additive current-inventory admission

Before live use, read the [legacy identity transition procedure](interra-identity-transition.md).
Complete current provider inventory does not authorize replacing historical IDs
or accepting duplicate database records.

The explicit checkpoint option `--admit-current-inventory-source interra-realty`
admits one complete current Interra inventory when its historical baseline is
absent. It is valid only with `--sources interra-realty --transactions both`,
unlimited full collection, a clean collector checkout, and without
`--admit-baseline-hold-additively` or `--allow-dirty`. The option is validated
before credentials or output creation. Disabled runs retain their existing
configuration; enabling, disabling or changing the source-policy digest on a
resume refuses the immutable configuration comparison.

This engineering change does not establish a successful live run. A root-owned
pilot still needs fresh sole-writer, resource and backend admission, reviewed
pushed code, SQL dry-run, additive ingestion, quality and exact generation
readback through the existing checkpoint machinery. No new scheduler, runner,
baseline write, status activation, retirement, DDL or OM write is authorized.

Both source and aggregate gates revalidate the exact artifact digest, clean
collector SHA, generation/start, source-policy digest and database target. The
source adapter's sale receipt records the completed uncapped terminal feed,
exact endpoint, response-text digest, genuine observation time and disjoint
available/under-contract/closed native-ID sets. The emitted set must equal the
available plus under-contract set by identity, including its compact sorted-ID
digest. Every emitted canonical listing must carry the corresponding lifecycle,
sale tenure, source-owned URL and same inventory/detail observation. The lease
pass supplies explicit sale-only empty-transaction evidence, without pretending
to fetch lease inventory or observe a lease timestamp. A receipt in a capped or
monitor artifact grants no admission.

The live target-bound gate must still report raw `first_seen` with its exact
no-baseline reason. A missing median alone proves nothing. Artifact errors,
truncation, conversion failures, staging discrepancies and all existing strict
freshness checks remain blocking. A baseline appearing before aggregate
admission refuses this mode, even if an ordinary gate would report `ok`.

The raw verdict, reason, floor and median remain visible. The admitted verdict
is `ok_additive_current_inventory`, its scope is `additive_current_inventory`,
and `mark_missing_safe` remains false. Successful checkpoints use status
`additive_scope_complete_current_inventory` and scope kind
`collector_registry_additive_current_inventory`. These labels distinguish
current provider inventory evidence from historical-baseline acceptance. The
existing freshness certificate rejects this scope; other missing-baseline
sources remain blocked. Receipt digests bind source-owned evidence and do not
claim an independent provider cardinality or cryptographic attestation.

Use the existing [checkpoint operating procedure](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md)
and preserve these explicit opt-in flags for the new reviewed invocation.

For the existing Windows launcher, use its explicit series option:

```powershell
./scripts/firecrawl-ops/cre_collector/windows/Invoke-CreGuardedTier.ps1 `
  -Tier series -Source interra-realty -AdmitCurrentInventorySource interra-realty `
  -ExpectedSha <reviewed-main-sha> -WorkDeadlineSeconds 1800 `
  -AcknowledgeDatabaseWrites
```

The matching Linux series option is `--admit-current-inventory-source interra-realty`
with `--sources interra-realty`. The series retains the explicit additive scope
and accepts only a child with the exact matching opt-in configuration and scope.
A completed Interra-only series does not publish whole-registry producer health.
Default runs, other sources and incremental tiers do not inherit the option.

Before listing upsert, ingestion refuses exact source-URL collisions between
Interra's numeric provider IDs and different existing, non-soft-deleted IDs of
the same brokerage. Legacy `listing/<slug>` IDs require a separately reviewed
identity or lifecycle repair; this admission never aliases, merges or retires
those rows. An absent legacy ID in a numeric-ID feed is not provider-absence
evidence. The guard runs under the existing transaction-wide lifecycle lock,
so its failure rolls back the transaction instead of leaving duplicate inserts.
