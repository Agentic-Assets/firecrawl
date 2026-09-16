# C10 capacity experiment contract

Wave 1 is intentionally an offline admission and evidence protocol. It now
also includes sealed receipt substrate and source-owned candidate producers,
but it does not provide a general collector CLI, generic network transport,
scraper, public controller integration, or generic source adapter. No
TypeScript component opens a provider connection or can execute a request by
itself: the Linux-only Python host coordinator owns the lock, sealed card
manifest, Compose lifecycle, capability signer, and private artifacts.

`policy.py` seals the fixed 20-source, 12/8-plane matrix. `adapters.py`
requires an exact registry where every source-specific adapter is explicitly
reviewed and fully verified. `admission.py` binds that registry, a hash-bound
multisource-v1 cohort, and the isolated `c10-p0`/`c10-p1` configuration into an
immutable plan. `runner.py` is a pure, data-only session helper: it has no
runtime hooks, subprocesses, lock access, or callback-driven execution path.
`compare.py` is pure and can only produce an operator-review candidate, never
an executable adoption decision.

The receipt producers do not change this admission boundary. Inventory
producers, strict-detail Batch A producers, and the Foundry Batch B producer
can construct and seal source-specific request graphs when a future reviewed
coordinator supplies an implementation of the transport interface. They are
not registered in a live collector, and no producer is evidence of adapter
admission or of a completed C10 run. `candidate_registry()` remains an
unverified review surface and `default_registry()` remains empty.

The executable path is `capacity_c10.production`, not an injectable runner.
It durably claims an arm before any host activity, holds the canonical
`SharedLock` through runtime preflight, P1 candidate transition, host execution,
settlement, rollback, terminalization, and quarantine, and invokes only the
existing `cre_capacity_runtime` controller. Its only admitted alternate runtime
profile is the canonical `cre_capacity_c10_profiles_v1.json`, named with
`experiment_kind="C10"`; the ordinary controller retains its historic default
profile behavior.

The former TypeScript JLL executor, lifecycle preflight, and local browser
constructor have been removed. A host creates a sealed, plan-bound JLL card
registry only from the canonical GraphQL enumeration recipe and the exact
sixteen hash-bound JLL cohort members. It issues the enumeration and all
sixteen member capabilities before the bounded P0/P1 scheduler begins. The
narrow issued-capability child can perform only one host-issued card and cannot
receive or create a lock, keypair, receipt store, Compose configuration, or an
arbitrary card. The host accepts a terminal success only after it has verified
signed cleanup-complete evidence and derived the exact 4/10 active-lease peak
from sidecar lease intervals.

The only production entrypoint is `python -m capacity_c10.production`. It
defaults to a local-input-only dry run. `--execute --smoke` runs one sealed
16-member P0/P1 arm only after its canonical runtime preflight, and P1 also
requires a fresh approval plus admission path. All CLI paths are canonical
roots: `--runtime-receipt-root`, `--approval-root`, and `--admission-root`.
After the coordinator claims the next durable arm under `SharedLock`, it alone
derives `arm-N.json` beneath each root. The dry run validates that exact next
arm's receipt output and, for P1, approval/admission files; an approved P1
smoke therefore executes the same `approval-root/arm-N.json` file.
`--execute --counterbalanced` runs the fixed eight-arm sequence and requires
one approval and admission file per P1 arm. It constructs the host registry
itself and accepts no browser callback, arbitrary card, or caller scheduler
evidence.

The durable ledger path is derived solely from the canonical shared-lock root
and immutable plan digest. It is not a CLI argument. Claim and terminal state
are one atomically replaced ledger record;
the terminal record retains the safe authenticated comparator envelope and
artifact manifest hashes, never browser bodies. Replaying an unadvanced session
or selecting an alternate ledger fails closed. One monotonic deadline begins
before preflight and is carried through host execution, settlement, rollback,
and terminalization. The compatibility facade `host_session.py` exposes the
minimal public API; crypto, ledger, registry, sidecar, and orchestration live
in focused host modules.

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

The production coordinator binds the existing public components, without
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

This foundation is not whole-cohort browser-fidelity proof. The present
20-source matrix remains a compatibility and review panel. The JLL-only lane
has local browser-route evidence; every other source still requires independent
reviewed browser-path, cache instrumentation, and scheduler proof.
Direct-native receipts remain non-comparable compatibility evidence.
Authenticated JLL host evidence is terminalized separately and remains
`not_comparable_pending_authenticated_20_source_evidence`; no missing source is
converted into an assumed throughput observation.

## Candidate receipt hooks

`strict_detail_jll.py`, `strict_detail_jll_investor.py`,
`strict_detail_colliers.py`, `strict_detail_colliers_main.py`,
`strict_detail_marcus_millichap.py`, and
`strict_detail_avison_young.py` are source-local receipt verifiers and
no-write request descriptors only. `candidate_registry()` exposes them for
review alongside the other C10 candidates, but all retain
`fully_verified = False`; neither `default_registry()` nor plan admission can
execute them. A later registry-only admission change must independently prove
each adapter's private receipt root, actual no-write transport, and
source-specific attrition behavior; it must not turn these fixtures or
descriptors into a generic fetcher.

The TypeScript receipt package additionally has source-owned inventory
producers for seven authoritative-inventory sources and one Batch B
producer for Foundry. The remaining Batch B sources, together with Avison
Young, Colliers Main, and CBRE Deal Flow, retain explicit blockers. CBRE Deal
Flow remains blocked until its provider-derived engine key and form-urlencoded
POST/HTML ListingEngine protocol can be represented without weakening the
sealed request-card boundary. These candidates have no
concrete direct-provider transport, CLI or controller wiring, verified-registry
entry, admission, or live-run evidence.
