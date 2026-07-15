# `credeals` Ownership Contract

Status: proposed contract, verified against the listing collector migration
tree and a read-only production check on 2026-07-10. Cross-repository adoption
requires an explicit acknowledgement from GetCREdata before either repository
changes a shared object.

## Rules

- The listing collector `sql/` directory is the migration home for objects it
  creates. No other repository may alter those objects through an ad hoc REST
  migration path.
- A consumer may read an object but may not rename, drop, or change its
  semantics without the owning repository's reviewed migration.
- `cre_listing_om_facts` has one schema owner, the listing collector. The
  current verified external writer is GetCREdata `documents/`; this contract
  makes no claim that the cross-repository approval has already been recorded.
  The collector's deprecated `om_parse.py --apply` fallback remains prohibited
  until a future signed change explicitly enables it.
- The canonical OM-facts conflict key is `(listing_id, fact_group, fact_key,
  source_doc_url, parser_version) NULLS NOT DISTINCT`.

## Shared-object matrix

| Object | Schema owner and migration home | Approved writers | Known readers | Change rule |
| --- | --- | --- | --- | --- |
| `cre_listings` and listing child tables | Listing collector, `scripts/firecrawl-ops/sql/001-014` | Listing collector | GetCREdata documents pipeline, EQUIRE | Additive, reviewed collector migration only. |
| `cre_listing_om_facts` | Listing collector, `013` and `015` | GetCREdata `documents/` is the current observed writer; collector parser is disabled | Listing collector, EQUIRE | Preserve the five-column key. Formal cross-repository approval remains required. |
| `cre_zip_cbsa_crosswalk` | Listing collector, `014` | Listing collector refresh process | GetCREdata CMBS pipeline, EQUIRE | Treat the database table as the canonical shared crosswalk pending refresh-policy approval. |
| `cre_market_index` | GetCREdata, `sql/cre_cmbs_schema.sql` | GetCREdata reviewed migration/export path | EQUIRE and market tooling | Listing collector is read-only. Do not alter without consumer compatibility review. |
| `cbsa_market_data` and market-derived tables | GetCREdata, its reviewed migration/export path | GetCREdata | EQUIRE and future listing-market view | Listing collector must not alter or overwrite. |
| `cmbs_properties`, `cmbs_loans`, `cre_cap_rate_survey`, `reit_operating_facts`, `reit_transaction_caps`, `reit_filings_processed`, `cbre_filings_processed` | GetCREdata, its reviewed migration/export path | GetCREdata | EQUIRE and market tooling | Listing collector is read-only. |

## Production truth check

The 2026-07-10 read-only check found 398,040 OM-facts rows and the canonical
five-column production index. The checked-in `013` migration previously
declared the old four-column form, so `015` is staged as a guarded alignment
migration for legacy deployments. It is not applied by this contract.

## Required follow-through

1. Mirror this document in GetCREdata and obtain its owner acknowledgement.
2. Replace any stale "OM facts empty" claim with a generated read-only status
   artifact, not hand-maintained prose.
3. Decide the GetCREdata scheduler and the owning crosswalk refresh cadence.
4. Apply `015` only under an approved maintenance plan. Production already has
   the desired index, so it currently needs no DDL.
5. Make GetCREdata's `cre_market_index` read one canonical parser release,
   rather than aggregating across parser versions with `max()`.
