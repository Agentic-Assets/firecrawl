# C10 capacity experiment contract

Wave 1 is intentionally an offline admission and evidence protocol. It now
also includes sealed receipt substrate and source-owned candidate producers,
but it does not provide a runnable CLI, a concrete network transport, a
scraper, a collector/controller command integration, or a generic source adapter. The receipt
transport is an injected interface: no shipped C10 component opens a provider
connection or can execute a request by itself.

`policy.py` seals the fixed 20-source, 12/8-plane matrix. `authority.py` loads
the one non-substitutable repository authority and fingerprints the actual C10
verifier source tree and shared dependencies. That authority currently approves
no cohort and no adapter, so `admission.py` cannot issue a plan. A future
reviewed commit must pin one exact cohort digest and all twenty current
implementation digests before `admission.py` can bind the registry, cohort, and
isolated `c10-p0`/`c10-p1` configuration into an immutable plan. `runner.py`
owns serial one-use arm ordering and a library-only
coordinator that requires injected runtime, browser, settlement, rollback, and
quarantine hooks. Before preflight, it atomically persists each arm claim in an
owner-only, FD-identity-checked session root derived solely from the canonical
shared-lock location and immutable plan/session identity. Callers cannot select
another ledger. An interrupted claim remains unresolved after quarantine
recovery and cannot be replayed. Terminalization atomically retains the sealed
result and digest in that ledger, so a caller crash cannot consume the only
recoverable copy. `compare.py`
is pure and can only produce an operator-review candidate, never an executable
adoption decision.

The receipt producers do not change this admission boundary. Inventory
producers, strict-detail Batch A producers, and the Foundry Batch B producer
can construct and seal source-specific request graphs when a future reviewed
coordinator supplies an implementation of the transport interface. They are
not registered in a live collector, and no producer is evidence of adapter
admission or of a completed C10 run. `candidate_registry()` remains an
unverified review surface and `default_registry()` remains empty.

The executable-foundation seam is deliberately still library-only. Its
`run_one_coordinated_arm()` coordinator receives explicitly injected runtime,
browser, settlement, and quarantine hooks; it has no CLI and does not arm or
call the local API by itself. It holds one `SharedLock` from preflight through
browser execution, settlement, P1 rollback, and quarantine. Its only admitted
alternate runtime profile is the canonical
`cre_capacity_c10_profiles_v1.json`, named with `experiment_kind="C10"`; the
ordinary controller retains its historic default profile behavior.

The coordinator seals a browser arm only when it carries the plan/config and
requested-profile digests, private runtime receipt digest, container snapshot
and transition fingerprints, immutable per-source cohort count/hash bindings,
serial per-source monotonic timing, and saturation evidence. P0
must demonstrate four active scheduled members and P1 ten, with at least that
many scheduled members. The comparator derives each source's qualified rows
per minute from that source's sealed interval and a row count constrained to
the inclusive `0..cohort_member_count` range. A zero-rate arm remains valid
evidence but cannot pass the operator-review threshold. It rejects
direct/native transport,
cache reads/writes, fallback/multiple attempts, caller-supplied throughput
scalars, and unsaturated cohorts.

The future live binding must use the existing public components, without
duplicating them:

- `cre_checkpoint_refresh.SharedLock` for exclusive ownership and retained
  quarantine authority.
- `cre_capacity_runtime.preflight` and `cre_capacity_runtime.transition` for
  resource admission, P1 transition, and verified P0 restoration.
- `cre_capacity_telemetry` settlement parsers through the runtime's established
  idle proof.
- `cre_capacity_runtime.recover_quarantine` for the operator-only recovery
  path.

No C10 adapter may be admitted until its enumeration verifier, member verifier,
and provider-specific attrition classifier are independently reviewed. A failed
or uncertain arm must quarantine under the held canonical lock; it must not
attempt a fresh lock acquisition, a generic fallback, or an automatic rerun.

This foundation is not browser-fidelity proof. The present 20-source matrix
remains a compatibility and review panel. A browser-sensitive primary
comparison cannot run until source-specific adapters prove the reviewed browser
path, single engine attempt, cache controls, and scheduler activity.
Direct-native receipts remain non-comparable compatibility evidence.

## Candidate receipt hooks

`strict_detail_jll.py`, `strict_detail_jll_investor.py`,
`strict_detail_colliers.py`, `strict_detail_colliers_main.py`,
`strict_detail_marcus_millichap.py`, and
`strict_detail_avison_young.py` are source-local receipt verifiers and
no-write request descriptors only. `candidate_registry()` exposes them for
review alongside the other C10 candidates, but all retain
`fully_verified = False`; neither `default_registry()` nor plan admission can
execute them. Mutable adapter fields are not admission authority. A later
repository-authority change must pin a reviewed cohort and actual source-tree
digests only after independently proving each adapter's private receipt root,
actual no-write transport, and source-specific attrition behavior; it must not
turn these fixtures or descriptors into a generic fetcher.

The TypeScript receipt package additionally has source-owned inventory
producers for seven authoritative-inventory sources and one Batch B
producer for Foundry. The remaining Batch B sources, together with Avison
Young, Colliers Main, and CBRE Deal Flow, retain explicit blockers. CBRE Deal
Flow remains blocked until its provider-derived engine key and form-urlencoded
POST/HTML ListingEngine protocol can be represented without weakening the
sealed request-card boundary. These candidates have no
concrete direct-provider transport, CLI or controller wiring, verified-registry
entry, admission, or live-run evidence.
