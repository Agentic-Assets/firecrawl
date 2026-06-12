# Colliers Scraper Notes

Colliers is currently unsupported in the production collector.

## Blocker

The property search at `https://www.colliers.com/en/properties` loads results through Coveo POST requests behind consent and application-gateway behavior. No stable public GET endpoint or server-rendered listing markup has been verified.

## Research Path

Use this folder for request-replay experiments, but keep production ingest disabled until a repeatable public path exists.

## 2026-06-12 Deep Dive Notes

Status: main Colliers sale plus lease coverage remains blocked. The current
`www.colliers.com/en/properties` Coveo path is still not safely collectable as a
public GET feed.

Partial path found: Colliers SalesTracker at `https://sales.colliers.com/`
embeds RCM ListingEngine, and these public GET endpoints worked in bounded
probes:

```text
https://my.rcm1.com/api/AjaxEngine/GetListingsHtml?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=5
https://my.rcm1.com/api/AjaxEngine/GetMapData?pv=BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4&Start=1&PageSize=5
https://my.rcm1.com/api/handler/slp/Init?pv=<landing-page-pv>
```

Evidence:

- Direct `www.colliers.com` probes for properties, robots, and sitemap returned
  Cloudflare 403 challenge pages.
- Local Firecrawl rendered the main properties shell, but it showed no matching
  results and exposed only a path toward SalesTracker.
- The main raw HTML contained Coveo Sitecore config and `/coveo/rest`, but no
  `usa######` listing IDs or embedded listing JSON.
- A tiny Coveo GET probe returned a Cloudflare challenge, including through
  local Firecrawl.
- The SalesTracker RCM list endpoint returned `success: true`, `total: 1653`,
  `totalAvail: 2094`, and `numProjects: 5`.
- Pagination worked with `Start=6&PageSize=5`.
- RCM map GET returned coordinates and `ProjectId` values.
- A sample SLP detail GET returned ProjectId `150540`, title
  `Land - 8304 S. Broadway`, Los Angeles location fields, asking price
  `$1,140,000`, project type `Investment Sale`, asset type
  `Land - Multifamily`, 5 gallery image URLs, 1 Colliers contact, and a
  brochure viewer URL. `IsLeasingProject` was false.

Artifacts:

```text
/tmp/colliers_probe_2026-06-12
```

Recommendation:

- Keep complete Colliers coverage blocked until a public GET, authorized
  request-body Coveo integration, or other safe repeatable path covers the main
  sale and lease inventory.
- Optionally add partial Colliers support as SalesTracker investment-sale-only,
  if EQUIRE wants sale-only public RCM coverage under the `colliers` source key.

Partial adapter plan:

1. Build a SalesTracker adapter using GET list, GET map, and GET SLP detail.
2. Treat rows as sale-only unless a lease-specific public endpoint is proven.
3. Use `ProjectId`, detail PV, or landing PV as the documented external-id
   policy.
4. Run full pagination conservatively with `PageSize=50`.
5. Store brochure viewer, image, source, and contact URLs only.
6. Dry-run ingest and verify child URL rows before any live ingest.
