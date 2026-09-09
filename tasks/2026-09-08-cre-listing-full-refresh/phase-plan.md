# CRE listing full refresh plan

## Objective

Refresh every supported commercial real estate listing source through the
strict checkpoint series, ingest the accepted generations into Supabase, and
produce current freshness and integrity proof.

## Safety boundaries

- Use `scripts/firecrawl-ops/cre_collector/` only.
- Keep ingestion additive. Do not activate listing status or mark missing rows.
- Do not run OM extraction, change the five-column OM-facts identity, apply
  schema DDL, or mutate EQUIRE market-data objects.
- Do not install or load scheduler jobs during the supervised recovery.
- Stop on global resource, database, or checkout-integrity gates. Preserve
  source-local failures for diagnosis and retry.

## Proof path

1. Record checkout, runtime, database integrity, and per-source freshness.
2. Repair confirmed run-blocking defects and pass focused plus full tests.
3. Push the exact clean branch SHA required by the strict runner.
4. Run the 51-source checkpoint series serially under the conservative CPU
   profile.
5. Diagnose and repair source failures, then rerun the affected proof path.
6. Revalidate Supabase counts, freshness, queue health, and child integrity;
   restore the local runtime profile and record remaining external gates.

