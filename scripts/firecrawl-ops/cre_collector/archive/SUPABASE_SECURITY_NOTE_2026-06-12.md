# Supabase credeals Security Note - 2026-06-12

This note is for agents working in the CRE collector and ingestor after the
display app security follow-up in
`dynamically-display-cre-listing-data`.

## Current Posture

- Project: `fhqycqubkkrdgzswccwd`, schema: `credeals`.
- Collector-owned base tables have RLS enabled.
- No public row policies were added for `anon` or `authenticated`.
- The four display views now use `security_invoker=true`.
- `credeals.search_cre_listings(text,text,text,text,text)` and
  `credeals.update_cre_listing_timestamp()` are executable by `service_role`,
  not by `anon` or `authenticated`.

## Collector Impact

- `cre_ingest.py` should keep using the service-role Postgres connection.
- Do not grant public table, view, or function access to make scraper code
  easier to run.
- Public client reads remain a display-app or API-layer design decision, not a
  collector-side change.
- Supabase advisor notices about RLS without public policies are expected for
  this private collector-owned surface.

## Display-App Performance Context

The display app ranks active candidate IDs from base tables, then hydrates only
selected rows from `credeals.v_cre_listings_full`. On 2026-06-12, the real
board query explained at about 159 ms, and a filtered Texas sale query at about
23 ms. If collector schema changes rename listing, brokerage, or child-table
columns, update the display app query and mapper together.
