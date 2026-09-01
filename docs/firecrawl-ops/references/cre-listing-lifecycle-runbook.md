# CRE Listing Lifecycle Contract Runbook

Migration `016_cre_listing_lifecycle.sql` and lifecycle reconciliation are
approval-gated database operations. Migration 016 is deliberately absent from
`000_run_all.sql`. Nothing in this runbook records approval or proves that the
migration has been applied to production.

## Migration 016 gate

Before applying, record a dedicated `AGENTIC-NNN` issue with the target,
operator (`cayman` or `stace`), reviewed migration SHA, maintenance window,
readback owner, and rollback decision. Confirm that no ingest, monitor, or
reconciliation process is writing the CRE tables. Resolve the database URL from
the approved secret source and verify the target fingerprint without printing
the URL.

Run only with `psql`; SQL editors and generic migration runners cannot satisfy
the fail-closed variable contract:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -v CRE_APPROVE_LISTING_LIFECYCLE=1 \
  -v CRE_LISTING_LIFECYCLE_OPERATOR='<cayman-or-stace>' \
  -v CRE_LISTING_LIFECYCLE_APPROVAL_REF='<AGENTIC-NNN>' \
  -v CRE_LISTING_LIFECYCLE_CONFIRM='APPLY 016_cre_listing_lifecycle' \
  -f scripts/firecrawl-ops/sql/016_cre_listing_lifecycle.sql
```

The migration is one transaction with bounded lock and statement timeouts. A
missing or malformed approval variable aborts before `BEGIN`.

## Required readback

Save the output with the issue. All queries are read-only.

```sql
SELECT column_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'credeals'
  AND table_name IN ('cre_source_index', 'cre_scrape_jobs',
                     'cre_listing_events', 'cre_listing_price_history')
  AND column_name IN ('observation_present', 'presence_generation',
                      'presence_changed_at', 'artifact_run_key',
                      'reconciliation_provenance', 'evidence_observed_at',
                      'evidence_time_semantics',
                      'reconciliation_evidence_sha256')
ORDER BY table_name, ordinal_position;

SELECT conname, pg_get_constraintdef(oid), convalidated
FROM pg_constraint
WHERE conrelid = 'credeals.cre_listing_events'::regclass
  AND conname = 'cre_listing_events_lifecycle_identity_required';

SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'credeals'
  AND indexname IN ('cre_listing_events_idem_uq',
                    'cre_listing_events_presence_transition_uidx',
                    'cre_scrape_jobs_artifact_run_key_uidx',
                    'cre_listing_price_history_reconciliation_job_uidx')
ORDER BY indexname;

SELECT confdeltype
FROM pg_constraint
WHERE conrelid = 'credeals.cre_listing_events'::regclass
  AND confrelid = 'credeals.cre_scrape_jobs'::regclass
  AND contype = 'f';
```

The lifecycle constraint must require `presence_generation` but not a non-null
`scrape_job_id`; the job foreign key readback must remain `n` (`ON DELETE SET
NULL`). The legacy idempotence index must exclude `disappeared` and
`reappeared` and cover only rows whose `scrape_job_id IS NOT NULL`. Rows leave
that index when job deletion nullifies the FK, so otherwise-identical events
from different deleted jobs remain valid audit history. The presence-transition
index owns the lifecycle generation key independently of job retention.

## Rollback posture

- If the migration transaction fails, PostgreSQL rolls it back. Do not retry
  until the lock, approval, or schema cause is understood.
- After commit, first roll application writers back to the preceding reviewed
  SHA while leaving the additive columns and provenance fields in place. This
  is the operation-specific safe rollback.
- Do not drop lifecycle columns or restore the old lifecycle-spanning unique
  index after lifecycle events may have been written. That would discard audit
  evidence or fail on legitimate later generations. Any schema compensation
  requires a separately reviewed forward migration and a conflict readback.
- Lifecycle reconciliation has no destructive inverse. Correct a bad approved
  action only with new source evidence, a new plan hash, and a new approval
  reference; never delete history or events.

## Evidence-backed reconciliation

The evidence bundle must point to the actual successful full collector artifact
using `source.evidence_path`. The planner hashes the artifact bytes, derives the
listing identity from the artifact, binds `observed_at` to
`runMeta.finishedAt`, and checks freshness against the host clock. Application
revalidates the same bytes, strict scope, and timestamp. The artifact itself
must prove the unlimited full `sale` and `lease` contract: exact source-pass
coverage, `supported=true`, no pass error or truncation, and collected counts
that reconcile to the artifact listings and `totalListings`. Caller-supplied
claims such as `source.complete` have no authority.

Dry-run first. After review, the operator creates a JSON approval contract
outside the repository, restricts it with `chmod 600`, and passes it with
`--approval-contract`. The file has exactly this shape and binds the operator
and issue to one immutable plan:

```json
{
  "schema_version": 1,
  "operation": "cre-listing-lifecycle-reconciliation",
  "operator": "cayman",
  "approval_ref": "AGENTIC-NNN",
  "plan_hash": "<plan-hash>"
}
```

Application also requires `--plan-hash`, the expected database-target SHA-256,
and this exact confirmation with the generated hash:

```text
APPLY cre-listing-lifecycle-reconciliation <plan-hash>
```

Reconciliation jobs stay `running` while batches execute and become
`completed` only in the final transaction after every batch succeeds. Preserve
the JSON/CSV plan and the final database readback with the approval issue.
