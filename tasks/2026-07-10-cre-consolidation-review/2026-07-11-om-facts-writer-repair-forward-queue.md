# Forward queue after OM-facts writer repair (2026-07-11)

Candidate work surfaced during the repair. This is a menu, not a delivery
commitment. Re-verify each item before acting.

## Hardening

- **Gate cross-repository OM writers against the same manifest** (confidence:
  verified gap, high priority).
  The collector and GetCREdata should both assert their conflict targets against
  the object-ownership contract before an OM writer changes. This prevents a
  source migration, SQL generator, or external writer from drifting alone.

## Operations

- **Run the approved five-row additive enrich canary after deployment**
  (confidence: blocked on review, merge, live-checkout update, and scheduler
  approval).
  Capture queue and validation baselines first, then require five completed
  claims, no released claims, no conflict error, no status/soft-delete changes,
  and a fresh green marker before restoring normal cadence.

- **Pause the repeating enrich loop until the deployed writer is proven**
  (confidence: verified operational risk).
  The known mismatch released 200 claims after successful enrichment. A scheduler
  owner should decide whether to unload enrich and weekly while the branch waits
  for review, while leaving monitor active.

## Documentation

- **Generate the OM-facts contract status from source and read-only database
  metadata** (confidence: verified documentation-drift class).
  The old key remained in two collector documents after the source migration had
  moved to five columns. A generated report can compare the migration, emitted
  SQL, external writer configuration, and live index definition before a
  scheduled ingest resumes.

## Process

- **Replace the remote runtime discovery gap with a pre-canary checklist**
  (confidence: verified operational gap, high priority).
  The Mac mini has a different checkout, stopped Docker runtime, TCC-risk path,
  and undiscoverable database environment. Turn those four observations into a
  single read-only preflight that must pass before a deployment or canary is
  proposed.
