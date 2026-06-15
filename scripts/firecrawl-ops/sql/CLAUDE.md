# sql/ Module

## Most Critical Rule

**Idempotent `credeals` DDL only on Supabase `fhqycqubkkrdgzswccwd`.** Apply via `000_run_all.sql` in dependency order: `001`→`002`→`003`→`004`→`007`→`008`→`009`→`006`→`005`. **`001_cre_brokerages.sql` seed slugs must match `../cre_collector/cre_ingest.py` `SOURCE_TO_BROKERAGE`.** Never commit or print `DATABASE_URL`. Advisor triage: `advisor-reports/` (plan `2026-06-13-cre-execution-readiness.md`; live apply log `2026-06-13-cre-remediation-apply-log.md`; best-practices review + fresh-DB smoke test `2026-06-13-cre-best-practices-review.md`). Migration `009_cre_history_retention.sql` (2026-06-15): price-history table, child-archive tables, `prior_*` columns on `cre_source_index`, and the `trg_cre_listings_block_history_delete` retention trigger. NOT YET APPLIED to prod (gated; existence guards make pre-apply ingestor runs safe).

## Folder-Specific Commands

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 000_run_all.sql
```

Set `DATABASE_URL` from `~/.pgpass` or a local secrets source. Alternatives: Supabase SQL editor (paste in order) or MCP `apply_migration` per file (`project_id = fhqycqubkkrdgzswccwd`).

## Naming Patterns

- Files: `NNN_cre_<domain>.sql` (`000` master runner).
- Objects: `cre_*` tables/views in `credeals` schema.
- Dedup key: `(brokerage_id, external_id)` unique where `external_id IS NOT NULL`.
- `cap_rate` / `occupancy_rate` are fractions in `[0,1]` (6.5% → `0.065`).
- Soft delete: `cre_listings.deleted_at`; views exclude deleted rows.
- Date semantics: `listing_date` is source-proven first-listed/published only,
  `updated_date` is broker/source recency, `scraped_at` is collector snapshot
  time, `updated_at` is DB/index mutation time, and `created_at` / `deleted_at`
  are database lifecycle markers. `last_seen_at` is reserved nullable
  per-listing enumeration state; current observe-only monitor state lives in
  `cre_source_index` to avoid churning `updated_at`. UI copy should say "Source
  updated", "Snapshot collected", or "Latest indexed" according to the column
  provenance, never generic "updated".

## Module Boundaries

Owns DDL, brokerage seeds, indexes (`004`, `008` FK covers on 007), views, and 007 monitor tables. Does **not** own runtime ingest SQL (`cre_ingest.py`) or observe-only monitor writes (`cre_monitor.py`, `cre_gate.py`). Child FKs (`cre_listing_contacts`, `cre_listing_documents`, `cre_listing_images`, `cre_scrape_log`) are `ON DELETE CASCADE`.

## Integration Points

| Consumer | Contract |
|----------|----------|
| `../cre_collector/cre_ingest.py` | Seeds + listing columns; sub-sources fold in ingest (`dealflow:`, `investor:`, `main:`) |
| `../cre_collector/cre_monitor.py`, `cre_gate.py` | 007: `cre_listing_events`, `cre_source_index`, `cre_enrichment_queue`, `cre_source_baseline`; 009: `prior_sale_price`/`prior_lease_rate`/`prior_status` columns on `cre_source_index` (monitor reads AND writes these unconditionally, so the monitor tier REQUIRES 009 applied before it runs; the ingest/daily path is existence-guarded and stays safe pre-apply) |
| `../cre_collector/cre_ingest.py` (history writes) | 009: `cre_listing_price_history` (existence-guarded INSERT); `cre_listing_contacts_archive`, `cre_listing_documents_archive` (existence-guarded, mark-missing only) |
| EQUIRE (`CRE_EQUIRE`) | Do not rename/drop `v_cre_listings_full`, `v_cre_active_for_sale`, `v_cre_active_for_lease`, `v_cre_market_summary`, `search_cre_listings()` without coordinating |
| Access | `cre_*` / `v_cre_*` service-role only; display views `security_invoker=true`; read `../cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` before grant changes |

## References

- `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md`
- `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md`
- `../cre_collector/CLAUDE.md` (ingest, monitor, daily ops)
