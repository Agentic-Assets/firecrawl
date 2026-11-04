Parent: [AGENTIC-4177](https://linear.app/agenticassets/issue/AGENTIC-4177). Engineering: [AGENTIC-4179](https://linear.app/agenticassets/issue/AGENTIC-4179).

# Evidence-bound retirement without a source-index row

`cre_reconcile_listing_lifecycle.py` defaults to the existing indexed lifecycle
path. Missing index evidence is refused by default. The explicit
`--allow-index-absent-retirement` mode prepares only an exact active → inactive
listing transition backed by absence of its provider identity from a complete,
fresh, digest-bound full-source artifact. Shared URLs, names and addresses are
not identity or retirement evidence.

The evidence row must carry exact `listing_id`, `brokerage_id`, `external_id`,
`listing_status="active"`, `listing_deleted=false`, `listing_deleted_at=null`,
`listing_updated_at`, `listing_scraped_at` (explicitly null if absent), and
`index_exists=false`. Index state fields `index_soft_deleted`,
`observation_present` and `presence_generation` must be null or absent because
there is no index row. Updated/scraped timestamps must be timezone-aware and no
newer than the source observation. The ordinary `source` object still requires
`state="absent"`, `source_key`, `url`, `observed_at`, `evidence_path` and the exact
`evidence_sha256`; existing full-source scope, pass success and freshness checks
remain mandatory. Already inactive, sold or otherwise non-active listings are
refused. Event/history repair requests cannot be combined with this mode.

Prepare evidence with an approved target-bound read-only snapshot. Keep the
artifact, before-state and generated plan private (directory 0700, files 0600)
under ignored scratch outside the runtime. Do not invent index state. For an
operator-provided evidence file, a dry-run example from the collector directory is:

```bash
python3 -B cre_reconcile_listing_lifecycle.py \
  --evidence /private/scratch/evidence.json \
  --out-prefix /private/scratch/plan \
  --allow-index-absent-retirement
```

This is not approval. Apply retains the private operator/reference contract,
exact plan hash and confirmation, expected database-target fingerprint, and
fresh artifact revalidation gates. A concrete production retirement still
requires founder review. Never manufacture an operator attestation.

Each transaction takes the shared lifecycle advisory lock, then an
absence-protecting source-index table lock before locking the exact listing.
A newly inserted index row or changed listing identity/status/deleted/updated/
scraped state refuses the transaction. Lock and statement waits are bounded.
Only listing retirement and deterministic job/audit evidence are written; no
index row, queue, baseline, OM or child data is created or deleted. The audit is
`status_change`, field `status`, real `active` → `inactive`, with artifact, plan
and observation-time provenance. There is no fabricated presence generation
or disappeared/reappeared event. Exact replay requires the same retirement
state, matching deterministic audit evidence and continued index absence. The
listing mutation timestamp remains the database transaction time, including the
existing update trigger, and must match that audit event's detection time on replay.

Failure before commit rolls back the transaction; no postcommit destructive
inverse is supplied. Any later correction needs fresh evidence and approval.
The opt-in contract runner `tests/run_index_absent_postgres_contract.py` requires
an already-cached PostgreSQL 17 image ID and `--proof-dir`. It uses one isolated
network-none disposable container without published ports or persistent volumes,
real migration-016 event constraints and migration-005 timestamp trigger, rollback faults and two-connection insert
races. It never accepts an external database URL or pulls an image.
