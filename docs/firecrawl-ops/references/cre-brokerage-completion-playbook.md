# CRE Brokerage Completion Playbook

Last updated: 2026-06-12.

Use this playbook when upgrading one brokerage website from shallow coverage to
complete public-feed coverage for EQUIRE. Work one brokerage at a time.

## Completion Standard

A brokerage is complete only when all of these are true:

1. Discovery covers the public source feed with real pagination, not just the first rendered batch.
2. Sale and lease modes are both understood, including sale-only or lease-empty platforms.
3. Each listing has a stable external id or a documented fallback id.
4. Each listing captures the best public structured fields available: title, transaction type, property type, address, city, state, ZIP, coordinates, price/rate text, size, status, description, brokers, and source URL.
5. Detail pages are scraped when the search feed does not expose enough data.
6. Document and image assets are captured as URLs only. Do not download or upload PDF/image binaries into Supabase storage.
7. Contacts include visible names, phones, emails when public, profile URLs, avatar URLs, and VCard or contact-card URLs when available.
8. A target listing is tested against manual expectations for documents, images, contacts, and key facts.
9. The collector passes typecheck, the ingestor dry-run stages expected rows, and docs record the remaining limits.

## Investigation Workflow

1. Start with the source folder:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

2. Inspect the public website with browser devtools or the Browser plugin:

- Search for XHR and fetch requests while loading the listing page.
- Change filters, pagination, map/list mode, and text search.
- Record GET endpoints first. Treat POST-only or consent-gated paths as blocked until a repeatable safe path is found.
- Save endpoint shape, required query params, page size, and total-count fields.

3. Test endpoints through local Firecrawl:

```bash
python3 scripts/firecrawl-ops/firecrawl_request.py scrape '<api-url>' \
  --formats rawHtml --out /tmp/source-api.json
```

Use local Firecrawl even when direct Python or curl gets blocked. Some sites
serve the local browser path but reject plain urllib.

4. Build the collector adapter:

- Prefer public JSON APIs over rendered cards.
- Use full pagination and cap by `--max-items`.
- Preserve source raw rows in `raw_data`.
- Add bounded concurrency and per-listing failure capture.
- If a detail scrape fails, keep the feed row and store `detailError` rather than failing the whole source.
- Canonicalize hostnames and relative URLs.

5. Detail-page enrichment:

- Scrape `rawHtml`, `markdown`, and `links`.
- Parse JSON-LD when present.
- Scan raw HTML for asset URLs. Do not rely only on Firecrawl's extracted links.
- Dedupe image variants and prefer high-width URLs when the CDN exposes width params.
- Exclude related-listing photos by using page-specific media IDs, document media IDs, or the first listing media group.

6. Targeted proof:

- Pick one known listing with manually visible documents, photos, contacts, and facts.
- Add a temporary or environment-gated query hook when the source supports text search.
- Save a small targeted artifact under `/tmp`.
- Confirm counts for document URLs, image URLs, contact URLs, and key facts.

7. Ingest proof:

```bash
python3 cre_ingest.py --in /tmp/source_target_probe.json \
  --dry-run --keep-artifacts /tmp/source_target_ingest_check
```

Check that staged child rows contain URLs only and no binary payload fields.

8. Full proof:

```bash
npx tsx collect.ts --source=<source-key> --transaction=both \
  --max-items=0 --page-cap=400 --concurrency=2 \
  --out=out/<source>_full_<timestamp>.json
python3 cre_ingest.py --in out/<source>_full_<timestamp>.json --dry-run
```

Use live ingest only when source errors are understood. Use `--mark-missing`
only after a clean full run with no source gaps.

## Documentation Required Per Brokerage

Each source should have:

- `scripts/firecrawl-ops/cre_scrapers/brokers/<broker>/README.md`
- Status in `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- Current session notes in `scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md`
- Any source lesson in `scripts/firecrawl-ops/cre_collector/LESSONS_2026-06-11.md`
- If the source changes counts or coverage, update `START_HERE.md` and the dated brokerage status report.

## Cushman Pattern To Reuse

Cushman & Wakefield was upgraded by:

- Finding the public API in browser network traffic.
- Verifying local Firecrawl could fetch the API even when direct urllib got `403`.
- Replacing rendered-card scraping with full API pagination.
- Scraping detail pages for facts, JSON-LD, contacts, PDFs, and images.
- Scanning raw HTML for PDF URLs because Firecrawl's extracted links omitted them.
- Saving document/image URLs only.
- Proving the known 1800 Central listing captured 2 PDFs, 15 photos, and the broker contact links.

This is the model process for the remaining partial brokerage sources.

## Sitemap-Discovery Pattern To Reuse (Cloudflare-protected sites)

Colliers main (`colliers-main`, 2026-06-13) was unblocked when neither a public
search GET nor a usable Coveo POST existed, by discovering listings through the
site's own XML sitemap. Reuse this when a brokerage hides search behind
Cloudflare/JS but still publishes a sitemap:

1. Fetch `robots.txt` through local Firecrawl (not direct GET, which Cloudflare
   403s). Read the declared `Sitemap:` URL and confirm `User-agent: *` allows
   crawling. Try the bare `/sitemap` path, not only `sitemap.xml`.
2. Fetch the sitemap through local Firecrawl. Walk the `<sitemapindex>` to the
   relevant child (e.g. `?type=properties`), then parse the `<urlset>` for
   detail `<loc>` URLs plus `<lastmod>` (refresh semantics). Generic helpers
   `extractSitemapLocs()` / `extractSitemapUrlEntries()` in `collect.ts` do this.
3. Render each detail URL through local Firecrawl with `proxy: stealth` and a
   `waitFor`. Parse `RealEstateListing` JSON-LD for the reliable fields
   (transaction in `name`, category, canonical URL, primary image) and the
   markdown/HTML for the rest (price, size, coordinates, photos, named PDF docs,
   broker contacts).
4. Handle Cloudflare rate-limiting: under sustained paging the site returns 429
   "Just a moment..." challenge shells. Detect them and retry with backoff; the
   stealth proxy rotates IPs per request, so a retry usually lands clean
   (`scrapeColliersMainDetailDoc`).
5. Tombstone non-listings so they are cached, not re-fetched, and never
   ingested: 404 / "Property Not Found" (`skip:"not_found"`) and real 200 pages
   on an alternate template with no JSON-LD (`skip:"no_structured_data"`).
6. Cache enriched rows in a durable JSONL file so a multi-hour run resumes
   across attempts; memoize across the sale and lease passes and partition by
   transaction client-side.
7. Fold into the parent brokerage with a prefixed external_id (`main:<id>`) in
   `SOURCE_TO_BROKERAGE`, and add a matching `external_id LIKE` branch to
   `cre_validate.py`'s source-key CASE so the report attributes folded rows.

Document/image URLs only; no POST replay, auth, or gated documents.
