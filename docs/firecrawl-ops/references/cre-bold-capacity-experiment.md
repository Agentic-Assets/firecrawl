# CRE bold capacity experiment

> **Status:** implemented and exercised once on 2026-09-14. The candidate was
> admitted, one 128-detail replicate ran, strict quality failed, the remaining
> replicates were stopped, and the original baseline was restored. The result
> does not support adoption. The commands below remain guarded operator steps,
> not standing authorization for another run.

`bold-jll-128` is a named, no-write experiment profile. It is not the default
production profile and normal collector startup never selects it. Its single
tracked source of truth is
`scripts/firecrawl-ops/cre_collector/cre_capacity_experiment_profiles.json`.
The current checkpoint series remains serial. The later two-provider split is a
planned global 10-page allocation of 6 plus 4, not 10 pages per provider and not
implemented source parallelism.

The proposed candidate is browser 6 CPU, global 10 pages, JLL detail width 10,
browser 16 GiB with no additional swap, browser PID 768, and API 2 CPU with
8 GiB and no additional swap. OrbStack remains configured at exactly 32 GiB.
Docker's reported usable memory may differ slightly, but must be at least 95
percent of that configured capacity. The host guard is 90 percent CPU sustained
for 30 seconds, sampled every 2 seconds.

## Resolve and inspect

The planner is read-only unless `--write-plan` is supplied. With no profile
argument it resolves `production-current`, preserving the production default.

```bash
cd scripts/firecrawl-ops/cre_collector
python3 cre_capacity_experiment.py
python3 cre_capacity_experiment.py --profile bold-jll-128
```

The runtime controller never reads the repository `.env`. Its preflight
captures the active container IDs, image and environment fingerprints, ports,
network, mounts, security settings, cgroup limits and current memory use,
OrbStack capacity, local endpoints, queue settlement, and exact Git HEAD. The
transition fingerprint binds that HEAD plus hashes of every execution-relevant
tracked input. Unrelated worktree dirt is recorded diagnostically but does not
invalidate this resource-only preflight. This is intentionally narrower than
benchmark admission: launching the benchmark separately requires the entire
source worktree to be clean at the admitted HEAD.

Preflight writes a private, machine-generated transition receipt and retains a
failed receipt for diagnosis. The receipt expires after 10 minutes for apply.

```bash
python3 cre_capacity_runtime.py preflight \
  --profile bold-jll-128 \
  --out ../../../tasks/tmp/cre-capacity-transition-001/receipt.json
```

The receipt directory is mode `0700` and the receipt is mode `0600`. Do not
edit it. Apply recalculates its integrity hash, baseline fingerprint, every
admission check, the selected profile hash, and the unchanged live snapshot.

## Review the transition without changing runtime

Apply and rollback are dry-run by default. The apply dry-run resolves the
private Compose configuration, image, environment, resources, and topology,
then prints the bounded transition plan without recreating or updating a
container.

```bash
python3 cre_capacity_runtime.py apply \
  --receipt ../../../tasks/tmp/cre-capacity-transition-001/receipt.json
```

After explicit technical review, an independent coordinating reviewer supplies
a private, one-use mode `0600` attestation with kind
`cre_capacity_review_approval`, `approved_by` set to `coordinating-review`, and
the exact profile, config hash, source SHA, transition receipt hash, timestamp,
600-second expiry, `approved: true`, and a fresh 64-hex-character `nonce`. The
reviewer, not this controller and not an operator helper command, must create
it. The file must have exactly one hard link, be owned by the operating account,
and live in an operating-account-owned mode `0700` directory. The controller
atomically renames, reads, and destroys it in an isolated same-user helper
before issuing any resource command. While holding the canonical lock, the
controller then creates a durable `O_EXCL` consumption marker keyed by the
approval nonce hash in the canonical private output tree. The marker binds the
approval hash, profile, config, transition receipt, source SHA, original review
timestamp, and expiry. It survives rollback, so neither the original file nor a
copy can authorize another attempt.

Successful approval consumption also creates a second private, mode `0600`,
one-use benchmark grant in the same private directory. Its filename is bound to
the approval nonce hash, and its payload is bound to the profile, profile-config
hash, transition receipt, source SHA, original review timestamp, and 600-second
expiry. The runtime admission records that grant path, nonce hash, and
review-bound timestamp, never the nonce. If the candidate transition or
admission write fails, the controller destroys the unused grant before
completing automatic compensation. A benchmark cannot start from the ordinary
admission JSON alone: while holding the canonical lock, it must atomically
consume and destroy this review grant in a same-user helper, enforce the
review-bound expiry again immediately before first worker launch, and record a
private, non-authoritative local consumption receipt. Editing the ordinary
admission timestamp cannot extend this authority.

A practical safe path is for the coordinating reviewer to create a dedicated
directory under `tasks/tmp/cre-capacity-approvals-<attempt>`, set it to mode
`0700`, enter the reviewed bindings and timestamp into a new mode `0600` JSON
file, and transfer its exact path to the execution owner. Generate the nonce
separately with a cryptographically secure tool and paste it into the reviewed
document; do not derive it from the receipt. The attestation contains bindings,
not secrets. Never paste container environments, credentials, raw receipt
snapshots, or provider data into it. Do not generate the approval from the
receipt with a script, shell substitution, `jq`, or the controller, and do not
reuse or edit it after an execution attempt.

This filesystem boundary prevents accidental disclosure, loose permissions,
unsafe file types, and protocol-level replay. It does not make the approval
cryptographically independent from another process running as the same
operating account, which could delete or alter its own files. Independence is
provided by the separate coordinating review record and exact content bindings,
not by a claim of Unix privilege separation. No `sudo` or Unix-root ownership
is needed for these Docker resource controls.

The candidate transition is:

```bash
python3 cre_capacity_runtime.py apply \
  --receipt ../../../tasks/tmp/cre-capacity-transition-001/receipt.json \
  --approval ../../../tasks/tmp/cre-capacity-approvals-001/review-approval.json \
  --execute \
  --admission-out ../../../tasks/tmp/cre-capacity-transition-001/admission.json
```

The controller uses `docker update` for the API, preserving its current
container ID, image, environment, port, network, and mounts. It recreates only
the browser through an ephemeral private Compose overlay assembled from the
running browser environment. Before recreation it rejects image, environment,
port, network, volume, tmpfs, security, resource, or topology drift. The
overlay is deleted after the command and cleanup failure is fatal. Candidate
verification failure triggers a bounded automatic baseline restore unless
unrelated environment drift makes restoration unsafe.

Every executing transition must hold the canonical CRE `SharedLock` for its
entire verify, mutate, post-verify, and automatic-rollback boundary. The
benchmark adapter must independently reacquire that same canonical lock before
its live preflight and hold it until all owned worker activity is terminal and
its result is durably written. No alternate lock directory is allowed. The
short gap between a completed candidate transition and benchmark launch is an
idle admitted runtime state, not permission for another collector or repair
job to run.

The rollback command remains available after the apply receipt expires. It is
idempotent from the exact baseline, exact candidate, or either one-component
mixed state created by an interrupted transition. It refuses unrelated
resource, environment, topology, source-input, host, endpoint, or settlement
drift. Dry-run reports the observed component states and only the commands an
execute would still need:

```bash
python3 cre_capacity_runtime.py rollback \
  --receipt ../../../tasks/tmp/cre-capacity-transition-001/receipt.json \
  --execute
```

Do not use `set_cre_resource_profile.sh apply`, recreate the API from the
current `.env`, or change model/OCR routing for this experiment. The active API
environment fingerprint differs from the root `.env`; recreating it could
adopt an unrelated, unreviewed environment change.

## Prepare and run the bounded benchmark

The benchmark adapter accepts only source `jll`, exactly 128 predeclared detail
records, exactly three matched replicates, the local Firecrawl endpoint, a
fully clean source worktree at the admitted HEAD, and a fresh one-use admission
record from the controller plus its bound private benchmark grant. Both are
consumed when launch begins, including when the worker is interrupted or a
provider cooldown is detected; a new artifact directory does not make the old
approval reusable. Its default mode only validates and prints a plan and does
not consume either record. Preparing a representative manifest reads an
existing JLL detail cache and makes no network or database call:

```bash
python3 cre_capacity_benchmark.py \
  --artifact-root ../../../tasks/tmp/cre-capacity-benchmark-001 \
  --prepare-sample /restricted/existing-jll-detail-cache

python3 cre_capacity_benchmark.py \
  --artifact-root ../../../tasks/tmp/cre-capacity-benchmark-001 \
  --sample ../../../tasks/tmp/cre-capacity-benchmark-001/jll-128-sample.json \
  --admission ../../../tasks/tmp/cre-capacity-transition-001/admission.json
```

Only after candidate admission and review, add `--run` to the second command.
The adapter has a global 10-request semaphore and JLL width 10, preserves
restricted request, raw, native, normalized, and telemetry artifacts, and has
no collector, ingest, or database writer import. The CPU guard trips only after
90 percent is sustained for 30 seconds at 2-second samples. The benchmark sets
both the collector helper and the pinned Firecrawl SDK itself to one attempt,
so an SDK-internal gateway retry cannot silently exceed the 128-request bound.
Any provider 429 or challenge signal stops the run and records cooldown
required. No later run may start on that admission. Wait through the recorded
cooldown, confirm local and remote settlement, and obtain a fresh independently
approved admission before another live attempt.

An interrupt or guard trip initiates bounded worker termination and settlement.
The adapter must wait only for its owned work and explicitly record whether
local requests became terminal and whether any remote request remains unknown.
Unknown, timed-out, or unavailable settlement is inconclusive and blocks the
next replicate and every adoption decision. It is never safe to infer
settlement from an exited worker alone. If final idle settlement cannot be
proved after any worker may have launched, the adapter writes quarantine
evidence inside the canonical lock and removes that lock's PID and lease. A
durable active marker is armed before the first worker can start and remains
through all three replicates and final settlement; every canonical `SharedLock`
acquirer and releaser treats either marker as non-reclaimable. The shell tier
dispatcher applies the same checks before stale-owner reclamation and release.
This includes an uncatchable parent-process death, where the active marker
remains even if the detached worker or remote jobs outlive it. The interlock
blocks rollback, another collector, and another benchmark until an operator
proves all loopback API, browser, RabbitMQ, NuQ, active-crawl, and queue counters
are idle, reviews the recorded result, and removes that exact canonical lock
directory. Do not remove it merely because the local worker process exited.

The candidate is adoptable only if three counterbalanced matched pairs (the
fixed `baseline,candidate,candidate,baseline,baseline,candidate` AB/BA/AB
sequence) show at least a 15 percent throughput gain and all identity, normalized-field fidelity,
document/media/asset fidelity, freshness, executable provenance, host and
container telemetry, OOM, PID, queue settlement, cooldown, and no-write gates
pass. Summary booleans alone are insufficient: every replicate must retain the
required raw host samples, before/after cgroup counters, request/performance
telemetry, settlement snapshots, and per-record normalized fidelity evidence.
A safe negative result is nonfatal. Missing required evidence makes the trial
inconclusive, never a production pipeline success or a collector failure. The
historical global duplicate-URL regression still blocks any production write
canary, and this experiment never changes canonical rows, caches, status, OM
objects, defaults, or schedules.

The comparison itself is pure and offline. It permits only the intended
resource and concurrency differences. Both inputs must be JLL, 128 details,
three replicates, and writes forbidden; they must share the exact sample
inventory and manifest content, freshness policy, and executable/parser
dependency fingerprints. Each replicate must be terminal and measured, retain
the raw telemetry listed above, and prove all 128 expected identities plus
freshness and normalized field, asset, document, and media parity. A source Git
SHA remains provenance, but an unrelated documentation-only commit is not a
substitute for executable dependency matching.

The ordinary two-result command remains diagnostic-only and can never return
`adoptable`. It reopens and rehashes the sample, admission, consumption marker,
worker output, performance receipt, and each raw cache receipt before reporting
its measured result. Create the immutable paired plan from a fresh prevalidated
JLL sample, then execute only its next arm with a fresh one-use admission. The
candidate arm requires its existing runtime receipt and is rolled back to the
baseline in `finally` before the arm is recorded. Do not batch admissions or
hold an approval across the allowed pair gap.

Worker status fields are not comparison authority. The comparator derives each
row's URL, hashes, HTTP status, challenge signal, and JLL tombstone semantics
again from its rehashed private raw receipt. A confirmed tombstone requires a
JLL HTTP 404, valid `__NEXT_DATA__`, explicit `notFound`, an error status 404,
and no property object. A worker claim that disagrees with its receipt is a
fidelity failure. Production comparison also rehashes the current clean
checkout's implementation manifest and every generated `worker.mts`; arm
results must be below the paired plan root. A `sealed_offline_fixture` plan is
valid only for test evidence and always reports `fixture_only_not_adoptable`.

When JLL reports a withheld or unknown price control, raw-data retention keeps
only the control and redacted pricing provenance. It removes known price schema
paths, legacy `financials.amount`, and monetary disclosures in stored markdown,
description, highlights, and summary text; it preserves unrelated provenance
such as `currentTenants`. A merged sale/lease row carries a non-sensitive
`jllPriceWithheld` marker so the SQL upsert clears a previously visible sale or
lease price without inferring state from nested provider JSON. Foreign-currency
lease amounts remain public normalized provenance only and never enter the
currency-free lease columns.

```bash
python3 cre_capacity_benchmark.py --artifact-root /restricted/pair-001 \
  --sample /restricted/jll-128-sample.json --create-counterbalanced-pair

# Repeat only for the plan's next arm, using its fresh baseline or candidate admission.
python3 cre_capacity_benchmark.py --pair-plan /restricted/pair-001/counterbalanced-pair-plan.json \
  --admission /restricted/fresh-admission.json --run-counterbalanced-step \
  --candidate-rollback-receipt /restricted/candidate-runtime-receipt.json

python3 cre_capacity_benchmark.py \
  --compare-counterbalanced-pair /restricted/pair-001/counterbalanced-pair-plan.json
```

The final comparator admits only six disk-bound arms with the exact fixed
order, pair ID, immutable sample/config hashes, matching per-pair attrition
identity manifests, and timestamps within the centrally configured gap. It
reports median qualified rows per minute, percentage gain, completeness,
normalized/native/fresh fidelity, and p50/p95/p99 latency. Confirmed JLL 404
attrition remains in its immutable slot, continues later replicas, and is
excluded only from that row's current throughput. Asymmetric attrition reduces
matching confidence and blocks adoption; it never silently substitutes cohort
members or infers a database inactive state. Any transport, challenge, 429,
unknown-status, malformed-content, parser, or fidelity failure remains
fail-closed.

The separate [multisource-v1 cohort contract](cre-multisource-capacity-v1.md)
is prevalidation-only until every listed provider has a reviewed execution
adapter and explicit current not-found classifier. It must not be substituted
for this controlled JLL paired lane.

## 2026-09-14 execution record

The reviewed candidate at Git commit
`489959adc6041935be31e4ce15ac2a13d8f9937e` passed all 38 live admission
checks. Replicate 1 issued exactly 128 predeclared JLL detail requests at a
maximum locally awaited concurrency of 10. The worker returned 128 unique
identity rows in 38.031 seconds of source time, a gross diagnostic rate of
201.941 rows per minute. This is not qualified throughput because 25 rows were
degraded detail-error rows.

The guard did not trigger: 19 two-second samples peaked at 52.31 percent host
CPU. There was no 429, challenge, provider cooldown, retry, OOM, PID-limit, or
queue-settlement failure. Both replicate and final settlement were idle in one
poll. The result is `completed=false`, `stop_reason=replicate_failed`, and
`comparison_state=failed`; the fail-closed runner correctly did not start
replicates 2 or 3.

Strict fidelity results were:

- 103 of 128 rows had fresh detail provenance.
- 102 retained every historically present native asset channel.
- 89 retained every historically supported normalized field.
- 0 received the run-level `qualified_fresh_unique_rows` measure because that
  measure is intentionally all-or-nothing across the exact 128-row replicate.

Offline artifact diagnosis found that all 25 detail errors were current target
HTTP 404 pages with valid `__NEXT_DATA__` but no property, while all 103
successful rows were HTTP 200. The historical source bodies for those failed
records dated from June 12 through June 19, so listing turnover is supported by
the evidence; permanent retirement is not proven. One current HTTP-200 listing
removed brokers and another removed a brochure. Twelve HTTP-200 listings
exposed structured `{amount,currency,unit}` price objects that the detail
normalizer previously treated as strings. The normalizer now accepts both
structured values and legacy strings, but that correction is locally tested
only and does not retroactively change this failed result.

The private result artifact is
`tasks/tmp/cre-capacity-benchmark-489959ad-0700/result.json` in the isolated
runtime worktree, with SHA-256
`8c14fcf1944c728e79a54eaae7e509add7cfc09a855759e7992d76ffdf6fe1b0`.
Raw provider bodies remain private and uncommitted. Independent post-run
readback verified the baseline API at 1 CPU and 8 GiB, the browser at 2 CPU,
16 GiB, four page slots, and PID 384, with no additional swap. API, RabbitMQ,
NuQ, active-crawl, and browser-page counters were idle, and the canonical lock
was absent.

Do not relax the fidelity gate or reinterpret gross rows as successful
enrichment. A later matched experiment needs a newly validated current
128-record cohort, an explicit diagnostic for current target-404 attrition,
and the same contemporaneous cohort under both baseline and candidate settings.
It requires a new preflight, approval, admission, grant, and artifact root.

## Stop, rollback, and resume

On a candidate verification failure, the controller attempts its bounded
automatic baseline restore while it still owns the canonical lock. On a
benchmark interrupt, guard trip, provider cooldown, telemetry loss, or unknown
settlement, do not resume the partial result and do not launch another
replicate. Preserve the private artifacts and verify bounded settlement. If the
lock is quarantined, complete the quarantine recovery evidence and remove only
that exact lock directory before attempting rollback. Then use the original
transition receipt to restore the exact baseline. Rollback remains available
after receipt expiry, but it must reacquire the canonical lock and classify each
component as the exact baseline or admitted candidate before mutation; any
third state is refused.

Any later retry is a new reviewed attempt: start from verified baseline, run a
fresh preflight at the intended clean HEAD, obtain a new independent one-use
review approval and admission, and use a new private benchmark artifact root. A
partial or interrupted three-replicate result is diagnostic evidence only and
cannot be spliced into a matched comparison.
