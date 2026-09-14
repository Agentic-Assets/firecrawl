# CRE bold capacity experiment

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
OrbStack capacity, local endpoints, queue settlement, and exact clean Git SHA.
It writes a private, machine-generated transition receipt and retains a failed
receipt for diagnosis. The receipt expires after 10 minutes for apply.

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

After explicit technical review, the reviewer supplies a private mode `0600`
attestation with kind `cre_capacity_root_approval`, `approved_by` set to
`root-review`, and the exact profile, config hash, source SHA, transition receipt
hash, timestamp, 600-second expiry, and `approved: true`. The controller has no
command that self-issues this approval. The candidate transition is:

```bash
python3 cre_capacity_runtime.py apply \
  --receipt ../../../tasks/tmp/cre-capacity-transition-001/receipt.json \
  --approval /restricted/cre-capacity-transition/root-approval.json \
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

The rollback command remains available after the apply receipt expires, but
both its dry-run and execute modes require the exact admitted candidate state:

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
clean source SHA, and a fresh admission record from the controller. Its default
mode only validates and prints a plan. Preparing a representative manifest
reads an existing JLL detail cache and makes no network or database call:

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
90 percent is sustained for 30 seconds at 2-second samples. Any provider 429 or
challenge signal stops the run and records cooldown required.

The candidate is adoptable only if the three matched replicates show at least a
15 percent throughput gain and all identity, fidelity, freshness, provenance,
OOM, PID, queue, cooldown, and no-write gates pass. A safe negative result is
nonfatal. Missing required telemetry makes the trial inconclusive, never a
production pipeline success or a collector failure. The historical global
duplicate-URL regression still blocks any production write canary, and this
experiment never changes canonical rows, caches, status, OM objects, defaults,
or schedules.

The comparison itself is pure and offline. It permits the intended resource and
concurrency differences, but requires the same exact sample inventory and
manifest content, source/parser version, freshness policy, three complete
replicates, and every quality and telemetry gate:

```bash
python3 cre_capacity_benchmark.py \
  --compare-baseline /restricted/matched-baseline-result.json \
  --compare-candidate ../../../tasks/tmp/cre-capacity-benchmark-001/result.json
```

It reports median qualified rows per minute, percentage gain, completeness,
native/fresh fidelity, and p50/p95/p99 latency. At least 15 percent yields
`adoptable`; a measured smaller gain yields nonfatal `do_not_adopt`; missing or
unmatched evidence yields `no_adoption_decision`. Do not substitute the prior
32-detail probe or any unmatched historical result. A low-setting run is not a
prerequisite to the first bold candidate run, so the first result may remain
inconclusive for adoption until a genuinely matched baseline exists.
