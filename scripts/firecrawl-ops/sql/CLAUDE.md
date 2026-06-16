# sql/ Module

## Most Critical Rule

**Idempotent `credeals` DDL only on Supabase `fhqycqubkkrdgzswccwd`.** Apply via `000_run_all.sql` in dependency order: `001`→`002`→`003`→`004`→`007`→`008`→`009`→`010`→`011`→`012`→`013`→`014`→`006`→`005`. **`001_cre_brokerages.sql` seed slugs must match `../cre_collector/cre_ingest.py` `SOURCE_TO_BROKERAGE`.** Never commit or print `DATABASE_URL`. Advisor triage: `advisor-reports/` (plan `2026-06-13-cre-execution-readiness.md`; live apply log `2026-06-13-cre-remediation-apply-log.md`; best-practices review + fresh-DB smoke test `2026-06-13-cre-best-practices-review.md`). Migration `009_cre_history_retention.sql` (2026-06-15): price-history table, child-archive tables, `prior_*` columns on `cre_source_index`, and the `trg_cre_listings_block_history_delete` retention trigger. APPLIED to prod 2026-06-15 (existence guards kept pre-apply ingestor runs safe). Migration `010_cre_enrichment_ops.sql` (2026-06-15): the additive enrichment-queue health views `v_cre_enrichment_queue_pending` and `v_cre_enrichment_dead` over the existing 007 `cre_enrichment_queue` (no table change; `CREATE OR REPLACE VIEW`, idempotent). WRITTEN and WIRED into `000_run_all.sql` after `009`; apply is GATED to the enrichment cutover runbook (`../cre_collector/ENRICHMENT_WORKER_DESIGN_2026-06-15.md` Section 9) and NOT yet applied to prod. Migration `011_cre_listing_media.sql` (2026-06-15): NEW `cre_listing_media` + `cre_listing_links` child tables (+ `*_archive` mirrors), widens `cre_listing_documents.doc_type` to add `financials`/`rent_roll`, and `005` exposes both via `v_cre_listings_full` LATERALs. Idempotent + additive; ingest INSERTs are `to_regclass`-guarded so pre-apply daily/enrich runs are no-ops for media/links. WRITTEN + WIRED into `000_run_all.sql` after `010`; NOT yet applied to prod. See `../cre_collector/HANDOFF_MEDIA_CAPTURE_2026-06-15.md`. Migrations `012`/`013`/`014` (Phase-2 data-lift, 2026-06-15; spec `../cre_collector/PHASE2_DATA_LIFT_CONTRACT_2026-06-15.md`): `012_cre_listing_institutional_cols.sql` adds additive scalar columns on `cre_listings` (`building_class`, `property_subtype`, `apn`, `tenant_name`, `guarantor`, `lease_years_remaining`, `price_per_unit`, `grm`, `price_per_acre`, `num_rooms`, `revpar`, `clear_height_ft`, `dock_doors`, `drive_in_doors`, `power_service`, `rail_served`, `cbsa_code`, `cbsa_name`, `geo_source`, `extra_facts` jsonb) with guarded range/enum CHECKs plus `license` on `cre_listing_contacts`; `013_cre_listing_om_facts.sql` adds the NEW `cre_listing_om_facts` table (+ `*_archive` mirror) for OM/PDF-parsed scalar/unit_mix/rent_roll facts with parse provenance (RLS on, FK `ON DELETE CASCADE`, `NULLS NOT DISTINCT` unique key for idempotent re-parse); `014_cre_geo_crosswalk.sql` adds the NEW `cre_zip_cbsa_crosswalk` reference table (ZIP->county+CBSA, US-gov public domain; `\copy` load gated, commented out so a schema-only apply never fails on a missing CSV). `005` exposes `cre_listing_om_facts` via a `v_cre_listings_full` LATERAL and the new institutional/geo columns via `v_cre_active_for_sale`/`v_cre_active_for_lease`. Idempotent + additive; ingest INSERTs into `cre_listing_om_facts`/archive are `to_regclass`-guarded; status badges route to the existing OPT-IN activation gate, never auto-activate. WRITTEN + WIRED into `000_run_all.sql` after `011` (order `012`→`013`→`014`→`006`→`005`); NOT yet applied to prod.

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
| `../cre_collector/cre_enrich.py`, `cre_status.sh` | 010: `v_cre_enrichment_queue_pending`, `v_cre_enrichment_dead` (read-only health views over the 007 `cre_enrichment_queue`); `attempts >= 5` dead-letter threshold matches `cre_enrich.py` claim SQL |
| EQUIRE (`CRE_EQUIRE`) | Do not rename/drop `v_cre_listings_full`, `v_cre_active_for_sale`, `v_cre_active_for_lease`, `v_cre_market_summary`, `search_cre_listings()` without coordinating |
| Access | `cre_*` / `v_cre_*` service-role only; display views `security_invoker=true`; read `../cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` before grant changes |

## References

- `../../../docs/firecrawl-ops/references/cre-equire-consumer-api.md`
- `../../../docs/firecrawl-ops/references/cre-intelligence-system-design.md`
- `../cre_collector/CLAUDE.md` (ingest, monitor, daily ops)
