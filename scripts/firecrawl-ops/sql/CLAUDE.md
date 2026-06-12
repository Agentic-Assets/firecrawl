# CLAUDE.md  -  sql/

SQL migrations for the EQUIRE CRE listing intelligence schema.
Target: Supabase project `fhqycqubkkrdgzswccwd` (supabase-agentic-assets-v2), `credeals` schema.

## File order

Run these in order. Each file is idempotent (`CREATE TABLE IF NOT EXISTS`, etc.).

| File | What it creates |
|------|----------------|
| `000_run_all.sql` | Master runner, dependency order `001`, `002`, `003`, `004`, `006`, then `005` |
| `001_cre_brokerages.sql` | `cre_brokerages` table + collector brokerage seed rows |
| `002_cre_listings.sql` | `cre_listings`, `cre_listing_contacts`, `cre_listing_documents`, `cre_listing_images` |
| `003_cre_scrape_tracking.sql` | `cre_scrape_jobs`, `cre_scrape_log` |
| `004_cre_indexes.sql` | Performance indexes (geo, FTS, jsonb GIN, price, cap_rate) |
| `006_cre_contact_urls.sql` | Contact profile/avatar/VCard URL columns and refreshed `v_cre_listings_full` contact JSON |
| `005_cre_views.sql` | `v_cre_listings_full`, `v_cre_active_for_sale`, `v_cre_active_for_lease`, `v_cre_market_summary`, `search_cre_listings()` function, `updated_at` trigger |

## Running migrations

```bash
# Option A: psql direct
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 000_run_all.sql

# Option B: Supabase MCP (apply_migration per file)
# Use ToolSearch to load mcp__claude_ai_Supabase__apply_migration
# project_id = "fhqycqubkkrdgzswccwd"

# Option C: Supabase dashboard SQL editor  -  paste each file in order
```

Set `DATABASE_URL` from `~/.pgpass`, a secrets vault, or another local secret
source before running `psql`. Never commit or print the connection string.

## Schema conventions

- All PKs are `uuid DEFAULT gen_random_uuid()`.
- All timestamps are `timestamptz DEFAULT now()`.
- Money fields are `numeric` (USD). No currency column unless non-USD.
- `cap_rate` and `occupancy_rate` are fractions in `[0,1]`  -  6.5% is stored as `0.065`.
  This matches the EQUIRE valuation layer.
- Soft delete via `deleted_at timestamptz` on `cre_listings`. Views exclude soft-deleted rows.
- `cre_` prefix is safe  -  only `cre_business_plan_runs` predated this schema.

## Key constraints

- `cre_listings(brokerage_id, external_id)`  -  unique where `external_id IS NOT NULL`.
  This is the dedup key for upserts. Listings without an external_id (scraped from
  pages with no parseable ID) can coexist but won't dedup.
- All child FKs (`cre_listing_contacts`, `cre_listing_documents`, `cre_listing_images`,
  `cre_scrape_log.listing_id`) are `ON DELETE CASCADE`.

## Collector alignment

The production bulk loader is `../cre_collector/cre_ingest.py`. Its
`SOURCE_TO_BROKERAGE` mapping must match the slug values inserted in
`001_cre_brokerages.sql`. Sub-sources fold into parent brokerages:
`cbre-dealflow` -> `cbre`, and `jll-investor` -> `jll`. New source keys
must be added to both the loader mapping and the seed file before dry-run or
live ingest.

The legacy Python scraper package in `../cre_scrapers/` still has its own
`config.py` and `ListingData` model. Keep those aligned when using that package,
but do not treat it as the daily production path.

The collector-owned `cre_*` tables and `v_cre_*` views are service-role only.
`anon` and `authenticated` do not have table or view `SELECT`. RLS is enabled
with no public row policies by design.

As of the 2026-06-12 display-app security follow-up, the four display views use
`security_invoker=true`. `credeals.search_cre_listings(text,text,text,text,text)`
and `credeals.update_cre_listing_timestamp()` should remain executable by
`service_role`, not by `public`, `anon`, or `authenticated`. Read
`../cre_collector/SUPABASE_SECURITY_NOTE_2026-06-12.md` before changing view or
function grants.

## Agent-facing objects (do not drop these)

EQUIRE agents read these  -  do not rename or drop without coordinating with the
EQUIRE codebase (`CRE_EQUIRE` repo):

- `v_cre_listings_full`
- `v_cre_active_for_sale`
- `v_cre_active_for_lease`
- `v_cre_market_summary`
- `search_cre_listings(query, p_city, p_state, p_type, p_transaction)`
