# Avison Young Detail Enrichment - 2026-06-12

Scope: `avison-young` only. Public URL-only enrichment. No PDF or image
binaries were downloaded, no auth path was used, and no Supabase live ingest
was run.

## Implementation

`scripts/firecrawl-ops/cre_collector/collect.ts` now keeps the SharpLaunch
public feed as the discovery spine, then enriches selected rows with public
detail pages when the run is bounded.

Default bound:

- Finite `--max-items=N`: enrich the selected Avison Young rows for that run.
- Unlimited `--max-items=0`: preserve the existing SharpLaunch-only full-feed
  behavior unless `AVISON_YOUNG_DETAIL_LIMIT` is set.
- `AVISON_YOUNG_DETAIL_LIMIT=0`: force detail enrichment off.
- `AVISON_YOUNG_DETAIL_LIMIT=N`: enrich at most `N` selected rows.
- `AVISON_YOUNG_DETAIL_CONCURRENCY`: capped by the collector `--concurrency`
  setting.

The detail pass fetches the SharpLaunch microsite and Avison public detail URL
when present. It extracts public PDF URLs into `brochures`, public
SharpLaunch CDN image URLs into `photos`, the first `RealEstateListing`
JSON-LD object into raw listing metadata, broker profile URLs matching
`/professionals/-/ayp/view/`, and VCard-like URLs when visible. Any failed
detail fetch stays row-local in `detailError`.

## Commands

Local Firecrawl health:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Small public detail probe through local Firecrawl:

```bash
node --input-type=module <<'NODE'
// POST /v2/scrape against four known Avison/SharpLaunch detail URLs.
// Count PDFs, SharpLaunch images, JSON-LD scripts, broker profile URLs, and
// VCard-like URLs without downloading binaries.
NODE
```

Bounded collector run:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=2 --concurrency=2 --out=/tmp/avison_young_detail_enriched_probe_2026-06-12.json
npx tsx collect.ts --source=avison-young --transaction=both --max-items=2 --concurrency=2 --out=/tmp/avison_young_detail_probe_after_ingest_filter_2026-06-12.json
```

Dry-run ingest:

```bash
python3 cre_ingest.py --in /tmp/avison_young_detail_enriched_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/avison_young_detail_enriched_ingest_check_2026-06-12
python3 cre_ingest.py --in /tmp/avison_young_detail_probe_after_ingest_filter_2026-06-12.json --dry-run --keep-artifacts /tmp/avison_young_detail_probe_after_ingest_filter_ingest
```

Typecheck:

```bash
npm run typecheck
```

## Counts

Local Firecrawl health passed: API root and scrape smoke test succeeded.

Direct detail probe results:

| URL family | Public PDFs | SharpLaunch images | JSON-LD | Broker profiles | VCards |
|---|---:|---:|---:|---:|---:|
| `ayus4316jmturkroad.sharplaunch.com` | 3 | 15 | 0 | 0 | 0 |
| Avison 4316 J M Turk Road page | 3 | 15 | 1 | 0 | 0 |
| `ayusalmaschoolcorporatecenteriii.sharplaunch.com` | 1 | 11 | 0 | 0 | 0 |
| Avison Alma School page | 1 | 11 | 1 | 0 | 0 |

Collector output for `--max-items=2 --transaction=both`:

- 4 listings total.
- 2 sale rows and 2 lease rows enriched.
- 6 PDF document URLs.
- 36 public image URLs.
- 5 contact rows.
- 4 rows with JSON-LD captured.
- 1 broker profile URL.
- 0 VCard URLs.
- 0 detail errors.

Dry-run ingest:

- Staged listings: 4.
- Skipped missing URLs: 0.
- Brokerage rows staged: `avison-young: 4`.
- SQL artifact: `/tmp/avison_young_detail_enriched_ingest_check_2026-06-12/ingest.sql`.
- Repeat dry-run after the ingestor URL filter staged 4 and skipped 0.

Full-feed guard check:

```bash
npx tsx collect.ts --source=avison-young --transaction=both --max-items=0 --concurrency=4 --out=/tmp/avison_young_full_sharplaunch_after_detail_patch_2026-06-12.json
python3 cre_ingest.py --in /tmp/avison_young_full_sharplaunch_after_detail_patch_2026-06-12.json --dry-run --keep-artifacts /tmp/avison_young_full_sharplaunch_after_detail_patch_ingest
```

- Full run remained SharpLaunch-only by default: 0 detail-enriched rows, 0
  documents, 0 detail errors.
- Current live source drifted slightly from the earlier ingested artifact:
  2,332 raw rows staged to 2,199 unique rows, with 0 skipped missing URLs.
- No live Avison reingest was run from that drifted full probe.

## Limits

- The enrichment is detail-page request heavy, so full-feed runs stay
  SharpLaunch-only unless an explicit detail limit is set.
- VCard URLs remain unproven in the checked samples.
- Broker profile URL coverage is sparse and only stored when a broker-specific
  Avison profile link is visible.
- PDFs and images are stored as URLs only.
