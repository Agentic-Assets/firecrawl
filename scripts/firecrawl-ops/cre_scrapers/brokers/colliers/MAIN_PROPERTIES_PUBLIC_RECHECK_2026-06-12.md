# Colliers Main Properties Public Recheck - 2026-06-12

Scope: main Colliers public sale and lease path only,
`https://www.colliers.com/en/properties`. SalesTracker was used only as
contrast from existing docs. No live ingest was run. No binary files were
downloaded. No auth, gated agreement, unsafe POST, or Coveo POST path was used.

## Verdict

Main Colliers sale and lease discovery remains blocked for production
collection.

No safe repeatable public GET endpoint was found for the main
`www.colliers.com/en/properties` inventory. Local Firecrawl can render the
main search shell and can render known detail pages, but the rendered search
shell and its sale, lease, and sublease hash-filter variants expose no
`usa######` listing links, no result count, and no pagination. Known detail
pages are useful for enrichment only after a URL is already known.

## What Changed Since Prior Notes

- Direct HTTP access to `www.colliers.com` still returns Cloudflare challenge
  pages for the search shell, filter variants, robots, sitemap, and Coveo GET
  sanity URLs.
- Local Firecrawl now successfully renders known main-site detail pages. Two
  sample detail pages returned property facts, related listing URLs, and one
  public blob document URL each.
- Local Firecrawl did not render main search inventory results. Plain
  `/en/properties` plus hash-filter URLs for sale, lease, and sublease returned
  the same shell with only three links, including SalesTracker.
- The rendered shell confirms the main site is Coveo-backed. It exposes
  `/coveo/rest`, `/coveo/rest/ua`, field suffix `16556`, and facet hashes such
  as `#f:listingtype=[For Sale]`, but GET probes to those Coveo paths returned
  Cloudflare challenge HTML, not JSON results.

## Commands And Artifacts

Artifact directory:

```text
/tmp/colliers_main_public_recheck_2026-06-12
```

Direct GET probe:

```bash
node --input-type=module <bounded direct GET probe>
```

Saved:

```text
/tmp/colliers_main_public_recheck_2026-06-12/direct_get_summary.json
```

Result: all main `www.colliers.com` search, filter, robots, sitemap, and small
search sanity URLs returned HTTP 403 Cloudflare challenge pages. No listing IDs
or inventory JSON were present.

Local Firecrawl health:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: passed. The local API, scrape smoke test, and core containers were up.

Rendered main search shell:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_public_recheck_2026-06-12/firecrawl_main_properties.json \
  --pretty --quiet --print-paths
```

Result: success, HTTP 200, title `Properties | Colliers`, 723 markdown chars,
290,828 raw HTML chars, 3 links, 0 listing links, and a SalesTracker link.

Rendered hash-filter checks:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties#f:listingtype=[For%20Sale]&f:recenttransactions=[0]' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_public_recheck_2026-06-12/firecrawl_main_hash_sale.json \
  --pretty --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties#f:listingtype=[For%20Lease]&f:recenttransactions=[0]' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_public_recheck_2026-06-12/firecrawl_main_hash_lease.json \
  --pretty --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties#f:listingtype=[For%20Sublease]&f:recenttransactions=[0]' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_public_recheck_2026-06-12/firecrawl_main_hash_sublease.json \
  --pretty --quiet --print-paths
```

Saved summary:

```text
/tmp/colliers_main_public_recheck_2026-06-12/firecrawl_hash_summary.json
```

Result: all three returned the same search shell, 0 listing links, and no
`usa######` IDs in raw HTML.

Known detail-page render checks:

```bash
scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties/for-sale-commercial-lot-on-major-thoroughfare/usa-9100-brockington-rd-sherwood-ar-72120-usa/usa1155686' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_public_recheck_2026-06-12/firecrawl_detail_usa1155686.json \
  --pretty --quiet --print-paths

scripts/firecrawl-ops/firecrawl_request.py scrape \
  'https://www.colliers.com/en/properties/kapolei-business-park-phase-i-for-lease-or-sale/usa-kalaeloa-blvd-kapolei-hi-96707/usa1092689' \
  --formats markdown,links,rawHtml \
  --out /tmp/colliers_main_public_recheck_2026-06-12/firecrawl_detail_usa1092689.json \
  --pretty --quiet --print-paths
```

Saved summary:

```text
/tmp/colliers_main_public_recheck_2026-06-12/firecrawl_artifact_summary.json
```

Result: both detail pages returned HTTP 200 through local Firecrawl. The sale
sample exposed 10 listing links and 1 document URL. The sale-or-lease sample
exposed 14 listing links and 1 document URL. This supports future detail
enrichment once a valid URL list exists, but it is not a source feed.

Coveo GET sanity checks:

```bash
node --input-type=module <bounded Coveo GET probe>
```

Saved:

```text
/tmp/colliers_main_public_recheck_2026-06-12/coveo_get_summary.json
```

Result: `/coveo/rest`, `/coveo/rest/search`, `/coveo/rest/search/v2`,
GET query variants, `/coveo/rest/ua`, and `/coveo/rest/v6/analytics` all
returned HTTP 403 Cloudflare challenge HTML.

## Exact Blockers

1. Direct public GET to the main `www.colliers.com` search and Coveo paths is
   Cloudflare-challenged and does not return JSON or server-rendered listing
   markup.
2. The rendered main search shell has the Coveo result container and facet
   controls, but local Firecrawl output has no rendered inventory, no total
   count, no listing URLs, and no page controls.
3. Hash-filter rendering for sale, lease, and sublease does not unlock results
   in Firecrawl.
4. Known detail pages are renderable, but related-listing links are local graph
   hints, not a complete repeatable discovery source with totals or refresh
   semantics.
5. The only already proven repeatable Colliers GET feed remains SalesTracker,
   which is investment-sale oriented and does not cover the main sale plus
   lease inventory.

## Next Unlock

Colliers can move from blocked to implementable only if one of these becomes
available:

1. A public, repeatable inventory endpoint or sitemap/feed that returns main
   Colliers sale and lease listing URLs with totals or pagination.
2. Explicit approval for an authorized Coveo integration, including the request
   body, consent posture, throttling, and evidence that it is an allowed public
   contract.
3. A rendered path that consistently exposes result cards, stable `usa######`
   URLs, result counts, and pagination through local Firecrawl without relying
   on hidden or unsafe POST replay.

Until then, keep production status as partial: SalesTracker investment-sale
subset only, with main Colliers `www.colliers.com/en/properties` sale and
lease discovery blocked.
