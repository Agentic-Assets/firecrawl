Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Transwestern Performance And Accuracy Note - 2026-06-12

Scope: Transwestern collector review only. No full run was started by this
review, and no live ingest was run. The main thread already had a full
Transwestern collection running, so this pass used existing artifacts, code
inspection, local saved probe files, process/log observation, and typecheck.

## Files Reviewed

- `START_HERE.md`
- `CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/START_HERE.md`
- `scripts/firecrawl-ops/cre_collector/CLAUDE.md`
- `scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/transwestern/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/transwestern/sidecar_probe_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/collect.ts`
- `scripts/firecrawl-ops/cre_collector/cre_ingest.py`
- `scripts/firecrawl-ops/sql/001_cre_brokerages.sql`

## Current Strengths

- The public GET feed path is real and repeatable:
  `https://transwestern.com/properties?call=ajax`.
- The collector avoids POST-body dependence and skips invalid `PageUrl` values.
- The source is wired into the collector, ingestor mapping, and brokerage seed
  SQL.
- The asset filter is materially improved from the first probe. On the filtered
  4-row sample, document and image rows were URL-only and had 0 TREC/footer
  PDFs and 0 decorative site images.
- TypeScript validation passed with `npm run typecheck`.

## Source Totals And Dedupe

Saved public GET evidence shows:

| Feed bucket | Rows | Unique valid slugs | Notes |
|---|---:|---:|---|
| all | 2,025 | 2,021 valid, 2,022 including `-` | 1 bad `PageUrl` |
| Sale | 389 | 389 | no duplicate slugs |
| Lease | 1,377 | 1,373 | 3 duplicate slugs plus 1 bad `PageUrl` |
| Sublease | 129 | 129 | no duplicate slugs |
| Sale or Lease | 130 | 130 | no duplicate slugs |

Current both-transaction collection intentionally includes `Sale or Lease` in
both sale and lease passes. That means it enriches 2,151 rows:

- sale pass: 389 `Sale` + 130 `Sale or Lease` = 519
- lease pass: 1,373 valid `Lease` + 129 `Sublease` + 130 `Sale or Lease` =
  1,632
- valid bucket union: 2,021 unique slugs
- repeated cross-transaction detail scrapes: 130 `Sale or Lease` slugs

This is acceptable for downstream sale-or-lease merging, but it costs 130 extra
detail scrapes and should be cached if Transwestern becomes a daily source.

Within one transaction pass, duplicate handling is currently first-wins by
slug. That handles exact duplicate feed rows. It does not yet detect a future
case where the same `PageUrl` appears with different facts. Add a mismatch log
or hash-suffixed fallback before relying on silent first-wins for reconciliation.

## Performance Review

The active main-thread full run was observed, not started by this review:

```bash
npx tsx collect.ts --source=transwestern --transaction=both --max-items=0 --concurrency=4 --out=out/transwestern_full_2026-06-12_121302.json
```

At about 26 minutes elapsed, the log showed sale enrichment complete at 519/519
and lease enrichment at 1,400/1,632. The output JSON had not been written yet.

Likely performance drivers:

- Every listing gets a Firecrawl detail scrape with `rawHtml`, `markdown`, and
  `links`.
- Most Transwestern fields can be parsed from raw HTML alone.
- `markdown` currently creates one known bad field, the footer-like description
  fallback, and may be avoidable.
- `Sale or Lease` detail pages are scraped once in the sale pass and again in
  the lease pass.

Refinement recommendation:

1. Add a Transwestern detail cache keyed by deterministic detail URL for the
   duration of a process.
2. Consider a Transwestern-only detail scrape helper that requests `rawHtml`
   only, then extracts all anchor URLs from raw HTML instead of depending on the
   Firecrawl `links` format.
3. Keep a source-specific concurrency knob such as `TRANSWESTERN_DETAIL_CONCURRENCY`
   so daily runs can balance speed against source pressure without changing all
   other sources.

## Accuracy Review

### Description

The filtered 4-row sample still has a bad description on all 4 rows. Each
description starts with footer/TREC/copyright content such as "TREC Information
About Brokerage Services", not property narrative.

Recommendation: do not emit `description` unless a trusted property-body
selector matches. As a guard, drop any fallback description containing
`TREC Information About Brokerage Services`, `Privacy Policy`, `Copyright
Transwestern`, or `Sitemap`.

### Availability And Rates

The current `parseTranswesternAvailability` assumes fixed column positions:
suite, size, rate, type. The sample proves that not every row has the same
shape:

- `1025-w-national-avenue`: `rate="$3,500,000.00"`, `type="Sale"`, which is
  useful but not promoted to `salePriceUsd`.
- `753-w-annoreno-drive`: `rate="Sale"`, `type="10,296"`, showing a shifted
  row when no explicit price is present.
- `869-s-route-53-addison-il`: `rate="Direct-New"`, `type="26,522"`, another
  shifted lease row.
- `300-w-laura-drive-addison-il`: `rate="$12.25"`, `type="Direct-New"`, valid
  lease-like data.

Recommendation: parse the table header or infer by cell count and token type.
Only promote a price or rate when the value is clearly a currency or
currency-per-square-foot field. For sale rows, promote detail availability
currency to `salePriceUsd` when feed `Price` is zero and the row type is Sale.

### Contacts

The detailed probe files show office names are visible on contact cards. The
collector stores name, title, phone, profile URL, avatar URL, and vCard URL,
but the filtered artifact did not include office. Add office parsing when the
selector is stable because it improves broker matching without requiring vCard
downloads.

### Assets

The current filter is good enough to keep:

- 4 filtered sample listings
- 4 document URLs
- 4 image URLs
- 12 detailed contacts
- 0 TREC/footer document URLs
- 0 decorative site-image URLs
- no binary-like top-level fields

Keep storing PDFs, `twurls.com` flyers, image URLs, profile URLs, and vCard URLs
as URLs only. Do not dereference or download them in the collector.

## Recommendations Before Live Ingest

1. Let the active full run finish, then run full dry-run ingest only:
   `python3 cre_ingest.py --in out/transwestern_full_2026-06-12_121302.json --dry-run --keep-artifacts <dir>`.
2. Run quality checks on the full artifact before any live ingest decision:
   duplicate `(sourceKey, id, transactionMode)`, duplicate URL groups, missing
   URL/title/state, invalid coordinates, footer descriptions, bad document URLs,
   decorative images, binary-like fields, and malformed sale price/rate fields.
3. Apply small Transwestern-only code refinements before daily scheduling:
   detail cache, description guard, availability parser hardening, sale price
   promotion from detail availability, office parsing, and optional rawHtml-only
   detail mode.
4. Do not use `--mark-missing` for Transwestern until a full artifact, dry-run
   ingest, live ingest, and Supabase validation are all clean.

## Commands Run By This Review

```bash
sed -n '1,240p' /Users/caymanseagraves/.agents/skills/firecrawl-ops/SKILL.md
rg -n "Transwestern|cre-brokerage|brokerage|CRE listing|collector|firecrawl" /Users/caymanseagraves/.codex/memories/MEMORY.md
sed -n '1,240p' CLAUDE.md
rg --files | rg '(^|/)(START_HERE\.md|BROKERAGE_STATUS_2026-06-12\.md|HANDOFF_LOG_2026-06-11\.md|sidecar_probe_2026-06-12\.md|CLAUDE\.md)$'
git status --short --branch
find scripts/firecrawl-ops/cre_scrapers/brokers/transwestern -maxdepth 3 -type f | sort
find scripts/firecrawl-ops/cre_collector -maxdepth 2 -type f | sort | sed -n '1,200p'
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/START_HERE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/CLAUDE.md
sed -n '1,320p' scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md
sed -n '1,320p' scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md
sed -n '1,320p' scripts/firecrawl-ops/cre_scrapers/brokers/transwestern/sidecar_probe_2026-06-12.md
sed -n '1,520p' scripts/firecrawl-ops/cre_scrapers/brokers/transwestern/README.md
rg -n "transwestern|Transwestern|srcTranswestern|twurls|isTranswestern" scripts/firecrawl-ops/cre_collector/collect.ts
sed -n '3520,3875p' scripts/firecrawl-ops/cre_collector/collect.ts
sed -n '3875,4125p' scripts/firecrawl-ops/cre_collector/collect.ts
sed -n '140,310p' scripts/firecrawl-ops/cre_collector/collect.ts
sed -n '1,220p' scripts/firecrawl-ops/cre_collector/out/transwestern_sidecar_collect_probe_filtered_2026-06-12.log
sed -n '1,220p' scripts/firecrawl-ops/cre_collector/out/transwestern_full_2026-06-12_121302.log
tail -80 scripts/firecrawl-ops/cre_collector/out/transwestern_full_2026-06-12_121302.log
ps -axo pid,etime,command | rg 'collect\.ts|tsx|transwestern' | rg -v 'rg '
ls -lh scripts/firecrawl-ops/cre_collector/out/transwestern_full_2026-06-12_121302.* 2>/dev/null
jq '.' scripts/firecrawl-ops/cre_collector/out/transwestern_sidecar_public_probe_2026-06-12.json
jq '[.listings[] | {id, transactionMode, transactionType, url, docs:(.brochures // [] | length), photos:(.photos // [] | length), contacts:(.contactsDetailed // [] | length), detailError, descriptionLen:(.description // "" | length)}]' scripts/firecrawl-ops/cre_collector/out/transwestern_sidecar_collect_probe_filtered_2026-06-12.json
jq -r '.sources, {totalListings, brokers:(.brokers|length)}' scripts/firecrawl-ops/cre_collector/out/transwestern_sidecar_collect_probe_filtered_2026-06-12.json
rg -n "transwestern|SOURCE_TO_BROKERAGE|cre_brokerages" scripts/firecrawl-ops/cre_collector/cre_ingest.py scripts/firecrawl-ops/sql/001_cre_brokerages.sql
ls -ld /tmp/transwestern_probe_20260612 2>/dev/null && find /tmp/transwestern_probe_20260612 -maxdepth 1 -type f | sort
jq '.' /tmp/transwestern_probe_20260612/feed_summary.json
jq '.' /tmp/transwestern_probe_20260612/detail_summary.json
cd scripts/firecrawl-ops/cre_collector && npm run typecheck
jq -n --slurpfile sale /tmp/transwestern_probe_20260612/ajax_sale.json --slurpfile lease /tmp/transwestern_probe_20260612/ajax_lease.json --slurpfile sublease /tmp/transwestern_probe_20260612/ajax_sublease.json --slurpfile sol /tmp/transwestern_probe_20260612/ajax_sale_or_lease.json 'def slugs($a): $a[0] | map(.PageUrl // "") | map(select(. != "" and . != "-")); {saleUnique:(slugs($sale)|unique|length), leaseUnique:(slugs($lease)|unique|length), subleaseUnique:(slugs($sublease)|unique|length), saleOrLeaseUnique:(slugs($sol)|unique|length), bucketUnionUnique: ((slugs($sale)+slugs($lease)+slugs($sublease)+slugs($sol))|unique|length), emittedByCurrentBoth: ((slugs($sale)+slugs($sol)|unique|length) + ((slugs($lease)+slugs($sublease)+slugs($sol))|unique|length)), saleOrLeaseDuplicatedAcrossTx:(slugs($sol)|unique|length)}'
jq -r '{listings:(.listings|length), docs:([.listings[].brochures[]?.url]|length), photos:([.listings[].photos[]?]|length), contacts:([.listings[].contactsDetailed[]?]|length), badDocUrls:([.listings[].brochures[]?.url | select(test("/Upload/TREC/|privacy-policy|health1\\.aetna\\.com"; "i"))]|length), badPhotoUrls:([.listings[].photos[]? | select(test("/assets/images/(mail|comment|connect-image|tw-logo|Transwestern_2023|tw_gl|transwestern-mapmarker)"; "i"))]|length), footerDescriptions:([.listings[].description? | select(test("TREC Information About Brokerage Services|Privacy Policy|Copyright Transwestern|Sitemap"; "i"))]|length)}' scripts/firecrawl-ops/cre_collector/out/transwestern_sidecar_collect_probe_filtered_2026-06-12.json
date '+%Y-%m-%d %H:%M:%S %Z'
```
