# JLL Investor Center Sitemap Detail Expansion - 2026-06-12

Scope: source key `jll-investor` only.

This pass implements the defensible public GET-only expansion path identified
in `JLL_INVESTOR_PUBLIC_PATH_RECHECK_2026-06-12.md`: XML sitemap discovery plus
public detail-page `__NEXT_DATA__` parsing. It avoids the robots-disallowed
`property-search?...` query-string pagination path.

## Boundaries Honored

- Public URL-only data.
- No PDF or image binary downloads.
- No auth, gated deal-room, agreement, or unsafe external POST path.
- No live Supabase ingest.
- No commit or push.
- Main `jll` source behavior was not changed.

## Fresh Local Evidence

Local Firecrawl health:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: local Firecrawl was healthy at `http://localhost:3002`.

Bounded sitemap and detail probes:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  https://invest.jll.com/sitemap_index.xml \
  --formats rawHtml,markdown,links --wait-for 1000 --timeout 60000 \
  --out /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_sitemap_index.json \
  --save-fields /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_sitemap_index_fields \
  --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  https://invest.jll.com/us/sitemap-us.xml \
  --formats rawHtml,markdown,links --wait-for 1000 --timeout 60000 \
  --out /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_sitemap_us.json \
  --save-fields /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_sitemap_us_fields \
  --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  https://invest.jll.com/us/en/listings/living-multi-housing/ora-apartments \
  --formats rawHtml,markdown,links --wait-for 5000 --timeout 90000 \
  --out /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_detail_ora.json \
  --save-fields /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_detail_ora_fields \
  --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  https://invest.jll.com/us/en/listings/land/alcanena \
  --formats rawHtml,markdown,links --wait-for 5000 --timeout 90000 \
  --out /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_detail_alcanena.json \
  --save-fields /tmp/jll_investor_sitemap_detail_2026-06-12_codex/fc_detail_alcanena_fields \
  --quiet --print-paths
```

Observed:

- The sitemap index includes `https://invest.jll.com/us/sitemap-us.xml`.
- The fresh US sitemap had 1,940 raw sitemap entries and 1,855 unique
  `/us/en/listings/...` detail URLs after de-dupe in the collector run.
- ORA Apartments parsed from public detail `__NEXT_DATA__` with country
  `United States`, state `CA`, city `Oakland`, 6 image URLs, and 4 brokers.
- Alcanena parsed from public detail `__NEXT_DATA__` with country `Portugal`,
  proving the US locale sitemap is global and must be country-filtered from
  detail-page data.

## Implementation

Changed `scripts/firecrawl-ops/cre_collector/collect.ts` only in the
`jll-investor` section.

New behavior:

1. Fetch `https://invest.jll.com/sitemap_index.xml`.
2. Discover and fetch `https://invest.jll.com/us/sitemap-us.xml`.
3. Extract and de-dupe public `/us/en/listings/...` detail URLs.
4. Scan a bounded candidate window:
   - `JLL_INVESTOR_SITEMAP_SCAN_LIMIT` can cap detail candidates for probes.
   - Without that env cap, finite `--max-items` scans a wider bounded window to
     account for non-U.S. rows before slicing retained U.S. rows.
   - Unlimited runs scan all de-duped sitemap detail URLs.
5. Reuse the existing JLL Investor detail enrichment helper.
6. Retain rows only when detail-page country normalizes to `US`.
7. Continue storing public teaser document URLs, image URLs, and visible broker
   contact fields as URLs only. `documentsCA` stays inside raw JLL Investor
   detail metadata, matching prior behavior.

Detail scrape failures remain row-local inside the enrichment helper. For the
sitemap path, rows without a confirmed U.S. detail country are excluded from
the retained output so an unverified detail failure cannot be ingested as U.S.
inventory.

## Targeted Collector Probe

Command:

```bash
cd scripts/firecrawl-ops/cre_collector
env JLL_INVESTOR_SITEMAP_SCAN_LIMIT=8 \
  npx tsx collect.ts --source=jll-investor --transaction=sale \
  --max-items=4 --concurrency=2 \
  --out=/tmp/jll_investor_sitemap_detail_2026-06-12_codex/jll_investor_probe.json
```

Result:

- 1,855 unique sitemap detail URLs found.
- 8 detail URLs scanned.
- 3 U.S. rows retained.
- 5 non-U.S. rows skipped.
- 0 detail errors.
- 12 unique brokers.

Retained sample rows:

| Name | State | City | Docs | Photos | Contacts |
| --- | --- | --- | ---: | ---: | ---: |
| 12795 W Alameda Pkwy | CO | Lakewood | 1 | 8 | 3 |
| Undeveloped Portion of 69-699 Waikoloa Beach Dr | HI | Waikoloa Village | 1 | 5 | 2 |
| 15.27 Acres on Calder Drive - League City, TX | TX | League City | 1 | 2 | 1 |

## Verification

Typecheck:

```bash
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
```

Result: passed.

Dry-run ingest:

```bash
python3 cre_ingest.py \
  --in /tmp/jll_investor_sitemap_detail_2026-06-12_codex/jll_investor_probe.json \
  --dry-run \
  --keep-artifacts /tmp/jll_investor_sitemap_detail_2026-06-12_codex/ingest_dry_run
```

Result:

```text
staged listings: 3 (skipped, no URL: 0)
  jll-investor: 3
dry run: not connecting
```

URL-only sanity check on generated SQL:

```text
dataUri: 0
base64: 0
cloudinary URL references: 52
pdf URL references: 9
```

Reviewer rerun:

```bash
JLL_INVESTOR_SITEMAP_SCAN_LIMIT=8 npx tsx collect.ts --source=jll-investor --transaction=sale --max-items=4 --concurrency=2 --out=/tmp/jll_investor_sitemap_probe_review_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_investor_sitemap_probe_review_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_investor_sitemap_probe_review_ingest_2026-06-12
```

Result:

- 1,868 unique sitemap detail URLs found.
- 8 detail URLs scanned.
- 2 U.S. rows retained.
- 0 detail errors.
- 2 public document URLs, 13 image URLs, 5 contact rows.
- Only `US` countries were retained.
- Dry-run ingest staged 2 rows and skipped 0 missing URLs.
- URL-only SQL sanity found no `data:` or `base64` strings.

Current-tree rerun after integration cleanup:

```bash
JLL_INVESTOR_SITEMAP_SCAN_LIMIT=8 npx tsx collect.ts --source=jll-investor --transaction=sale --max-items=4 --concurrency=2 --out=/tmp/jll_investor_sitemap_probe_current_tree_2026-06-12.json
python3 cre_ingest.py --in /tmp/jll_investor_sitemap_probe_current_tree_2026-06-12.json --dry-run --keep-artifacts /tmp/jll_investor_sitemap_probe_current_tree_ingest_2026-06-12
```

Result:

- 1,855 unique sitemap detail URLs found.
- 8 detail URLs scanned.
- 3 U.S. rows retained, all with country `US`.
- 3 public document URLs, 15 image URLs, 6 contact rows.
- 0 detail errors and 0 missing URLs.
- Dry-run ingest staged 3 rows and skipped 0 missing URLs.
- URL-only SQL sanity found no `data:` or `base64` strings.

## Current Status

`jll-investor` now has a public-defensible sitemap/detail expansion path for
bounded and full runs. Full runs may still be slow because the sitemap contains
global inventory and requires detail scraping before U.S. filtering. Use low
concurrency and consider `JLL_INVESTOR_SITEMAP_SCAN_LIMIT` for future probes.

## Full Run Completion

Full sitemap detail run completed 2026-06-12 22:47 UTC.

- 1,857 sitemap detail URLs scanned.
- 934 U.S. sale rows retained and live-ingested.
- Lease: 0 (not applicable; investment-sale platform only).
- 878 unique brokers in the full output artifact
  `out/jll_investor_full_sitemap_detail_2026-06-12.json`.
- Live-ingested additively (no broad `--mark-missing`).
- Source-scoped soft-delete applied after user approval: 50 stale early-probe
  rows (from the 04:31 UTC probe ingest) removed.
- Post-cleanup validation: 0 missing-state rows, 0 duplicate source URL groups,
  934 active jll-investor rows (all latest batch), 50 soft-deleted.
- Child rows: 2,572 contacts, 345 documents, 5,658 images.
- All jll-investor rows lack coordinates; the Investor detail path does not
  expose latitude or longitude. This is a known limitation, not a regression.
- Speed env vars used for the full run:
  `JLL_INVESTOR_DETAIL_WAIT_MS=1000`,
  `JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS=8000`,
  `JLL_INVESTOR_DETAIL_CONCURRENCY=4` (commit d0c9f5d63).

`jll-investor` status: **Complete**.
