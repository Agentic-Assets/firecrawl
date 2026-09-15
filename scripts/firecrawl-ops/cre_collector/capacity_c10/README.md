# C10 capacity experiment contract

Wave 1 is intentionally an offline admission and evidence protocol. It now
also includes sealed receipt substrate and source-owned candidate producers,
but it does not provide a general collector CLI, generic network transport,
scraper, public controller integration, or generic source adapter. The sole
concrete exception is the reviewed JLL browser library described below: it
uses a private loopback listener, a short-lived one-time signed capability,
and an injected held coordinator lock. No ordinary C10 component opens a
provider connection or can execute a request by itself.

`policy.py` seals the fixed 20-source, 12/8-plane matrix. `adapters.py`
requires an exact registry where every source-specific adapter is explicitly
reviewed and fully verified. `admission.py` binds that registry, a hash-bound
multisource-v1 cohort, and the isolated `c10-p0`/`c10-p1` configuration into an
immutable plan. `runner.py` owns only the serial one-use arm ordering and
requires injected settlement, rollback, and quarantine evidence. `compare.py`
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

The reviewed JLL browser library is the single source-specific exception to
the former interface-only substrate. `receipts/jll_browser.ts` binds an exact
ordered 16-member JLL cohort, its digest, all 17 request cards, and a held C10
coordinator lock to either a one-member fidelity smoke or a 16-member P0/P1
saturation calibration. It can only use the private loopback Playwright
executor and fails before execution unless the source cohort, arm binding,
card hashes, Linux receipt-store support, sidecar health, and coordinator lock
are all present. `local_operator_preflight.ts` provisions a generated shared
sidecar/coordinator secret only for a callback and restores the environment on
every exit. It never writes, logs, or persists that secret. These are library
entrypoints deliberately, not ordinary collector commands.

The coordinator seals a browser arm only when it carries the plan/config and
requested-profile digests, private runtime receipt digest, container snapshot
and transition fingerprints, monotonic timing, and saturation evidence. P0
must demonstrate four active scheduled members and P1 ten, with at least that
many scheduled members. The comparator derives qualified rows per minute from
that sealed timing and row count. It rejects direct/native transport,
cache reads/writes, fallback/multiple attempts, caller-supplied throughput
scalars, and unsaturated cohorts.

Wave 2 must bind the runner hooks to the existing public components, without
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
producers for the eight authoritative-inventory sources and one Batch B
producer for Foundry. The remaining Batch B sources, together with Avison
Young and Colliers Main, retain explicit blockers. These candidates have no
concrete direct-provider transport, CLI or controller wiring, verified-registry
entry, admission, or live-run evidence.
