Parent: [AGENTIC-1972](https://linear.app/agenticassets/issue/AGENTIC-1972); implementation and approval: [AGENTIC-4306](https://linear.app/agenticassets/issue/AGENTIC-4306).

# DAUM verified-subset admission

DAUM's ordinary complete-detail admission remains the default. Its separate
verified-subset path permits additive refresh of proved records while leaving
records with explicitly admitted incomplete detail evidence untouched. This is a DAUM-specific
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

The existing flag defaults to `tenure-only-v1`, preserving that contract. The
separate `--daum-subset-policy tenure-and-empty-title-v2` (Windows
`-DaumSubsetPolicy tenure-and-empty-title-v2`) requires the subset flag too. It
selects scope `daum_verified_subset_v2`, proof schema `daum-verified-subset-v2`
and exact policy metadata through configuration, cache, both gates and the
ingestion claims. Complete admission and V1 do not inherit this permission.

## What must be proved

Two fresh, complete inventories must match. Every member must then have a fresh,
valid detail response, owned canonical URL, literal property-path identity,
positive native post ID, response hash and observation time. Only after those
checks may the collector partition the inventory:

- **Emitted:** sale, lease or both are positively resolved. Only these records
  enter ordinary staging.
- **Held:** inventory and detail tenure are both unknown. The sole allowed
  V1 reason is `unknown_inventory_and_detail_tenure`.

V2 additionally permits `empty_required_property_title` only when a successful
detail has exactly one empty cleaned visible `.main-section .title-property`,
one matching owned observed canonical URL and one unambiguous native shortlink.
Missing or multiple empty title containers still fail. The normal full parser
for nonempty titles is unchanged. No head-title fallback or placeholder facts
enter staging. Identity and recognized tenure conflicts still fail before
partitioning. An empty-title hold preserves the resolved tenure, including a
recognized Lease, but excludes that parent from both emitted transaction sets.
This reason takes priority if tenure is also unknown, counting the hold once.

The empty-only `titleObservations` tuples `[postId, 1, "empty"]` are unique and
ordered by inventory URL. Their IDs must equal exactly the empty-title held
IDs, joined to the complete existing cohort and detail hash/time receipts.
These are producer observations, not independent HTML authentication by the
Python gates: a consistently false bound observation can conservatively
withhold a member; it cannot authorize its writes or claim full coverage.

The partition must be exhaustive and disjoint. Native IDs, paths and URL
associations must remain consistent across emitted and held records and all
relevant historical records. Changed slugs do not authorize aliases or rekeys;
missing historical native provenance remains unknown. At least 100 unique
emitted properties are required. A property offered for sale and lease counts
once toward that floor.

Transport errors, invalid responses, conflicting tenure, ambiguous identities,
resource or cleanup failures, and database uncertainty fail the whole attempt.
They are not incomplete-detail holds. Never infer sale/lease from an address, title or a
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
partial scope. If any holds exist and no lease records are emitted, report
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
