# Supabase Recent Upload QA - 2026-06-12

Timestamp: 2026-06-12 06:05:36 CDT (-0500), SQL runs at 2026-06-12T11:03:53Z through 2026-06-12T11:05:24Z.

Scope: read-only QA for the already-ingested recent uploads: CBRE Deal Flow, Newmark, Avison Young, and Colliers SalesTracker. Collector code was not modified. The database URL was passed only through the existing `cre_ingest.py` helper path and was not printed.

## Commands

Read first:

```bash
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/START_HERE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/VALIDATION_2026-06-12.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/SUPABASE_SECURITY_NOTE_2026-06-12.md
sed -n '640,815p' scripts/firecrawl-ops/cre_collector/cre_ingest.py
sed -n '1,260p' scripts/firecrawl-ops/sql/002_cre_listings.sql
sed -n '1,260p' scripts/firecrawl-ops/sql/005_cre_views.sql
sed -n '1,240p' scripts/firecrawl-ops/sql/003_cre_scrape_tracking.sql
git status --short
```

Read-only SQL was run through a Python one-liner that imported:

- `cre_ingest.py::load_db_url(None)`
- `cre_ingest.py::find_psql()`

The command shape was:

```bash
python3 - <<'PY'
# import cre_ingest.py helpers, print env-file path and psql path only,
# then run psql with SQL on stdin:
#   BEGIN READ ONLY;
#   counts, latest-batch quality, duplicates, children, orphans,
#   recent scrape jobs, and credeals.search_cre_listings samples
#   COMMIT;
PY
```

The helper selected:

- credentials env file: `/Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/.env.local`
- psql: `/opt/homebrew/opt/libpq/bin/psql`
- DB URL: redacted

The first SQL bundle produced the main quality counts, then stopped at a stale `cre_scrape_jobs` column name. I reran the job/search tail with the actual columns from `003_cre_scrape_tracking.sql`. No write statements were run.

## Counts

| Source | Active rows | Latest `scraped_at` | Latest-batch rows | Active sale | Active lease | Active sale_or_lease |
|---|---:|---|---:|---:|---:|---:|
| Avison Young | 2,200 | 2026-06-12 09:33:46.286+00 | 2,200 | 636 | 1,431 | 133 |
| CBRE Deal Flow | 1,857 | 2026-06-12 09:23:38.998+00 | 1,836 | 1,830 | 27 | 0 |
| Colliers SalesTracker | 1,172 | 2026-06-12 10:05:58.574+00 | 1,172 | 1,172 | 0 | 0 |
| Newmark | 5,086 | 2026-06-12 07:54:19.703+00 | 4,371 | 1,379 | 3,707 | 0 |

CBRE Deal Flow and Newmark still include older active additive rows. The latest-batch rows match the recently uploaded artifacts.

## Latest-Batch Quality

| Source | Rows checked | Missing URL | Missing title | Missing or empty raw_data | Missing state | Invalid state format | Missing coords | Impossible lat/lng | Bad cap rate | Price/PSF/lease-rate flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Avison Young | 2,200 | 0 | 0 | 0 | 0 | 0 | 0 | 0 / 0 | 0 | 2 sale-price flags, 1 PSF flag |
| CBRE Deal Flow | 1,836 | 0 | 0 | 0 | 112 | 0 | 406 | 0 / 0 | 0 | 0 |
| Colliers SalesTracker | 1,172 | 0 | 0 | 0 | 29 | 0 | 0 | 0 / 0 | 0 | 1 PSF flag |
| Newmark | 4,371 | 0 | 0 | 0 | 3 | 0 | 0 | 0 / 0 | 0 | 0 |

Price flags used conservative QA heuristics, not database constraints: `sale_price_usd <= 0 OR > 20,000,000,000`, `sale_price_per_sf <= 0 OR > 100,000`, and lease rates outside a high sanity ceiling. `cre_ingest.py` currently allows sale prices up to `1e11`.

## Duplicates

No active duplicate `(brokerage_id, external_id)` groups were found for any of the four source slices.

| Source | Active duplicate source_url groups | Active rows in those groups | Latest duplicate source_url groups | Latest rows in those groups |
|---|---:|---:|---:|---:|
| Avison Young | 4 | 8 | 4 | 8 |
| CBRE Deal Flow | 21 | 42 | 0 | 0 |
| Colliers SalesTracker | 0 | 0 | 0 | 0 |
| Newmark | 0 | 0 | 0 | 0 |

Latest-batch Avison Young duplicate URL samples:

- `146-harder-road`: external IDs `64526`, `64570`
- `7102-n-sam-houston-parkway-w`: external IDs `55673`, `55936`
- `850-sherman-avenue`: external IDs `55724`, `55735`
- `chamblee-international-logistics-center`: external IDs `51062`, `51063`

CBRE Deal Flow duplicate URLs are only in active rows, not the latest batch. The samples pair older `dealflow:url:*` additive rows with current numeric project IDs.

## Child Rows And Orphans

| Source | Latest listings | Contacts | Documents | Images | Document missing URL | Image missing URL |
|---|---:|---:|---:|---:|---:|---:|
| Avison Young | 2,200 | 4,125 | 0 | 2,186 | 0 | 0 |
| CBRE Deal Flow | 1,836 | 5,597 | 416 | 40,176 | 0 | 0 |
| Colliers SalesTracker | 1,172 | 2,733 | 0 | 9,908 | 0 | 0 |
| Newmark | 4,371 | 0 | 0 | 4,303 | 0 | 0 |

Global child orphan checks:

- contacts: 0
- documents: 0
- images: 0

## Recent Job Rows

Latest relevant scrape jobs were all `completed` with `errors_count = 0`:

| Slug | Discovered | Scraped | Saved | Started | Completed |
|---|---:|---:|---:|---|---|
| colliers | 1,300 | 1,172 | 1,172 | 2026-06-12 10:02:42.339+00 | 2026-06-12 10:08:49.496356+00 |
| avison-young | 2,333 | 2,200 | 2,200 | 2026-06-12 09:33:43.047+00 | 2026-06-12 09:34:08.300629+00 |
| newmark | 4,371 | 4,371 | 4,371 | 2026-06-12 07:53:38.821+00 | 2026-06-12 09:31:11.165707+00 |
| cbre | 1,836 | 1,836 | 1,836 | 2026-06-12 09:17:41.504+00 | 2026-06-12 09:24:15.531107+00 |

## `search_cre_listings` Samples

Each sample selected a latest-batch listing, then called `credeals.search_cre_listings(query, NULL, state, property_type, transaction)` and verified the same listing ID appeared in the function output.

| Source | Status | Query | Result |
|---|---|---|---|
| Avison Young | MATCH | `+59 acre development opportunity in Clyde, TX` | Avison Young, Clyde TX, land sale |
| CBRE Deal Flow | MATCH | `±0.5 AC in Houston | US 290 & Dacoma Rd` | CBRE, Houston TX, land sale |
| Colliers SalesTracker | MATCH | `+/- 62 Acre Industrial Development Site` | Colliers, Conyers GA, industrial sale |
| Newmark | MATCH | `, Elk Grove, CA for lease` | Newmark, Elk Grove CA, industrial lease |

## Risks

- Avison Young has four latest-batch same-URL duplicate pairs with distinct external IDs. This is not an external-ID dedupe failure, but it can display duplicate-looking properties until source-specific URL dedupe or merge policy is chosen.
- Three price/PSF outliers deserve source-level review: Avison Young `185 Oval Drive` at `98,000,000,000`, Avison Young `Lagos` at `39,204,000,000`, and Colliers `Triton Cay Orlando` at `109,816.51` PSF because the stored size is `872`.
- Missing states are not invalid state codes, but they affect filtering. CBRE Deal Flow has 112 latest-batch rows without state and 406 without coordinates, many appearing international, brochure-only, or title-only. Colliers has 29 missing-state latest rows. Newmark has 3.
- PostgreSQL printed the existing collation-version warning on each connection. It did not block read-only QA, but remains a database maintenance item outside this collector upload check.
