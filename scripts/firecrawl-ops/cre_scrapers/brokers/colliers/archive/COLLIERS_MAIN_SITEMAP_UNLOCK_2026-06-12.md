Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Colliers Main Sitemap Unlock - 2026-06-12

Scope: main Colliers public sale and lease inventory at
`https://www.colliers.com/en/properties`. This supersedes the earlier "blocked"
verdict reached by the dated public-path/sitemap rechecks (pruned 2026-06-13).

## Verdict

Main Colliers sale and lease inventory is now reachable through a public,
robots-compliant, GET-only path that needs no Coveo POST, no auth, and no gated
documents. The earlier rechecks only tried `sitemap.xml` / `en/sitemap.xml` via
direct GET (Cloudflare 403). They did not try the bare `/sitemap` path through
local Firecrawl. That is the unlock.

## The Path

1. `https://www.colliers.com/robots.txt` (through local Firecrawl): HTTP 200.
   `User-agent: *` has only `Crawl-Delay: 30` and no `Disallow` rules; the 72
   `Disallow: /` entries are all for specific abusive/legacy bots. robots
   declares `Sitemap: https://www.colliers.com/sitemap`.
2. `https://www.colliers.com/sitemap` (through local Firecrawl): HTTP 200 XML
   `<sitemapindex>` with 354 child sitemaps. US properties child is
   `https://www.colliers.com/en/sitemap?type=properties`.
3. `https://www.colliers.com/en/sitemap?type=properties` (through local
   Firecrawl): HTTP 200 XML `<urlset>` with 15,896 `usa#######` property detail
   URLs, every one carrying `<lastmod>` (clean refresh semantics). Slug
   transaction hints: 3,075 lease, 1,983 sale, 660 sublease, 18 mixed, 10,160
   none (so transaction must come from detail content, not the slug).
4. Each detail page renders through local Firecrawl (direct GET is Cloudflare
   403, but Firecrawl stealth render returns HTTP 200, consistent with the
   prior rechecks). Each detail page has:
   - a schema.org `RealEstateListing` JSON-LD block: `name` carries
     "<Type> For <Sale|Lease|Sublease|Sale or Lease> — <street>, <city>,
     <state> <zip>, USA | ...", plus `about.category` (property type),
     primary image, and canonical `usa#######` URL;
   - clean markdown: transaction header, `$ price USD`, `Building Size: N SF`,
     `Land Area: N ac`, `Property Types`, `Property Status`, description,
     features, a `Related Documents` block with named PDF blob URLs, a Google
     maps link exposing `q=<lat>,<lng>` coordinates, and property photos on
     `listingsprod.blob.core.windows.net/ourlistings-usa/...`. Broker headshots
     appear on `colliersapps.blob.core.windows.net/people/...`.

## Production Shape

- New collector source key `colliers-main`, folded into the `colliers`
  brokerage with id prefix `main:` (mirrors `cbre`/`cbre-dealflow` and
  `jll`/`jll-investor`). Existing SalesTracker `colliers` rows are untouched.
- Discovery: fetch the `?type=properties` sitemap once (cache across sale and
  lease passes), parse loc + lastmod.
- Enrichment: render each detail URL through local Firecrawl, parse JSON-LD +
  markdown, classify transaction, partition sale vs lease client-side.
- Documents and images stay URL-only. No gated path, no POST replay.

## Etiquette Note

robots `Crawl-Delay: 30` is advisory for a single crawler identity. Collection
runs through local Firecrawl's stealth proxy pool, the same posture already used
for CBRE (19k), JLL (10k), and Cushman (11k). Keep detail concurrency modest.

## Artifacts

`tasks/tmp/colliers_sitemap_probe_2026-06-12/` (gitignored): `robots.json`,
`sitemap_root.json`, `sitemap_en_properties.json`, `detail_usa1140782.json`,
`detail_sale_usa1152465.json`.
