# Eight actionable findings from the CRE consolidation safety review

**Status:** All eight repository-level repairs are implemented and locally
verified as of 2026-07-15. Production deployment, scheduler activation,
database changes, and live launchd observation remain separate Cayman-approved
operator gates and were not performed by this repair branch.

**Reviewed branch:** `fix/cre-consolidation-safety` at
`5208335a15d8fdae6a569c141b53942d23d38779`, compared with `origin/main` at
`c74ece4964e9ec2082516ef2ca6b6d856fd5f399`.

This file records the eight findings that survived independent finder and
skeptic review, plus their repair proof. Production deployment, scheduler
activation, database changes, and consumer rollout remain separately gated.

## Current-state refresh (2026-07-15)

The original review reproduced all eight findings before repair. The current
branch rejects marked and legacy retired OM artifacts before staging, separates
legacy index alignment from the generic migration runner, and closes the six
operator and documentation gaps described below.

## Resolution evidence (2026-07-15)

| # | Resolution | Verification |
| --- | --- | --- |
| 1 | `cre_ingest.py` rejects both the marked diagnostic envelope and the legacy `externalId` plus `omFacts` artifact before identity or scalar staging. Ordinary brokerage scalars remain supported. | Full Python collector suite: 1,416 passed. Focused artifact tests cover rejection, no fallback identity, and normal brokerage ingestion. |
| 2 | `000_run_all.sql` no longer invokes `015`; the legacy alignment script defaults to a PostgreSQL exception unless the explicit psql opt-in is set. | Disposable PostgreSQL proved fresh `013`, refusal without mutation, approved alignment, and idempotent second execution. |
| 3 | The operator runbook has a separate Cayman approval gate naming the coordinator, owner, exact job labels, configuration, observation window, and rollback. | Documentation review and final adversarial re-review. |
| 4 | Failure webhook delivery is synchronous and bounded while the original tier exit code is retained. | Behavioral shell-wrapper test plus shell syntax checks. A live launchd failure remains an activation-gated production proof. |
| 5 | Status reconstructs each tier with its own installed environment-file and alert-file paths. | Behavioral drift tests cover alert-free, alert-enabled, real mismatch, and distinct per-tier paths. |
| 6 | Gate 1 now requires an open, clean, mergeable, locally verified PR and keeps literal merge approval separate. | Documentation review and final adversarial re-review. |
| 7 | `cre_status.sh --expected-sha` reports branch, HEAD, dirty state, and exact-match status without changing the checkout. | Behavioral tests cover matching, wrong, and dirty Git checkouts. |
| 8 | GitHub Actions is manual-only, aa-hub is non-operational, and the historical workflow refuses live cutover. | Static regression guard plus documentation and final adversarial re-review. |

Additional verification: TypeScript typecheck and 479 unit tests passed;
`git diff --check` and relevant `bash -n` checks passed. No production write,
DDL, scheduler load, or live launchd cutover was attempted.

The surrounding cross-repository state has changed since the baseline review:

| Surface | Current evidence | Meaning for this checklist |
| --- | --- | --- |
| EQUIRE market context | PR [#418](https://github.com/Agentic-Assets/CRE_EQUIRE/pull/418) merged `feat/cre-listing-market-context` into EQUIRE `main`; repository records say its three DDL migrations and two caches were applied | Do not describe EQUIRE DDL as future work. The remaining gates are producer correctness, crosswalk adoption, cache health, and consumer adoption. |
| GetCREdata | Parser-version and unattended-hardening branches are now contained in GetCREdata `main` | The producer-code repair is no longer an unmerged-branch task, but applying its revised `cre_market_index` definition to shared `credeals` still requires explicit DDL approval and proof. |
| Context Engineering ownership branch | `docs/cre-data-object-ownership` remains unmerged and is behind current `main` | It is proposed guidance, not canonical policy or live authorization. It must be rebased and corrected before adoption. |

**Scheduler-policy correction.** GitHub Actions must remain manual-only. The
current company policy also treats aa-hub as historical source and runbooks,
not an execution control plane. Therefore no unattended scheduler should be
enabled until Cayman approves a named, current-policy-compatible execution
surface and owner, with rendered configuration, observation evidence, and
rollback. References below that call aa-hub the approved future scheduler are
historical and must not be used as activation authority.

## Priority summary

| # | Priority | Finding | Primary risk |
| --- | --- | --- | --- |
| 1 | P1 | Retired Firecrawl OM artifacts can still write listing scalars | Duplicate or unauthorized OM-derived listing data |
| 2 | P1 | The master SQL runner executes gated migration `015` | Unapproved production index rebuild |
| 3 | P1 | The operator runbook omits a distinct scheduler-load approval | Production schedules can be activated without the required gate |
| 4 | P2 | The failure webhook is backgrounded under launchd | Failure alerts can be killed before delivery |
| 5 | P2 | Alert-enabled plists always appear drifted | False unhealthy status and operator noise |
| 6 | P2 | Gate 1 requires the PR to remain draft | The documented merge path is impossible and stale |
| 7 | P2 | Gate 3 claims `cre_status.sh` verifies the checkout SHA | The wrong deployed commit can satisfy the listed evidence |
| 8 | P2 | Scheduler language conflicts with current company policy | An unapproved GitHub Actions or aa-hub scheduler can be activated |

## 1. Retire the OM scalar writer, not only the OM-facts child rows

**Where**

- `scripts/firecrawl-ops/cre_collector/cre_ingest.py:918-921`
- `scripts/firecrawl-ops/cre_collector/cre_ingest.py:942-953`
- `scripts/firecrawl-ops/cre_collector/cre_ingest.py:749-763`
- `scripts/firecrawl-ops/cre_collector/om_parse.py:394-418`

**What happens**

The new safety boundary discards `omFacts`, which prevents Firecrawl artifacts
from writing `cre_listing_om_facts`. It does not discard the high-confidence
OM scalars that `om_parse.py` also places on the listing object. A dry-run or
legacy OM artifact passed manually to `cre_ingest.py --in` can still stage and
write `noi`, `capRatePct`, `occupancyRate`, `units`, and `yearBuilt`.

There is an identity problem in the same path. The retired OM artifact emits
`externalId`, while `to_row()` reads `id`. The ingestor therefore falls back to
`url:<sha1>` and can insert a duplicate listing rather than update the intended
row.

**Recommendation**

Treat the presence of the retired OM artifact signature as a fail-closed input
case. Either reject the artifact or strip every OM-promoted scalar before
staging. Keep normal brokerage-source scalars working. Also remove or clearly
quarantine `build_enriched_listing()` so the supported dry-run path cannot
produce an artifact that looks safe to ingest.

**Required proof**

Add an artifact-to-ingest regression test using the real `om_parse.py` artifact
shape. Assert that no OM scalar or `om_facts` value reaches the stage table, no
fallback listing identity is created, and normal brokerage scalar ingestion is
unchanged.

## 2. Remove gated migration `015` from the generic master runner

**Where**

- `scripts/firecrawl-ops/sql/000_run_all.sql:110-111`
- `scripts/firecrawl-ops/sql/015_align_om_facts_conflict_key.sql:8-10`

**What happens**

Migration `015` says it must not be applied to production without schema-owner
approval and a maintenance-window decision. The generic documented master
runner includes it unconditionally. On a legacy installation, running
`000_run_all.sql` drops and recreates the live OM-facts unique index without a
separate acknowledgement.

Fresh installations already receive the five-column key from migration `013`,
so they do not need the legacy alignment operation.

**Recommendation**

Remove `015` from the generic runner until approval is recorded, or make it
require an explicit psql opt-in variable that defaults to refusal. Keep the
migration idempotent for legacy installations.

**Required proof**

Test these paths separately in disposable PostgreSQL:

1. A fresh schema reaches the five-column key through `013` without invoking
   the legacy index rebuild.
2. A legacy four-column schema refuses alignment without the opt-in.
3. An approved opt-in aligns the index and remains idempotent on a second run.

## 3. Add a distinct Cayman scheduler-activation gate

**Where**

- `tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md:31`

**What happens**

The runbook authorizes runtime recovery and a bounded five-row canary in
separate gates. The next row then requires scheduled enrich, monitor, and
weekly cycles, but it does not require a separate Cayman scheduler-load
approval. Calling the actor an authorized operator does not record the missing
authorization.

**Recommendation**

Insert a named gate between the canary and observation phases. It should
require Cayman's explicit approval to load the exact monitor, enrich, and
weekly jobs, identify the scheduler surface, and restate that the retired daily
tier must remain unloaded.

**Required proof**

The runbook and Linear handoff should record the approval, exact job labels,
rendered configuration, loaded-state evidence, and rollback command before any
scheduled cycle is accepted as evidence.

## 4. Make webhook delivery survive the launchd process lifecycle

**Where**

- `scripts/firecrawl-ops/cre_collector/launchd/cre_run_tier.sh:192-196`

**What happens**

`notify_failure()` backgrounds the curl pipeline inside the wrapper's exit
trap. The launchd templates do not enable `AbandonProcessGroup`. The macOS
launchd contract kills remaining processes in the job's process group when the
main job exits, so the alert can be terminated before the network request
finishes.

**Recommendation**

Run curl synchronously with the existing bounded timeout while preserving the
original tier exit code. Do not enable `AbandonProcessGroup` only to keep this
request alive unless the broader orphan-process lifecycle is explicitly
reviewed.

**Required proof**

Exercise a controlled failing tier under a real launchd job. Prove one webhook
delivery, prove the tier retains its original nonzero exit code, and prove no
curl process remains afterward.

## 5. Include the alert-file path in installed-plist drift checks

**Where**

- `scripts/firecrawl-ops/cre_collector/cre_status.sh:337-348`
- `scripts/firecrawl-ops/cre_collector/launchd/install_launchd.sh:121-139`

**What happens**

The installer supports `CRE_ALERT_WEBHOOK_FILE` and writes that non-secret path
into a rendered plist. `cre_status.sh` reconstructs the expected plist using
only `CRE_ENV_FILE`. Every correctly configured alert-enabled plist therefore
appears drifted, produces a warning, and makes the health command exit nonzero.

**Recommendation**

Read `CRE_ALERT_WEBHOOK_FILE` from each installed plist and pass it to the
read-only render operation. If different tiers may intentionally use different
paths, compare each tier independently.

**Required proof**

Add behavioral tests for an alert-free plist, an alert-enabled plist, a real
template mismatch, and different per-tier path configurations. Only the real
template mismatch should fail status.

## 6. Replace the stale and impossible draft-PR condition

**Where**

- `tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md:27`

**What happens**

Gate 1 requires PR #22 to remain draft while its pass condition is a merge
commit. GitHub does not allow a draft PR to merge. PR #22 was already marked
ready when this review ran, so the state claim was stale as well as impossible
as a merge condition.

**Recommendation**

Require the PR to remain open, clean, mergeable, and locally verified. Keep the
literal merge-approval phrase as the separate human gate. If draft state still
matters earlier in the workflow, name the transition to ready explicitly.

**Required proof**

The runbook should match the current PR state and describe one executable path
from review-ready to approved merge without contradictory conditions.

## 7. Verify the deployed SHA explicitly

**Where**

- `tasks/2026-07-10-cre-consolidation-review/2026-07-11-firecrawl-operator-runbook.md:29`
- `scripts/firecrawl-ops/cre_collector/cre_status.sh`

**What happens**

Gate 3 says `cre_status.sh` reports the expected checkout. The script does not
inspect Git state or compare a commit SHA. A stale or wrong checkout can satisfy
the listed pass evidence if its runtime checks otherwise look normal.

**Recommendation**

Record the expected merged SHA in the deployment handoff and compare it with
`git rev-parse HEAD` in the operator procedure. If this is added to
`cre_status.sh`, report branch, HEAD, dirty state, and expected SHA without
mutating the checkout.

**Required proof**

Test one matching SHA, one wrong SHA, and one dirty checkout. Only the exact,
clean checkout should pass the deployment gate.

## 8. Replace superseded scheduler language with one approved execution path

**Where**

- `tasks/2026-07-10-cre-consolidation-review/2026-07-10-safety-forward-queue.md:45-47`
- `tasks/2026-07-10-cre-consolidation-review/OPTIMAL_EXECUTION_PLAN_2026-07-11.md:250-252`

**What happens**

The historical optimal plan explicitly chose aa-hub and excluded GitHub
Actions. The forward queue says the manual GitHub workflow and the disabled
aa-hub lane both exist, then says to decide ownership before enabling either.
That leaves GitHub Actions scheduling open and also conflicts with the current
company policy: aa-hub is historical source and runbooks, not an execution
control plane.

**Recommendation**

State that the GitHub workflow remains manual-only and that neither a GitHub
workflow nor aa-hub authorizes unattended execution. Before any scheduler load,
record Cayman's approval of a named, current-policy-compatible execution
surface, the responsible owner, credential boundary, rendered configuration,
observation window, and rollback.

**Required proof**

The repo docs and Linear issue should identify one approved scheduler owner and
one activation path. No scheduled GitHub Actions workflow should be present,
and no aa-hub manifest should be treated as authorization to run the producer.

## Recommended repair order

1. Close the manual OM scalar and duplicate-identity path.
2. Restore the migration `015` approval boundary.
3. Correct scheduler authorization and merge/deployment evidence in the
   runbook.
4. Repair launchd alert delivery and plist drift verification.
5. Reconcile scheduler language with the current execution-control policy.
6. Run the focused Python, TypeScript, shell, and PostgreSQL contract suites.
7. Repeat adversarial review before requesting merge approval.
