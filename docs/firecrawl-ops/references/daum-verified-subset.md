Parent: [AGENTIC-1972](https://linear.app/agenticassets/issue/AGENTIC-1972); implementation and approval: [AGENTIC-4306](https://linear.app/agenticassets/issue/AGENTIC-4306).

# DAUM verified-subset admission

DAUM's ordinary complete-detail admission remains the default. Its separate
verified-subset path permits additive refresh of proved records and retains identity-proved
records with unknown tenure. This is a DAUM-specific
permission, not a general exception for incomplete sources.

## Selecting the scope

The shared runner uses `--admit-daum-verified-subset`; the Windows wrapper uses
`-AdmitDaumVerifiedSubset`. Use the existing guarded checkpoint/series procedure
in [START_HERE](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md).
The normal complete admission flag does not authorize a subset. Do not combine
the two DAUM permissions or supply the subset environment variable manually to
bypass the runner. The same collection, validation, staging and ingestion code
runs on macOS and Windows/WSL; native launching is the host-specific boundary.

New shared-runner and Windows-wrapper subset invocations default to `tenure-and-empty-title-v2`, with scope
`daum_verified_subset_v2` and proof schema `daum-verified-subset-v2`.
The explicit `tenure-only-v1` policy remains available, and a resumed run
uses its recorded policy when none is supplied. V1 receipts retain their
original contract. Complete and subset acquisitions cannot reuse each other's
cached result, including failures. Either subset always reports partial,
even with no holds; it cannot supply complete-source proof.

## What must be proved

Two fresh, complete inventories must match. Every member must then have a fresh,
valid detail response, owned canonical URL, literal property-path identity,
positive native post ID, response hash and observation time. Only after those
checks may the collector partition the inventory:

- **Known tenure:** sale, lease or both are positively resolved.
- **Unknown tenure:** when both inventory and detail tenure are unknown but
  identity and title are proved, emit once with a null transaction type. The
  sale pass is a transport carrier and does not establish a sale. Existing
  known transaction types and prior source evidence are preserved; observed
  children are added without removing prior children or inventing a primary.
  The verifier reconstructs the fixed writable projection from the retained
  inventory and detail observations and rejects unexpected or altered fields.
  Ambiguous prices are not promoted. The existing ingestor may normalize the
  bound source size text; the full parsed observations remain in the evidence.

The proof retains the existing `held` classification and parsed observations
for audit, and separately lists emitted unknown IDs in `retainedUnknownTenure`.
Historical proofs without the list retain their original hold-only behavior.

For V2, when one visible detail title is empty, the search-card title may
supply the title only if the unique owned document title exactly corroborates
it, the canonical URL and native post ID match, and tenure is resolved. The
parsed detail and inventory card remain in raw evidence. The title alone does
not establish a street address; canonical address and locality remain null when
the fallback is used. If that title
cannot be corroborated, the identified record remains an
`empty_required_property_title` hold. Missing or multiple title containers
still fail. Unknown-tenure fallback and records with no usable card title are
not yet admitted by this path.

The empty-only `titleObservations` tuples `[postId, 1, "empty"]` are unique and
ordered by inventory URL. Their IDs must equal exactly the empty-title held
IDs, joined to the complete existing cohort and detail hash/time receipts.
These are producer observations, not independent HTML authentication by the
Python gates: a consistently false bound observation can conservatively
withhold a member; it cannot authorize its writes or claim full coverage.

The proof membership and emitted staging must reconcile exactly. Native IDs, paths and URL
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

## Held inventory and detail observations (audit only)

Subset V1 and V2 proofs retain `heldInventoryObservations` for the exact held
IDs. Each URL-sorted observation contains the already-parsed 16-field search
card, copied independently into each transaction proof; a new proof emits an
empty array when there are no holds. Historical artifacts may omit the field
and remain compatible, but that omission does not prove their held-card data
was retained.

`heldDetailObservations` independently retains `{postId, detail}` for that same
held set and order. Unknown-tenure holds retain the complete existing parsed
17-field detail: source facts, title/location, tenure text, economics,
description/highlights, document and photo references, and published broker
contacts. Empty-title holds retain only the parser's existing five-field typed
marker; no missing fields are reconstructed. Both transaction proofs own deep
copies of their observations. New proofs emit `[]` when there are no holds;
historical proofs may omit either audit field. Complete and monitor output
remain unchanged.

Python checks present observations against the independently derived held
partition, with exact typed shapes, IDs, canonical URLs and tenure/text bindings
to the existing cohort and detail receipts. A malformed present audit refuses;
audit values cannot alter the partition or rescue a missing required fact.

These are integrity-bound parsed observations, not authenticated raw HTML or
all provider attributes. The retained audit observations do not independently
supply fallback facts or whole-source coverage. A fresh producer may separately
emit an identity-proved unknown-tenure row, bound by `retainedUnknownTenure`;
old audit-only artifacts cannot be converted into new emitted rows. The existing artifact
byte hash and paired-proof equality bind both fields; there is no separate
retention digest or admission rule.

For replay that needs primary response bytes, follow the
[independent verification capture procedure](development-and-runtime-checkouts.md#independent-verification-captures)
and preserve permitted bodies, metadata, clocks and safe request provenance
before removing a temporary clone. A hash or parsed observation cannot restore
a discarded response, establish a fresh source fact or replace that evidence.

As dated historical context, the retained October 19 qualification recorded
557/560. The historical audit field alone does not newly qualify 29589, 52023,
or 52271, and does not authorize retrying the closed 403 route. These facts do
not establish current status. Credentials, schedules, and Mac state are
outside this contract and remain unchanged.

## Database preservation

The source-bound fallback partition holds conflicting, terminal, stale or
otherwise unqualified targets locally; its locked transaction recheck refuses
a changed partition. Existing exact targets retain their populated scalars,
lifecycle, source index and children. Their full staged observation is appended
as encoded raw evidence with a digest; new targets enter the normal additive
path. Later ordinary upserts preserve that history. This stores parsed detail
and unit HTML/text, not the complete original HTTP response body. Other
ordinary subset identities retain their existing cohort-wide safety checks.

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

The normal pipeline creates its artifact-compute directory privately before
writing admission evidence. If an existing compute path is public, linked, or
owned by another user, the producer refuses it; it does not change permissions
to make the path usable. Preserve a failed mixed-stage run and reconcile that
exact generation before deciding what to run next. Resume still requires the
saved generation's original code SHA and configuration. Pipeline resume is
unsupported; only serial resume is supported under that exact-generation
contract. A new SHA is not a reason to rewrite a manifest or replay its cached
artifact.

Preserve failed evidence. Before commit, a refusal rolls back normally. For an
unknown commit, use the existing recovery procedure instead of replaying. A
committed run that fails qualification remains failed and fenced for reviewed
reconciliation; never automatically delete or rewrite data to hide that result.
This admission does not install a schedule or change another computer's setup.
