# C10 reviewed admission chain

## Purpose and boundary

This is the missing, reviewable bridge between source-owned C10 receipt
evidence and the existing offline `cre_capacity_multisource_v1` cohort
validator.  It includes a bounded compatibility capability report, but
deliberately does **not** include a generic transport, a C10 runtime entrypoint,
or a listing pipeline.  No production provider invocation is wired or run.
Its only writes are new `0600` immutable review artifacts below an explicitly
provisioned, owner-only root.  In particular, the legacy JLL capacity benchmark
is neither an input nor a source in this flow.

The fixed twenty-source matrix, its source contracts, and candidate adapters
remain the only source inventory.  The capability report walks each existing
candidate descriptor and records its current reviewed/unreviewed state without
opening a transport.  A real source coordinator is still missing: it must
invoke the existing source-owned `ReceiptProducer` using a
`SourceBoundOneShotTransport` and make one bounded, no-retry request-card
attempt per declared card.  Its future output will need one immutable outcome
per fixed source, including an explicit unavailable/blocked outcome where the
descriptor permits it.  That status is never eligible evidence.

This distinction matters: the current candidate registry has twenty
non-admitting validators, but the admission chain wires **zero** source
producers or transports.  Some separate TypeScript receipt modules contain
source-owned candidate producers while other sources are explicit blockers;
none is an admission-chain collector.  Consequently a 20-source collection
output is a compatibility panel, **not** an admissible browser cohort.  Treating
blocked output as a success, adding a generic HTTP fetcher, or treating a JLL
benchmark artifact as a receipt would break the existing fail-closed contract.

## Canonical flow

1. An operator runs `provision-roots` for the receipt and admission roots.
   Each requested path must be one new leaf below an existing owned,
   non-symlink, non-group/world-writable parent.  The command uses
   descriptor-relative creation and rechecks the root inode, ownership, and
   exact `0700` mode before every read or write.  It never changes an existing
   root's permissions.
2. `collect-panel` walks every fixed descriptor exactly once and emits only an
   immutable, non-eligible capability outcome.  It is not a receipt collector.
   No blocked, partial, stale, or malformed result can be represented as an
   eligible receipt.
3. A later, separately reviewed source coordinator obtains exactly one fresh
   enumeration and bounded selected member receipts through the source's
   existing request-card contract.  Every card is allowlisted, one-shot,
   `no-store`, bounded, and has no retry/fallback.  Source-specific verifier
   artifacts are placed in the provisioned receipt root and referenced by one
   `cre_capacity_multisource_v1_receipts` manifest.  The PR provides no
   production provider connection.  It is the missing implementation required
   before a full twenty-source collection can be proposed for review.
4. `build-bundle` reads that manifest only from the supplied private receipt
   root, checks its declared root matches, invokes the existing offline
   prevalidator, and writes one new immutable admission bundle.  The bundle
   commits to the raw manifest bytes and private-root identity, the resulting public cohort,
   fixed-policy digest, and the exact current per-adapter implementation
   digests.  Partial, stale, malformed, symlinked, or tampered artifacts fail
   before bundle publication.
5. `render-authority` consumes only a sealed bundle and produces the exact
   canonical authority JSON proposal.  It reopens and revalidates the named
   private receipt manifest before rendering, and cannot write the checked-in
   authority.  A human reviewer must make the resulting authority pin a
   separate reviewed Git change: that pin names the one cohort digest, every
   current adapter/tree digest, and the exact plan digest after `admit_plan`.
   This avoids a caller-selected artifact root or public object becoming
   authority.
6. The existing `capacity_c10.production` dry run then validates canonical
   roots and exact authority.  An `--execute` invocation remains a separate
   runtime/admission/approval gate; it is unavailable until every source
   adapter is independently reviewed and the repository authority is pinned.

The immediately viable admission lane is **JLL-only**: the current host already
has a sealed JLL card registry and exact sixteen-member execution path.  It
needs a dedicated JLL-only authority and plan contract, rather than weakening
the fixed 20-source cohort.  The twenty-source panel remains compatibility
evidence until every source gets a reviewed producer, verifier, attrition rule,
and browser path.  This branch keeps the existing 20-source policy untouched
and labels any panel output non-admitting.

The proposed JLL-only contract is intentionally a new scope, not a `20 -> 1`
escape hatch.  It will have a distinct `kind`, fixed source set `{"jll"}`, the
existing canonical GraphQL enumeration digest, exactly sixteen hash-bound
members, the current JLL adapter-tree digest, and a dedicated authority file
that cannot authorize a 20-source plan.  Its producer must use the existing
host-issued JLL card registry and one-shot browser protocol.  A review command
will reconstruct the JLL plan and render its one-source authority proposal.
The production host must reject a JLL-only plan until that contract is added
and independently reviewed; this PR does not silently reinterpret the current
twenty-source `PLAN_KIND`.

At no point in collection or admission may the flow write a database, cache, listing,
status, scheduler, model, or OCR configuration.  The review bundle records
that exact no-write contract.  It is evidence for code review, not approval to
contact a provider or run C10.

## Operator commands

```bash
# Safe local preparation only; both paths must be new leaf directories.
python -m capacity_c10.admission_chain provision-roots \
  --receipt-root /absolute/trusted-parent/c10-receipts \
  --admission-root /absolute/trusted-parent/c10-admission

# Hermetic/review-only: creates the 20-source compatibility outcome panel.
# It has no production provider transport in this PR.
python -m capacity_c10.admission_chain collect-panel \
  --receipt-root /absolute/trusted-parent/c10-receipts

# Offline only: validates existing private artifacts and emits one sealed bundle.
python -m capacity_c10.admission_chain build-bundle \
  --receipt-root /absolute/trusted-parent/c10-receipts \
  --receipt-manifest /absolute/trusted-parent/c10-receipts/manifest.json \
  --admission-root /absolute/trusted-parent/c10-admission

# Offline only: render an authority proposal for review; it cannot modify Git.
python -m capacity_c10.admission_chain render-authority \
  --bundle /absolute/trusted-parent/c10-admission/<bundle>.json
```

The bundle and compatibility-report commands create only immutable `0600`
artifacts; failed validation creates no bundle.  `provision-roots` can create
only fresh, empty owner-`0700` roots.  `build-bundle` is deterministic over a fixed
admission time supplied by the operator or test harness; live use supplies the
current UTC time and thus rejects stale enumerations.
