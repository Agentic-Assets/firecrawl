# C10 multisource capacity closeout (2026-09-15)

**Branch:** `feat/cre-c10-multisource-capacity`
**Base:** `main` at `db801fa551260e90e6139b6fbfd2d03af0f65048`
**Implementation range:** `16fb3567a14ade2a4b9690fa0c58daa65ec7b643` through
`df092d68f581126aef38f26bc8bc75ec8301e404`
**State:** [PR #66](https://github.com/Agentic-Assets/firecrawl/pull/66) is the
repository-integration candidate; no runtime or data mutation occurred.

## Goal

Create a safe, maintainable offline contract for a 20-source C10 capacity
experiment, without adding a generic live collector or changing the established
JLL benchmark path. The branch subsequently added sealed receipt substrate and
source-owned candidate producers; those additions remain offline-only and do
not change the Wave 1 execution boundary.

## What shipped

- `capacity_c10/` is a small, typed package with versioned policy/contracts,
  verified-only adapter admission, immutable v1 cohort binding, a canonical
  durable one-use arm ledger, an injected library-only coordinator that holds
  the existing `SharedLock` across runtime and recovery hooks, and a pure
  per-plane comparator.
- `cre_capacity_c10_v1.json` fixes the exact 20-source matrix and 12 strict
  detail / 8 authoritative inventory floor. The default registry names every
  source but intentionally admits none.
- `cre_capacity_c10_profiles_v1.json` isolates `c10-p0` and `c10-p1`; the
  central JLL experiment-profile configuration remains unchanged. The generic
  profile parser recognizes the strict C10 workload contract.
- Offline fixtures cover policy and cohort hashing, registry parity, exact
  cohort membership, no-write proof, P0/P1 resource whitelist, counterbalance,
  one-use arms, settlement/rollback/quarantine ordering, and comparison.
- The receipt package now seals private artifacts and size-bounded request-graph
  shards and has seven candidate inventory producers, strict-detail Batch A
  producers, and one Foundry Batch B producer. They describe source-specific
  enumeration cards and parsers. All seven inventory member paths are explicitly
  non-executable because their public targets return HTML and no source-owned
  identity parser is reviewed; fabricated JSON page fixtures are not treated as
  capability. No concrete direct-provider transport, CLI/controller integration,
  registry admission, or C10 live run was added. CBRE Deal Flow, Avison Young,
  Colliers Main, and the remaining Batch B sources remain explicit blockers.
- Browser-arm evidence binds each source to its immutable cohort member count
  and digest and derives throughput from source-level serial monotonic timing.
  Buildout candidates use the native `show_link` identity. CBRE Deal Flow was
  removed from the executable map rather than approximating its form POST/HTML
  protocol.
- Marcus member cards use the provider's `mappropertydetail` route and require
  native `PropertyDetail` / `PropertyUrl` evidence. Colliers list evidence uses
  and reconciles the provider-native `numProjects` page count.
- The coordinator derives the canonical shared CRE lock independently and
  treats the injected path only as an attestation. P0 can no longer bypass the
  collector lock merely because it does not call the transition hook.
- A new durable arm ledger accepts only the canonical empty session. A caller
  cannot skip the first P0 arm by presenting an advanced in-memory prefix.
- P1 rollback attestation compares the stable baseline transition fingerprint,
  profile, state, and verification result. It deliberately does not compare the
  runtime container snapshot digest, whose usage counters can change during a
  healthy arm.
- JLL strict-detail plans enumerate every exact transaction, property-type, and
  page stratum needed by the immutable cohort. The producer reconciles identity
  across the aggregate before any member request card is admitted.
- JLL Investor and Colliers likewise seal every exact page or map/list slice
  represented by the cohort and reconcile native identities across the combined
  responses. Marcus reconciles the complete map inventory to the provider's
  native count before it admits activity-bound member cards.
- Every strict-detail member expansion is anchored to the exact sealed
  enumeration event that exposed its native route, including members discovered
  only on later pages or slices.
- Execution and comparison revalidate the canonical policy digest, exact source
  set and schema, fixed 12/8 plane allocation, and exact P0/P1 resource tuples.
  A caller cannot make an arbitrary direct plan admissible merely by rehashing it.
- `admit_plan()` can issue a process-local capability only after the canonical
  repository authority approves one exact cohort digest and all twenty actual
  source-tree implementation digests. The checked-in authority approves none,
  so this wave cannot issue a plan. Execution, session, and comparison reject
  plain JSON copies or self-rehashed mappings even when their fields look valid.
- Buildout inventory cards use the provider-supported `q[s][]` array-form stable
  sort, matching the production adapter's cross-page ordering contract.
- Adapter admission now requires the exact concrete repository implementation
  type plus approval from immutable repository configuration. Implementation
  fingerprints hash actual verifier source files and shared dependencies rather
  than mutable flags, caller-provided values, or version labels.
- Marcus member receipts extract the native `DealId` from `PropertyDetail` and
  require it to match the selected provider identity before sealing evidence.
- JLL enumeration treats GraphQL card IDs as search identities, binds cohort
  members through enumerated canonical URLs, and independently verifies the
  numeric provider ID from each detail response.
- Implementation authority covers the complete collector Python/TypeScript
  source tree and package/config dependencies, including runtime, checkpoint,
  and multisource verification modules imported outside `capacity_c10/`.
- Browser evidence requires the reviewed `playwright` engine and exact
  per-source scheduled count/hash parity with every immutable cohort member;
  the aggregate scheduler count must equal the complete cohort.
- Comparison and durable-ledger commits accept only process-local coordinated
  arm capabilities issued after runtime validation or authenticated ledger
  recovery. Plain or subsequently mutated arm mappings fail closed.

## Verification

The final code candidate `df092d68f581126aef38f26bc8bc75ec8301e404`
passed the complete collector suites and static gates before this closeout-only
correction:

- `python3 -m pytest -q`: 3215 passed, 18 skipped.
- `npm test`: TypeScript typecheck passed; 908 passed, 1 expected
  platform skip, 0 failed.
- Changed Python: Ruff I/F, Ruff format, and `python3 -m py_compile` passed.
- `git diff --check` and the conflict-marker guard passed.

Seventeen exact-head Codex review passes produced thirty-four material findings. All
thirty-four were confirmed and fixed: immutable cohort binding, per-source timing,
Buildout `show_link`, fail-closed Deal Flow blocking, bounded member-graph
sharding, bounded cumulative request-accounting commitments, and recursively
immutable strict-detail plans, plus cohort-bounded qualified rows and durable
recoverable terminal-result evidence, independently derived canonical lock
ownership, fail-closed inventory HTML member paths, the Marcus native detail
endpoint/envelope, Colliers' native page-count field, and empty-ledger-only
durable session creation, stable-transition rollback attestation, and aggregate
JLL enumeration across exact cohort strata, plus complete JLL Investor pages,
Colliers slices, and Marcus map inventory, with each member graph expansion
bound to the enumeration event that exposed it, plus canonical fixed-policy
revalidation and an admission-issued cohort/adapter capability at the execution
and comparison boundary, plus Buildout's provider-supported array-form stable
sort for cross-page inventory enumeration, Marcus native `DealId` verification
from `PropertyDetail`, and a non-substitutable repository authority that pins a
real cohort and source-byte-derived verifier implementations before admission,
plus JLL's distinct search-card and numeric detail identities, complete imported
dependency coverage, complete per-source scheduled cohort parity, and an exact
reviewed browser engine, plus coordinator- or ledger-issued arm evidence at
comparison and terminal-commit boundaries.
The threads were answered and resolved.
GitHub Actions are not used as the primary completion proof. The exact final PR
head and fresh review status must still be read back after this documentation
correction before merge.

## Decisions made

- Keep C10 profile definitions separate from the central JLL profile file.
  Adding them to the central file made JLL benchmark profile selection
  ambiguous, so isolation preserves existing selection semantics.
- Do not make the v1 cohort hash a shape-only claim. Admission recomputes the
  published multisource-v1 hash recipe before binding cohort membership.
- Do not add a generic adapter, executor, or fallback. Every source must later
  provide reviewed native enumeration, member verification, and attrition
  classification before it can enter a live plan.
- Reuse runtime ownership instead of duplicating it. The library-only
  coordinator invokes the canonical `SharedLock`, capacity runtime,
  settlement, rollback, and quarantine behavior through explicit injected
  hooks. It intentionally provides no CLI, concrete browser transport, or live
  source admission.

## Deliberately deferred

No native live adapter, concrete direct-provider transport, receipt CLI,
concrete browser executor, source call, database/cache/status/scheduler/model/
OCR change, registry admission, or experiment arm is included. Runtime and
canonical-lock orchestration exists only as an injected library seam; no
shipped command can invoke it as a live C10 run. The request-card,
private-artifact, and durable arm-ledger seals are not execution evidence.
Concrete host/browser integration, source admission, and any live run require
a separately reviewed integration wave and explicit operator approval.

## Left to the operator

Review and merge PR #66 only after normal review gates. Any live C10
run also needs independently reviewed adapters, canonical runtime hook wiring,
fresh operator admission, and the existing production safety gates.
