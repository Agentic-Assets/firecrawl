# sql/ Module

## Most Critical Rule

**Idempotent `credeals` DDL only on Supabase `fhqycqubkkrdgzswccwd`.**
Apply via `000_run_all.sql` in dependency order:
`001`→`002`→`003`→`004`→`007`→`008`→`009`→`010`→`011`→`012`→`013`→`014`→`006`→`005`.
Migration `015` is excluded from the generic runner. It is a legacy-only
index rebuild and requires both schema-owner approval and the explicit psql
variable `CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1`.
**`001_cre_brokerages.sql` seed slugs must match
`../cre_collector/cre_ingest.py` `SOURCE_TO_BROKERAGE`.** Never commit or
print `DATABASE_URL`.

Advisor triage: `advisor-reports/` (plan
`2026-06-13-cre-execution-readiness.md`; live apply log
`2026-06-13-cre-remediation-apply-log.md`; best-practices review + fresh-DB
smoke test `2026-06-13-cre-best-practices-review.md`).

Migration status as of the live Supabase object check on 2026-06-16:

- `009_cre_history_retention.sql`: APPLIED. Price-history table, child-archive
  tables, `prior_*` columns on `cre_source_index`, and
  `trg_cre_listings_block_history_delete` are present.
- `010_cre_enrichment_ops.sql`: APPLIED. Additive enrichment-queue health views
  `v_cre_enrichment_queue_pending` and `v_cre_enrichment_dead` are present with
  `security_invoker=true`.
- `011_cre_listing_media.sql`: APPLIED. `cre_listing_media` and
  `cre_listing_links` are present, archive mirrors exist, and
  `cre_listing_documents.doc_type` accepts the widened values.
- `012`/`013`/`014` Phase-2 data-lift: APPLIED. Institutional scalar columns,
  `cre_listing_om_facts`, and `cre_zip_cbsa_crosswalk` are present; the crosswalk
  is loaded with 33,791 rows.
- `006`/`005` final refresh: APPLIED. Contact URL/license fields and refreshed
  views/search functions are present. Consumer deploy timing is still a product
  cutover concern, not a database-object gap.

## Folder-Specific Commands

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 000_run_all.sql

# Legacy OM-facts index alignment only after recorded approval.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -v CRE_APPROVE_OM_FACTS_KEY_ALIGNMENT=1 \
  -f 015_align_om_facts_conflict_key.sql
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
