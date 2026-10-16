Parent: [AGENTIC-1972](https://linear.app/agenticassets/issue/AGENTIC-1972); implementation and approval: [AGENTIC-4306](https://linear.app/agenticassets/issue/AGENTIC-4306).

# DAUM verified-subset admission

DAUM's ordinary complete-detail admission remains the default. Its separate
verified-subset path permits additive refresh of proved records while leaving
records with unresolved transaction tenure untouched. This is a DAUM-specific
permission, not a general exception for incomplete sources.

## Selecting the scope

The shared runner uses `--admit-daum-verified-subset`; the Windows wrapper uses
`-AdmitDaumVerifiedSubset`. Use the existing guarded checkpoint/series procedure
in [START_HERE](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md).
The normal complete admission flag does not authorize a subset. Do not combine
the two DAUM permissions or supply the subset environment variable manually to
bypass the runner. The same collection, validation, staging and ingestion code
runs on macOS and Windows/WSL; native launching is the host-specific boundary.

The exact DAUM admission entry carries
`admission_scope: "daum_verified_subset_v1"`. Collector proof uses schema
`daum-verified-subset-v1` and kind `verified_subset_with_holds`. Complete and
subset acquisitions cannot reuse each other's cached result, including failures.
Selecting this scope always produces a partial result, even when the held set
is empty. Changing a receipt label cannot turn it into complete-source proof.

## What must be proved

Two fresh, complete inventories must match. Every member must then have a fresh,
valid detail response, owned canonical URL, literal property-path identity,
positive native post ID, response hash and observation time. Only after those
checks may the collector partition the inventory:

- **Emitted:** sale, lease or both are positively resolved. Only these records
  enter ordinary staging.
- **Held:** inventory and detail tenure are both unknown. The sole allowed
  reason is `unknown_inventory_and_detail_tenure`.

The partition must be exhaustive and disjoint. Native IDs, paths and URL
associations must remain consistent across emitted and held records and all
relevant historical records. Changed slugs do not authorize aliases or rekeys;
missing historical native provenance remains unknown. At least 100 unique
emitted properties are required. A property offered for sale and lease counts
once toward that floor.

Transport errors, invalid responses, conflicting tenure, ambiguous identities,
resource or cleanup failures, and database uncertainty fail the whole attempt.
They are not tenure holds. Never infer sale/lease from an address, title or a
previous listing merely to increase throughput.

Both admission gates bind the exact artifact, code/policy, target database,
generation and start. The artifact worker passes the validated evidence through
a private claims file; ingestion independently checks it again before writing.
A raw subset artifact is not an ordinary ingest input.

## Database preservation

Inside the existing guarded transaction, before the unchanged upserts, refuse
the entire apply if an emitted existing parent is deleted or not active, or an
emitted existing source-index row is soft-deleted or not explicitly present.
Unknown flags also refuse. An absent index row is not a prior absence claim;
initial observation insertion is permitted after identity checks.

Held parents, all five child sets and index rows stay unchanged. Existing
emitted lifecycle flags, presence generation and presence-change time stay
unchanged; ordinary observation timestamps and legitimate emitted child
replacement are measured separately. No baseline, activation, retirement,
mark-missing, OM or schema changes follow from this permission.

## Reading the result

Reports must distinguish **verified subset refreshed** from **whole brokerage
complete**. Show emitted and held counts, verified sale/lease counts and the
partial scope. If unknown holds exist and no lease records are emitted, report
zero verified subset leases with whole-brokerage lease coverage unknown. That
is not evidence of an empty lease inventory.

Subset proof must not advance complete-inventory watermarks, last-good full
receipts or full-source certificates. Keep it separate from complete-source
counts in scorecards. Collection counts and staged rows are not refreshed
database counts; qualified emitted-generation readback supplies that evidence.

## Operational acceptance and recovery

Code tests or a merge do not establish a successful production refresh. A
supervised pilot needs a reviewed deployed version, fresh ownership/resource
checks, complete scoped before/after preservation snapshots, all 14 validation
families and no-regression checks, exact emitted-generation readback, and
settlement of the original process and its children.

Preserve failed evidence. Before commit, a refusal rolls back normally. For an
unknown commit, use the existing recovery procedure instead of replaying. A
committed run that fails qualification remains failed and fenced for reviewed
reconciliation; never automatically delete or rewrite data to hide that result.
This admission does not install a schedule or change another computer's setup.
