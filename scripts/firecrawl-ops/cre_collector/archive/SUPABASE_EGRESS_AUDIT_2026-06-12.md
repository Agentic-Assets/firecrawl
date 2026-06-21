# Supabase Egress Audit For CRE Listing Work - 2026-06-12

Scope: read-only audit of whether the CRE listing collector, database views,
display read paths, or helper functions plausibly explain the current Supabase
egress spike. No schema changes were made. No secrets were printed.

## Commands Run

Repo and documentation orientation:

```bash
sed -n '1,240p' /Users/caymanseagraves/.agents/skills/firecrawl-ops/SKILL.md
sed -n '241,520p' /Users/caymanseagraves/.agents/skills/firecrawl-ops/SKILL.md
sed -n '1,240p' START_HERE.md  # root check; file absent, nested file read below
sed -n '1,260p' CLAUDE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/START_HERE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/CLAUDE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/SUPABASE_SECURITY_NOTE_2026-06-12.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/SUPABASE_RECENT_UPLOAD_QA_2026-06-12.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/CONTRACT_SYNC_2026-06-12.md
sed -n '1,260p' docs/firecrawl-ops/references/cre-listing-system-design.md
git status --short --branch
```

SQL, collector, and display-read inspection:

```bash
sed -n '1,320p' scripts/firecrawl-ops/sql/002_cre_listings.sql
sed -n '1,360p' scripts/firecrawl-ops/sql/005_cre_views.sql
sed -n '1,280p' scripts/firecrawl-ops/sql/004_cre_indexes.sql
sed -n '1,260p' scripts/firecrawl-ops/sql/003_cre_scrape_tracking.sql
sed -n '1,220p' scripts/firecrawl-ops/sql/006_cre_contact_urls.sql
sed -n '1,140p' scripts/firecrawl-ops/sql/CLAUDE.md
sed -n '360,820p' scripts/firecrawl-ops/cre_collector/cre_ingest.py
rg -n "v_cre_listings_full|search_cre_listings|raw_data|markdown|cre_listing_images|cre_listing_documents|storage|download|from\\(|select\\(|\\.select|rpc\\(|limit|range|pageSize|page-size" scripts/firecrawl-ops/cre_collector scripts/firecrawl-ops/sql docs/firecrawl-ops/references/cre-listing-system-design.md
rg -n "Supabase|storage|download|raw_data|documents|images|markdown|file_size|psql|COPY|copy|INSERT|SELECT|select|RETURNING|print\\(|echo" scripts/firecrawl-ops/cre_collector/cre_ingest.py scripts/firecrawl-ops/cre_collector/collect.ts scripts/firecrawl-ops/cre_collector/cre_daily_update.sh
```

Adjacent display-app docs and read path:

```bash
find /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/docs -maxdepth 4 -type f \( -name '*supabase*' -o -name '*cre*' -o -name '*listing*' -o -name '*hybrid*' \) 2>/dev/null | sort | head -120
sed -n '1,280p' /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/docs/supabase-egress-incident-2026-06-12.md
sed -n '1,280p' /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/docs/cre-live-listing-current-state-2026-06-12.md
sed -n '1,220p' /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/docs/supabase-cre-view-security-2026-06-12.sql
sed -n '1,220p' /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/docs/supabase-cre-function-security-2026-06-12.sql
rg -n "v_cre_listings_full|search_cre_listings|cre_listings|cre_listing_images|cre_listing_documents|raw_data|markdown|select\\(|rpc\\(|from\\(" /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data --glob '!node_modules/**' --glob '!out/**' --glob '!*.log'
sed -n '1,260p' /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/lib/db/credeals.ts
sed -n '1,120p' /Users/caymanseagraves/Documents/GitHub/agentic-assets/dynamically-display-cre-listing-data/tests/credeals-query-payload.test.ts
```

Live read-only Supabase measurement was run through the existing
`cre_ingest.py::load_db_url(None)` and `cre_ingest.py::find_psql()` helper
path. The helper printed only the env-file path and `psql` path. SQL used
`BEGIN READ ONLY`, `SET LOCAL statement_timeout`, aggregate payload-size
queries, function privilege checks, RLS checks, view option checks, capped
`search_cre_listings` samples, and a filtered `pg_stat_statements` read. The
database URL was not printed.

## Key Evidence

### 1. Collector uploads are not a direct egress driver

The collector ingestor writes to Postgres with `psql` and an inline `COPY` into
temporary tables, then runs `INSERT ... ON CONFLICT DO UPDATE`, child-row
deletes/inserts, and one grouped summary `SELECT` after commit. The only
client-bound data from a live ingest is command tags and the final per-broker
count table. The large listing payload moves into Supabase, not out of it.

`cre_ingest.py` maps:

- `raw_data` to the original listing JSON payload.
- `documents` to URL rows only.
- `images` to URL rows only.
- `contacts` to text and URL metadata only.

The collector does not upload PDF, image, VCard, or brochure binaries into
Supabase Storage. The docs and code both say document and image collection is
URL-only. That means collector uploads can increase database size and can make
later reads heavier, but the upload itself does not plausibly create a 227 GB
Supabase outbound egress spike.

### 2. `v_cre_listings_full` is the heavy read surface

The view definition selects `l.*`, then adds brokerage fields and JSON arrays
for contacts, documents, and images. Because `l.*` includes `raw_data` and
`markdown`, a `select *` or broad view read is the main CRE egress risk.

Live aggregate measurement on 2026-06-12:

| Metric | Result |
|---|---:|
| Active rows in `v_cre_listings_full` | 39,408 |
| Total full-view payload estimate | 187 MB |
| Average full-view row | 4,964 bytes |
| Median full-view row | 3,819 bytes |
| P95 full-view row | 10,427 bytes |
| P99 full-view row | 25,485 bytes |
| Max full-view row | 282,640 bytes |
| `raw_data` total | 51 MB |
| `markdown` total | 0 bytes |
| images JSON total | 38 MB |
| contacts JSON total | 15 MB |
| documents JSON total | 5,807 kB |
| image URL refs | 136,979 |
| document URL refs | 21,528 |
| contact refs | 57,874 |

Payload by source shows CBRE dominates the full-view footprint:

| Source | Rows | Full payload | Raw data | Images JSON | Image refs | Max row |
|---|---:|---:|---:|---:|---:|---:|
| CBRE | 20,885 | 124 MB | 32 MB | 30 MB | 114,503 | 282,640 bytes |
| SVN | 5,309 | 17 MB | 5,024 kB | 2,135 kB | 5,257 | 4,838 bytes |
| Newmark | 5,086 | 15 MB | 6,089 kB | 2,398 kB | 5,008 | 6,681 bytes |
| Avison Young | 2,200 | 12 MB | 3,546 kB | 496 kB | 2,186 | 12,010 bytes |
| Colliers | 1,172 | 11 MB | 3,529 kB | 3,221 kB | 9,908 | 131,248 bytes |
| JLL | 4,593 | 7,942 kB | 1,676 kB | 50 kB | 50 | 2,430 bytes |

A single full export of the active view is around 187 MB. Reaching 227 GB from
that path alone would require roughly 1,200 full-view downloads, or an
equivalent loop that repeatedly selects rich rows. A few board or detail reads
cannot explain the spike by themselves.

### 3. Current board path is capped and no longer returns `raw_data`

The adjacent display app now ranks candidate IDs from base tables, then hydrates
only selected IDs from `v_cre_listings_full`. Its board column list explicitly
uses:

```sql
null::jsonb as raw_data
```

and caps images to the first six per row. The regression test
`tests/credeals-query-payload.test.ts` asserts that the board query does not
select `v.raw_data`.

Live payload estimate for the current default board slice:

| Board read shape | Rows | Estimated payload |
|---|---:|---:|
| Same rows if returned as full view rows | 30 | 129 kB |
| Current board projection | 30 | 48 kB |
| Average current board row | 1,626 bytes |
| Max current board row | 4,525 bytes |

That is not a plausible 227 GB source unless a separate crawler or browser loop
is issuing millions of board reads. The app also has a 5 second query timeout
and falls back to static seeds on error.

### 4. Detail and search paths are bounded

Current detail reads call:

```sql
select <detail columns including raw_data>
from credeals.v_cre_listings_full
where id = $1 and deleted_at is null
limit 1
```

That is intentionally richer than board rows, but it is one listing at a time.
The largest measured full-view row is about 283 KB.

`credeals.search_cre_listings(...)` returns only scalar listing fields plus
rank, source URL, and scrape timestamp. It does not return `raw_data`,
`markdown`, contacts, documents, or images. The function is `STABLE`, not
`SECURITY DEFINER`, has `search_path=""`, and has `LIMIT 200`.

Live search payload samples:

| Function call | Rows | Estimated payload |
|---|---:|---:|
| `search_cre_listings(NULL, NULL, NULL, NULL, NULL)` | 200 | 119 kB |
| `search_cre_listings('industrial', NULL, 'TX', NULL, 'sale')` | 115 | 72 kB |

Function privileges were verified live:

| Function | `anon` | `authenticated` | `service_role` |
|---|---:|---:|---:|
| `search_cre_listings` | no execute | no execute | execute |
| `update_cre_listing_timestamp` | no execute | no execute | execute |

### 5. RLS and view posture are hardened

Live checks showed RLS enabled on:

- `cre_brokerages`
- `cre_listings`
- `cre_listing_contacts`
- `cre_listing_documents`
- `cre_listing_images`
- `cre_scrape_jobs`
- `cre_scrape_log`

The four views have `reloptions = {security_invoker=true}`:

- `v_cre_listings_full`
- `v_cre_active_for_sale`
- `v_cre_active_for_lease`
- `v_cre_market_summary`

This matches the security note and adjacent display-app hardening docs. Public
browser roles should not be able to directly read this collector-owned surface.

### 6. Recent query stats point to prior rich reads, but not enough evidence for 227 GB

`pg_stat_statements` was available for a recent window. Matching CRE statements
included old or diagnostic shapes that selected richer rows, including queries
with `raw_data`, `images`, and `contacts`. Examples:

- A ranked read shape with `raw_data` appeared 28 calls, 789 rows, and high
  internal block touch.
- A similar older ranked shape appeared 7 calls, 168 rows.
- Current `candidate_ids` board shapes appeared with small row counts.
- Exact detail reads from `v_cre_listings_full where id = $1` appeared 4 calls.

The block-touch numbers are database buffer activity, not client egress. The
observed CRE row counts in this stats window are not enough by themselves to
explain 227 GB of outbound traffic. They do support keeping manual diagnostics
and old board code away from `select *` or broad rich-view reads.

## Assessment

The CRE collector is unlikely to be the direct egress cause. It writes inbound
database data through `psql`, stores URLs rather than binaries, and returns only
small ingest summaries to the local client.

The CRE read surface has one real sharp edge: `v_cre_listings_full` is rich
because it includes `l.*`, `raw_data`, and aggregated URL/contact arrays. A
manual export, broad PostgREST request, old board query, or repeated diagnostic
against that view can create meaningful egress. The full active view is about
187 MB today. That is costly enough to guard, but still too small to explain
227 GB without heavy repetition.

The current display board path is already mitigated. It hydrates selected IDs,
omits `raw_data`, caps images, and returns about 48 KB for the default slice.
`search_cre_listings` is also bounded and scalar-only. Single-detail reads keep
`raw_data`, but are one-row capped.

The broader egress incident note remains consistent with this audit: the first
places to investigate are Supabase dashboard service breakdown, Auth/PostgREST
loops, high-frequency Edge Function or cron traffic, storage downloads, log
drains, and any manual rich-view exports.

## Recommendations

1. Keep the current board projection contract. Do not reintroduce `v.raw_data`,
   full contacts, full documents, or uncapped images into board reads.
2. Treat `v_cre_listings_full` as a detail/evidence view, not a list view. Avoid
   `select *`, PostgREST table browsing, CSV export, or broad diagnostics
   against it while egress is constrained.
3. If the board becomes a public or high-traffic surface, add a dedicated slim
   server-side card view or RPC that never exposes `raw_data`, `markdown`, full
   contacts, full documents, or more than a small image slice.
4. Keep detail reads one-ID capped and server-only. Add caching at the app layer
   before increasing public traffic.
5. Keep collector uploads URL-only. Do not move PDFs or images into Supabase
   Storage for bulk ingestion.
6. For next incident triage, use Supabase dashboard egress by service first.
   Then inspect Auth/PostgREST loops, Edge Function and cron frequency, Storage
   object download counts, Log Drains, and top `pg_stat_statements` callers.
7. For manual QA, use aggregate size/count queries like this audit did, not
   payload-returning queries from the rich view.

## Follow-Up Watch Items

- Active `v_cre_listings_full` rows increased from the earlier 34,218 note to
  39,408 in this read-only audit. That is expected after additional additive
  source ingests, but it should be tracked because full-view export cost scales
  with row count and child URL arrays.
- CBRE is the main payload contributor because of row count and image URL
  arrays. This does not imply CBRE ingestion is bad, only that broad reads
  should stay slim.
- The database still prints the existing collation-version warning. It did not
  block this audit, but it remains a maintenance item outside this egress check.
