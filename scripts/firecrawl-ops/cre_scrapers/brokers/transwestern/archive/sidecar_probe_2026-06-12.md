Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Transwestern Sidecar Probe - 2026-06-12

Scope: Transwestern only, remaining-brokerage sidecar check. No live ingest was
run. All document and image handling stayed URL-only.

## Public Path

- Feed: `https://transwestern.com/properties?call=ajax`
- Bucket params: blank all-feed, `Sale`, `Lease`, `Sublease`, and
  `Sale or Lease`
- Detail pages: `https://transwestern.com/property/{PageUrl}`
- Browser still uses a POST payload, but the same public params continue to
  work over GET.

## Evidence Saved

Under `scripts/firecrawl-ops/cre_collector/out/`:

- `transwestern_sidecar_public_probe_2026-06-12.json`
- `transwestern_sidecar_collect_probe_2026-06-12.json`
- `transwestern_sidecar_collect_probe_2026-06-12.log`
- `transwestern_sidecar_collect_probe_filtered_2026-06-12.json`
- `transwestern_sidecar_collect_probe_filtered_2026-06-12.log`
- `transwestern_sidecar_ingest_check_2026-06-12/ingest.sql`

## Fresh Results

Direct public GET probe:

| Probe | HTTP | Rows | Unique `PageUrl` | Bad `PageUrl` |
|---|---:|---:|---:|---:|
| all | 200 | 2,025 | 2,022 | 1 |
| Sale | 200 | 389 | 389 | 0 |
| Lease | 200 | 1,377 | 1,374 | 1 |
| Sublease | 200 | 129 | 129 | 0 |
| Sale or Lease | 200 | 130 | 130 | 0 |
| search `1800` | 200 | 8 | 8 | 0 |

Filtered collector probe:

- Command collected 4 rows, 2 sale and 2 lease, with `--max-items=2`.
- Source totals reported by the adapter: sale side 519 after bucket de-dupe,
  lease side 1,632 after bucket de-dupe.
- Detail errors: 0.
- URL-only child rows from the 4-row sample: 4 document URLs, 4 image URLs,
  12 detailed contacts, 12 profile URLs, and 12 vCard URLs.
- Filter checks: 0 TREC footer PDFs in `brochures`, 0 decorative site images in
  `photos`, and no binary-like top-level fields.
- Dry-run ingest staged 4 Transwestern listings and skipped 0 missing URLs.

## Safe Patch

`scripts/firecrawl-ops/cre_collector/collect.ts` now filters Transwestern detail
assets so footer/compliance PDFs and site chrome images do not become
listing-level documents or photos. The filter is source-specific and keeps
property PDF URLs, feed images, gallery images, broker profile URLs, and vCard
URLs.

## Limits

- This was a bounded sidecar probe, not a full Transwestern run.
- No live Supabase ingest or Supabase validation was run.
- The full source still needs `--max-items=0`, dry-run review, live-ingest
  decision, and Supabase validation before Transwestern can be called complete.
