# CRE Listings Column Coverage Report

**Generated:** 2026-06-15  
**Source:** Supabase project `fhqycqubkkrdgzswccwd`, schema `credeals`, table `cre_listings`  
**Scope:** Active board rows only (`deleted_at IS NULL`), unless noted  
**Method:** Supabase MCP `execute_sql` (read-only aggregates)

## Artifact files

| File | Format | Contents |
|------|--------|----------|
| `reports/cre_listings_column_coverage_2026-06-15.csv` | CSV | Per-column population counts and percentages |
| `reports/cre_listings_brokerage_coverage_2026-06-15.csv` | CSV | Per-brokerage completeness and child-table coverage |
| `reports/cre_listings_transaction_coverage_2026-06-15.csv` | CSV | Price/size fill rates by `transaction_type` |
| `reports/cre_listings_coverage_summary_2026-06-15.json` | JSON | Full machine-readable bundle (all sections below) |

Open the CSV files in Excel, Numbers, or Google Sheets.

## Row inventory

| Metric | Count |
|--------|------:|
| Total rows (all statuses, incl. soft-deleted) | 92,699 |
| Active rows (`deleted_at IS NULL`) | 87,328 |
| Soft-deleted rows | 5,371 |
| Active with `status = 'active'` | 87,328 |
| Active with `status = 'inactive'` | 0 |

All active listings are on the board today (`status = 'active'`). Soft-deleted history lives in the 5,371 pruned rows.

## How population was counted

| Type | Counted as populated when |
|------|---------------------------|
| Text | `NOT NULL` and trimmed value is non-empty |
| Numeric / integer (size, price, rates) | `NOT NULL` and `> 0` |
| Arrays (`highlights`, `amenities`) | `NOT NULL` and `cardinality > 0` |
| `raw_data` (jsonb) | `NOT NULL` and not `{}` |
| Timestamps | `NOT NULL` |

Columns with database defaults (`country`, `scraped_at`, `status`) will look fully populated even when the collector did not explicitly set a broker value.

## Executive summary

**Strongest columns (near-universal on the active board):** identity and ingest plumbing (`external_id`, `source_url`, `raw_data`, `scraped_at`, `status`, `transaction_type`, `title`, `city`, `property_type`), plus geo for most rows (`lat`/`lng` at 98%).

**Weakest columns (0% populated):** eighteen columns have no mapped collector data yet, including `markdown`, `market`, `submarket`, `county`, `canonical_url`, `noi`, `occupancy_rate`, `amenities`, lease term fields, parking fields, and `last_seen_at` (reserved for future per-listing monitor state).

**Sparse but partially filled:** pricing and underwriting fields are the main product gap. Only **17.9%** of active rows have `sale_price_usd`, **10.3%** have `lease_rate_min`, and **2.5%** have `cap_rate`. That is expected: many brokers hide prices on list cards, lease inventory dominates (61% of rows), and several large sources do not map sale or lease rates into columns.

**Child tables:** contacts (97.5% of listings), images (96.7%), documents (62.7%). Rich media and broker contact coverage is much better than normalized price columns.

## Column fill rates (active rows)

Sorted by `% populated` descending.

### Tier A: 95%+ populated (core listing shell)

| Column | Type | Populated | Missing | % |
|--------|------|----------:|--------:|--:|
| country | text | 87,328 | 0 | 100.00 |
| external_id | text | 87,328 | 0 | 100.00 |
| raw_data | jsonb | 87,328 | 0 | 100.00 |
| scraped_at | timestamptz | 87,328 | 0 | 100.00 |
| source_url | text | 87,328 | 0 | 100.00 |
| status | text | 87,328 | 0 | 100.00 |
| transaction_type | text | 87,328 | 0 | 100.00 |
| title | text | 87,326 | 2 | 100.00 |
| city | text | 87,202 | 126 | 99.86 |
| property_type | text | 86,944 | 384 | 99.56 |
| lat | double precision | 85,632 | 1,696 | 98.06 |
| lng | double precision | 85,632 | 1,696 | 98.06 |
| state | text | 85,454 | 1,874 | 97.85 |
| address | text | 83,154 | 4,174 | 95.22 |
| zip | text | 82,553 | 4,775 | 94.53 |

### Tier B: 40-80% populated (useful but incomplete)

| Column | Type | Populated | Missing | % |
|--------|------|----------:|--------:|--:|
| canonical_key | text | 66,378 | 20,950 | 76.01 |
| size_sf | numeric | 60,290 | 27,038 | 69.04 |
| updated_date | timestamptz | 53,423 | 33,905 | 61.18 |
| description | text | 49,910 | 37,418 | 57.15 |
| source_lastmod | timestamptz | 41,632 | 45,696 | 47.67 |

### Tier C: under 20% populated (financial / detail fields)

| Column | Type | Populated | Missing | % |
|--------|------|----------:|--------:|--:|
| sale_price_usd | numeric | 15,584 | 71,744 | 17.85 |
| lot_size_sf | numeric | 12,530 | 74,798 | 14.35 |
| year_built | integer | 11,567 | 75,761 | 13.25 |
| sale_price_per_sf | numeric | 9,328 | 78,000 | 10.68 |
| lease_rate_min | numeric | 9,016 | 78,312 | 10.32 |
| listing_date | timestamptz | 5,876 | 81,452 | 6.73 |
| highlights | text[] | 2,899 | 84,429 | 3.32 |
| cap_rate | numeric | 2,163 | 85,165 | 2.48 |
| min_divisible_sf | numeric | 1,761 | 85,567 | 2.02 |
| lease_rate_max | numeric | 798 | 86,530 | 0.91 |

### Tier D: 0% populated (not mapped by current ingest)

`address2`, `amenities`, `available_sf`, `canonical_url`, `county`, `floors`, `gross_revenue`, `last_seen_at`, `lease_rate_type`, `markdown`, `market`, `max_divisible_sf`, `noi`, `occupancy_rate`, `parking_ratio`, `parking_spaces`, `submarket`, `term_max_months`, `term_min_months`, `units`, `zoning`

These are schema placeholders or reserved monitor fields, not evidence that upstream brokers lack the data (much of it may still exist inside `raw_data`).

## Transaction-type context (why prices look sparse)

| transaction_type | Listings | % sale_price | % lease_rate | % size_sf | % cap_rate |
|------------------|--------:|-------------:|-------------:|----------:|-----------:|
| lease | 53,504 | 0.01 | 15.12 | 74.38 | 0.00 |
| sale | 28,601 | 48.89 | 0.00 | 57.00 | 7.56 |
| sale_or_lease | 5,223 | 30.58 | 17.75 | 80.30 | 0.00 |

Lease-heavy inventory explains low global `sale_price_usd` fill. Even on sale rows, only about half carry a numeric ask price (POA / contact-for-pricing is common).

## Brokerage completeness (16-field core score)

Core score averages sixteen fields: `external_id`, `title`, `address`, `city`, `state`, `zip`, `property_type`, lat/lng pair, `size_sf`, `description`, `updated_date`, `sale_price_usd`, `lease_rate_min`, `cap_rate`, `year_built`, non-empty `raw_data`.

| Rank | Brokerage | Active listings | Avg core % | Address | Geo | Size | Description | Sale $ | Lease rate | Cap rate | Updated |
|-----:|-----------|---------------:|-----------:|--------:|----:|-----:|------------:|-------:|-----------:|---------:|--------:|
| 1 | Avison Young | 2,201 | **77.9** | 100.0 | 100.0 | 96.2 | 99.8 | 19.5 | 22.8 | 1.1 | 100.0 |
| 2 | Cushman & Wakefield | 11,318 | **76.4** | 99.9 | 99.9 | 92.3 | 53.2 | 10.0 | 0.0 | 0.0 | 95.6 |
| 3 | Marcus & Millichap | 3,124 | **74.0** | 96.8 | 100.0 | 0.0 | 100.0 | 87.8 | 0.0 | 68.5 | 0.0 |
| 4 | CBRE | 20,864 | **71.1** | 97.3 | 98.1 | 74.9 | 63.3 | 11.4 | 0.0 | 0.0 | 91.2 |
| 5 | Colliers | 17,001 | **70.4** | 81.8 | 99.3 | 27.7 | 96.6 | 24.2 | 20.7 | 0.0 | 93.1 |
| 6 | Newmark | 4,371 | **67.8** | 100.0 | 100.0 | 85.5 | 0.0 | 0.0 | 0.0 | 0.0 | 100.0 |
| 7 | SVN | 5,287 | **65.8** | 100.0 | 99.2 | 76.2 | 0.0 | 47.5 | 31.0 | 0.0 | 0.0 |
| 8 | JLL | 11,675 | **65.4** | 98.7 | 90.7 | 83.5 | 74.5 | 0.0 | 0.0 | 0.0 | 8.0 |
| 9 | Lee & Associates | 9,223 | **65.1** | 100.0 | 99.7 | 85.6 | 0.0 | 24.4 | 36.5 | 0.0 | 0.0 |
| 10 | Savills | 2 | **62.5** | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 11 | NAI Global | 241 | **61.5** | 0.0 | 100.0 | 83.8 | 100.0 | 0.0 | 0.0 | 0.0 | 100.0 |
| 12 | Transwestern | 2,021 | **61.3** | 100.0 | 100.0 | 88.4 | 0.0 | 1.6 | 0.0 | 0.0 | 0.0 |

**Fullest brokers (headline):** Avison Young and Cushman lead on the blended core score, with strong geo, size, and description coverage.

**Thinnest brokers (headline):** Transwestern and NAI Global score lowest on the core index. NAI has **0% address** in columns (geo-only feed). Several Buildout-heavy brokers (SVN, Lee, Newmark, Transwestern) have **0% description** because description is not mapped into the column (content may still be in `raw_data`).

**Notable outliers:**

- **Marcus & Millichap:** best sale price (87.8%) and cap rate (68.5%) coverage, but **0% size_sf** in columns.
- **Colliers:** strong description (96.6%) but weak **size_sf (27.7%)** and **address (81.8%)**, reflecting colliers-main vs SalesTracker merge patterns.
- **JLL:** **8% updated_date** despite 74% description; broker recency is under-mapped.
- **Newmark:** **100% updated_date** but **0% description** and **0% documents** child rows.

## Child table coverage (active listings)

### Portfolio-wide

| Child table | Listings with rows | % of active | Total child rows | Avg rows / listing w/ data |
|-------------|-------------------:|------------:|-----------------:|---------------------------:|
| contacts | 85,155 | 97.51 | 160,192 | 1.88 |
| images | 84,450 | 96.70 | 487,847 | 5.78 |
| documents | 54,746 | 62.69 | 70,391 | 1.29 |

### By brokerage (% listings with at least one child row)

| Brokerage | Contacts | Documents | Images |
|-----------|--------:|----------:|-------:|
| Lee & Associates | 100.00 | 83.28 | 98.25 |
| SVN | 100.00 | 73.75 | 99.02 |
| Savills | 100.00 | 100.00 | 100.00 |
| Colliers | 98.07 | 41.51 | 100.00 |
| JLL | 98.54 | 76.33 | 91.75 |
| CBRE | 99.82 | 81.13 | 99.70 |
| Avison Young | 97.82 | 84.55 | 99.59 |
| Marcus & Millichap | 97.34 | 0.00 | 100.00 |
| Cushman & Wakefield | 93.64 | 61.51 | 88.86 |
| Transwestern | 93.32 | 71.40 | 85.60 |
| Newmark | 90.62 | 0.00 | 98.44 |
| NAI Global | 0.00 | 0.41 | 95.85 |

## Recommended follow-ups

1. **Enrichment worker (Tier B):** prioritize lifting `sale_price_usd`, `lease_rate_*`, `cap_rate`, and `size_sf` from detail pages / `raw_data` where brokers hide list-card prices.
2. **Column mapping gaps:** `description` and `updated_date` are inconsistent across Buildout and JLL sources; fixing adapter mapping may be cheaper than full detail re-scrape.
3. **Schema vs product:** eighteen zero-percent columns are expected until ingest maps them or EQUIRE reads from `raw_data` / child tables instead.
4. **NAI contacts:** 0% contact child rows warrants a source-specific ingest check (241 active listings).

## Reproduce

```sql
-- Active row counts
SELECT count(*) FILTER (WHERE deleted_at IS NULL) AS active_rows
FROM credeals.cre_listings;
```

Re-run the full aggregate bundle via Supabase MCP `execute_sql` on project `fhqycqubkkrdgzswccwd`, or import the JSON summary and refresh dated artifacts under `reports/`.
