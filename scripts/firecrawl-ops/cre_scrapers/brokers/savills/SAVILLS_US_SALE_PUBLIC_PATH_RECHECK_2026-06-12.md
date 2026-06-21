# Savills U.S. Commercial Sale Public Path Recheck - 2026-06-12

Scope: Savills public U.S. commercial sale coverage only. No credentials,
gated flows, consent-blocked surfaces, unsafe POST coverage, binary downloads,
collector edits, shared status edits, or Supabase ingest were used.

## Verdict

No safe repeatable public U.S. commercial for-sale path was found.

Savills sale should remain blocked for EQUIRE CRE completeness claims. The
current collector sale path is still the legacy/global residential path, and
the public commercial sale route variants tested below either returned no rows
or returned commercial listings outside the United States, mostly Canada and
the United Kingdom.

No collector change is recommended from this pass. Do not switch Savills sale
to the commercial route yet: the route is public and parseable, but it is not a
defensible U.S. commercial sale feed.

## Existing Code And Notes Inspected

- `scripts/firecrawl-ops/cre_scrapers/brokers/savills/README.md`
- `scripts/firecrawl-ops/cre_scrapers/brokers/savills/RECHECK_2026-06-12.md`
- `scripts/firecrawl-ops/cre_collector/collect.ts`, Savills section only,
  lines around `srcSavillsCommercialLease` and `srcSavills`

Key current collector facts:

- Lease uses `https://search.savills.com/com/en/list/commercial/property-to-let/united-states-of-america`
  and parses public `__NEXT_DATA__`.
- Sale still uses `https://search.savills.com/com/en/list/property-for-sale/united-states-of-america`
  and card parsing. That path is not CRE-defensible because its accepted U.S.
  rows are residential or global luxury property rows.
- The earlier commercial sale candidate,
  `https://search.savills.com/com/en/list/commercial/property-for-sale/united-states-of-america`,
  exposes one commercial retail object, but it is Toronto, Canada.

## Commands Run

Local Firecrawl health:

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

Result: stack healthy. API root and scrape smoke passed. Docker compose printed
expected warnings for optional unset local env vars.

Existing Savills artifact and Next data inspection:

```bash
node - <<'NODE'
const fs = require("fs");
const files = [
  "scripts/firecrawl-ops/cre_scrapers/brokers/savills/artifacts/2026-06-12-recheck/savills_commercial_sale_correct_candidate.html",
  "scripts/firecrawl-ops/cre_scrapers/brokers/savills/artifacts/2026-06-12-recheck/savills_commercial_sale_candidate.html",
  "scripts/firecrawl-ops/cre_scrapers/brokers/savills/artifacts/2026-06-12-recheck/savills_sale_page1.html"
];
for (const file of files) {
  const html = fs.readFileSync(file, "utf8");
  const raw = html.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/)?.[1];
  const data = raw ? JSON.parse(raw) : null;
  const rows = Object.values(data?.props?.initialReduxState?.properties || {});
  console.log(file, rows.length, rows.slice(0, 3).map((row) => ({
    id: row.ExternalPropertyIDFormatted || row.ExternalPropertyID,
    address1: row.AddressLine1,
    address2: row.AddressLine2,
    isCommercial: row.IsCommercial,
    types: (row.PropertyTypes || []).map((type) => type.Caption),
    office: row.PrimaryAgent?.Office?.OfficeName
  })));
}
NODE
```

Static app and sitemap checks:

```bash
rg -n "api|Api|search|Search|property|Property|graphql|GraphQL|umbraco|__NEXT_DATA__|listings|Listing|POST|fetch|axios|endpoint|Sitemap|sitemap" \
  scripts/firecrawl-ops/cre_scrapers/brokers/savills/artifacts/2026-06-12-recheck/savills_next_list.js

scripts/firecrawl-ops/firecrawl_cli.sh map https://www.savills.us --limit 50 --json --pretty
```

Bounded public GET URL matrix:

```bash
node - <<'NODE'
const urls = [
  "https://search.savills.com/com/en/list/commercial/property-for-sale/united-states-of-america",
  "https://search.savills.com/us/en/list/commercial/property-for-sale/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/usa",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/united-states",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/development-land/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/retail/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/office/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/industrial/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/investment/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/hotel/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/healthcare/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/business-park/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/other-commercial/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/laboratories/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/residential-investment/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/student-accommodation/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/senior-living/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/property-for-sale/serviced-office/united-states-of-america",
  "https://search.savills.com/com/en/list/commercial/development-land-for-sale/united-states-of-america",
  "https://www.savills.us/search/site-search.aspx?q=property%20for%20sale",
  "https://www.savills.us/search/site-search.aspx?q=commercial%20property%20for%20sale",
  "https://www.savills.us/api-endpoints/search-cookie.aspx"
];
for (const url of urls) {
  const res = await fetch(url, { redirect: "follow", headers: { "user-agent": "Mozilla/5.0" } });
  const text = await res.text();
  const raw = text.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/)?.[1];
  const data = raw ? JSON.parse(raw) : null;
  const rows = Object.values(data?.props?.initialReduxState?.properties || {});
  console.log(JSON.stringify({
    url,
    status: res.status,
    finalUrl: res.url,
    bytes: text.length,
    route: data?.routerMap?.currentRoute?.as || data?.query?.url || null,
    totalItems: data?.props?.initialReduxState?.listPage?.totalItems ?? null,
    rowCount: rows.length,
    sampleRows: rows.slice(0, 3).map((row) => ({
      id: row.ExternalPropertyIDFormatted || row.ExternalPropertyID,
      address1: row.AddressLine1,
      address2: row.AddressLine2,
      isCommercial: row.IsCommercial,
      types: (row.PropertyTypes || []).map((type) => type.Caption || type.Type),
      office: row.PrimaryAgent?.Office?.OfficeName,
      agent: row.PrimaryAgent?.AgentName,
      price: row.DisplayPriceText || row.GuidePriceText,
      latitude: row.Latitude,
      longitude: row.Longitude
    }))
  }, null, 2));
}
NODE
```

Sitemap checks:

```bash
node - <<'NODE'
const urls = [
  "https://search.savills.com/robots.txt",
  "https://search.savills.com/sitemap.xml",
  "https://search.savills.com/sitemaps/sitemap_us_index.xml",
  "https://search.savills.com/sitemaps/us/en/sitemap_1.xml",
  "https://search.savills.com/sitemaps/us/en/sitemap_static_1.xml",
  "https://search.savills.com/sitemaps/us/en/sitemap_static_2.xml",
  "https://www.savills.us/sitemap.xml",
  "https://www.savills.us/robots.txt"
];
for (const url of urls) {
  const res = await fetch(url, { headers: { "user-agent": "Mozilla/5.0" } });
  const text = await res.text();
  const locs = [...text.matchAll(/<loc>(.*?)<\/loc>/g)].map((match) => match[1]);
  const hits = locs.filter((loc) =>
    /united-states|commercial|property-for-sale|investment|office|retail|industrial|land/i.test(loc)
  );
  console.log(url, res.status, text.length, locs.length, hits.slice(0, 20));
}
NODE
```

## URLs Tested And Outcomes

### Public commercial sale route

`https://search.savills.com/com/en/list/commercial/property-for-sale/united-states-of-america`

- HTTP 200.
- Public `__NEXT_DATA__` present.
- `rowCount: 1`, `totalItems: 0`.
- Row: `1303 Queen St E`, `Toronto On M4l 1c2`.
- Type: Retail.
- Office: Savills Toronto - Retail.
- Coordinates: Toronto area.
- Outcome: public and parseable, but not U.S.

`https://search.savills.com/us/en/list/commercial/property-for-sale/united-states-of-america`

- HTTP 200.
- Same Toronto retail object as the `com/en` route.
- Outcome: U.S. locale does not make it U.S. inventory.

### Invalid or empty U.S. aliases

`https://search.savills.com/com/en/list/commercial/property-for-sale/usa`

- HTTP 404.
- Public Next data present, but zero rows.

`https://search.savills.com/com/en/list/commercial/property-for-sale/united-states`

- HTTP 404.
- Public Next data present, but zero rows.

### Commercial type route variants

These routes used the public GET pattern:

`https://search.savills.com/com/en/list/commercial/property-for-sale/<type>/united-states-of-america`

Outcomes:

- `development-land`: 2 rows, both Quebec or Montreal office rows.
- `retail`: 1 row, Toronto retail.
- `office`: 1 row, Calgary office.
- `industrial`: 9 rows, first samples were Quebec or Montreal office rows.
- `investment`: 1 row, Edmonton.
- `hotel`: 7 rows, first samples were Scotland or Northern Ireland.
- `healthcare`: 1 row, Calgary.
- `business-park`: 0 rows.
- `other-commercial`: 1 row, Cork, Ireland.
- `laboratories`: 1 row, Edinburgh.
- `residential-investment`: 4 rows, first samples were England.
- `student-accommodation`: 10 rows, first samples were England.
- `senior-living`: 1 row, Glasgow.
- `serviced-office`: 5 rows, first samples were England and Wales.

Outcome: the route grammar is real and public, but the
`united-states-of-america` suffix does not reliably constrain rows to the
United States for commercial sale. It returns Canada or global fallback rows.

### Alternate development-land route

`https://search.savills.com/com/en/list/commercial/development-land-for-sale/united-states-of-america`

- HTTP 200.
- Public `__NEXT_DATA__` present.
- `rowCount: 2`.
- Rows were Quebec development land with Savills Montreal.
- The page linked to the canonical
  `/commercial/property-for-sale/development-land/united-states-of-america`
  shape.
- Outcome: not U.S.

### U.S. corporate site

`https://www.savills.us/search/site-search.aspx?q=property%20for%20sale`

- HTTP 200.
- No Next property data.
- Only relevant sale-like href observed in the bounded output:
  `/services/enhance-capital-strategies.aspx`.
- Outcome: site search does not expose a listing feed.

`https://www.savills.us/search/site-search.aspx?q=commercial%20property%20for%20sale`

- HTTP 200.
- Same outcome as above.

`https://www.savills.us/api-endpoints/search-cookie.aspx`

- HTTP 200.
- 48-byte cookie-style endpoint.
- Outcome: not an inventory endpoint.

Firecrawl map:

- `scripts/firecrawl-ops/firecrawl_cli.sh map https://www.savills.us --limit 50 --json --pretty`
- Returned public pages from the corporate site: home, services, about,
  research, people, and case studies.
- No U.S. commercial sale inventory feed appeared in the first 50 mapped links.

### Sitemaps And Robots

`https://search.savills.com/robots.txt`

- HTTP 200.
- Allows general crawling, disallows tracking parameter patterns.

`https://search.savills.com/sitemap.xml`

- HTTP 200.
- Sitemap index with 43 country or locale sitemap indexes, including
  `https://search.savills.com/sitemaps/sitemap_us_index.xml`.

`https://search.savills.com/sitemaps/sitemap_us_index.xml`

- HTTP 200.
- Three sitemap files:
  - `https://search.savills.com/sitemaps/us/en/sitemap_1.xml`
  - `https://search.savills.com/sitemaps/us/en/sitemap_static_1.xml`
  - `https://search.savills.com/sitemaps/us/en/sitemap_static_2.xml`

`https://search.savills.com/sitemaps/us/en/sitemap_1.xml`

- HTTP 200.
- 40,151 URLs, all property-detail URLs.
- It included the known Toronto retail commercial sale ID
  `9fb099fe-5f4a-47c4-b955-8054a6adc83a`.
- The sitemap does not provide enough type, transaction, or U.S. country
  metadata to derive a safe U.S. commercial sale inventory by itself.

`https://search.savills.com/sitemaps/us/en/sitemap_static_1.xml`
and `https://search.savills.com/sitemaps/us/en/sitemap_static_2.xml`

- HTTP 200.
- Mostly static global list URL combinations in the `us/en` locale.
- They are not a U.S. commercial sale inventory feed.

`https://www.savills.us/sitemap.xml`

- HTTP 200.
- 698 corporate-site URLs, including services, offices, people, case studies,
  industries, tenant representation, capital markets, and site search.
- No public U.S. commercial sale listing feed found.

`https://www.savills.us/robots.txt`

- HTTP 200.
- Points to `https://www.savills.us/sitemap.xml`.

## App Bundle Notes

The Savills Next app bundle references POST-based search and related-property
actions, including search API constants and `method:"POST"` request wiring.
Those were deliberately not used for coverage because this task excludes unsafe
POST coverage claims. No public GET JSON endpoint suitable for U.S. commercial
sale inventory was found in the bounded static inspection.

## Blocker

Savills public search routes currently conflate U.S. locale or U.S. URL suffix
with non-U.S. commercial sale rows. The public commercial sale route is
parseable, but it is not U.S.-defensible:

- Base commercial sale route returns Toronto retail.
- Type-specific commercial sale routes return Canada, the United Kingdom,
  Ireland, or no rows.
- The corporate U.S. site and sitemap expose service/people/research pages,
  not listing inventory.
- The search-domain U.S. sitemap is a locale sitemap and does not carry enough
  metadata to filter safely to U.S. commercial sale rows.

## Recommended Next Step

Keep Savills sale blocked for U.S. CRE collection. Do not ingest sale rows from
the legacy global residential path, and do not replace it with the current
commercial route variants.

The next unlock would need one of:

- A public Savills U.S. commercial sale route whose `__NEXT_DATA__` contains
  U.S. rows with U.S. addresses, U.S. offices or agents, and commercial sale
  types.
- A public authorized Savills U.S. inventory feed or partner feed.
- A product decision to store Savills global/residential sale rows outside CRE
  completeness claims.

Collector change recommendation: none in this task. If the broader collector
is later tightened for production claims, mark Savills sale as unsupported or
exclude the current legacy/global sale rows from CRE completeness rather than
claiming them as U.S. commercial sale coverage.
