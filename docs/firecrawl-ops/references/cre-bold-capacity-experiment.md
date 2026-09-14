# CRE bold capacity experiment

> **Status:** prepared and reviewed, not activated. No candidate resource
> transition or live 128-detail benchmark has been run under this workflow.
> The commands below describe guarded operator steps; they are not evidence of
> runtime activation, provider acceptance, throughput gain, or adoptability.

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

The candidate is adoptable only if the three matched replicates show at least a
15 percent throughput gain and all identity, normalized-field fidelity,
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

```bash
python3 cre_capacity_benchmark.py \
  --compare-baseline /restricted/matched-baseline-result.json \
  --compare-candidate ../../../tasks/tmp/cre-capacity-benchmark-001/result.json
```

It reports median qualified rows per minute, percentage gain, completeness,
normalized/native/fresh fidelity, and p50/p95/p99 latency. Under a future
supported baseline-admission path, at least 15 percent would yield `adoptable`
and a measured smaller gain would yield nonfatal `do_not_adopt`; missing or
unmatched evidence yields `no_adoption_decision`. At this revision the
comparison is hard-disabled from returning an adoption decision because no
supported baseline producer exists. Do not substitute the prior 32-detail
probe or any unmatched historical result.

The first candidate run does not require a baseline run. Adoption does. The
required baseline is the exact same JLL 128-record manifest and three-replicate
evidence contract at browser 2 CPU, global pages 4, JLL detail width 4, browser
PID 384, and API 1 CPU. At this revision, this workflow does not expose an
admitted command that generates that baseline artifact. Until a supported,
exactly matched baseline exists, the candidate result can establish safety and
fidelity evidence but the comparison must return `no_adoption_decision`.

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
