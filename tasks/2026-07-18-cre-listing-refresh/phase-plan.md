# CRE listing refresh plan

## Objective

Refresh the complete supported EQUIRE CRE listing inventory and verify that
collectors, change monitoring, and enrichment are current and safe.

## Scope and boundaries

- Run the supported `cre_collector` pipeline only. The legacy
  `cre_scrapers` package is reference/probe code, not a production writer.
- Preserve the five-column OM-facts identity. Firecrawl does not run OM
  extraction writes. GetCREdata remains the sole OM-extraction writer.
- Do not apply schema migration 015 or change EQUIRE market-data objects.
- Use additive ingestion unless the coverage gate independently proves that a
  source is safe for reconciliation. Never activate listing status as part of
  this refresh.

## Proof path

1. Record runtime, scheduler, checkout, credential-path, and database
   freshness baselines.
2. Repair only confirmed run-blocking defects, with focused tests.
3. Execute the supported all-source additive refresh, monitor, and eligible
   queue enrichment paths with durable artifacts.
4. Query production readbacks for per-source freshness, counts, queue state,
   and validation results; record any deferred source or system gate in Linear.
