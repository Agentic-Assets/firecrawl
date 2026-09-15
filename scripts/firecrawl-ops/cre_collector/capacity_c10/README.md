# C10 capacity experiment contract

Wave 1 is intentionally an offline admission and evidence protocol. It does
not provide a CLI, a scraper, or a generic source adapter.

`policy.py` seals the fixed 20-source, 12/8-plane matrix. `adapters.py`
requires an exact registry where every source-specific adapter is explicitly
reviewed and fully verified. `admission.py` binds that registry, a hash-bound
multisource-v1 cohort, and the isolated `c10-p0`/`c10-p1` configuration into an
immutable plan. `runner.py` owns only the serial one-use arm ordering and
requires injected settlement, rollback, and quarantine evidence. `compare.py`
is pure and can only produce an operator-review candidate, never an executable
adoption decision.

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
and transition fingerprints, monotonic timing, and saturation evidence. P0
must demonstrate four active scheduled members and P1 ten, with at least that
many scheduled members. The comparator derives qualified rows per minute from
that sealed timing and row count. It rejects direct/native transport,
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

## Wave 2 strict-detail batch A hook

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
