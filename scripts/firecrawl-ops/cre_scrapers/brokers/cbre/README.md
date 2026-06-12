# CBRE Scraper Notes

Production bulk collection uses the CBRE public listings JSON endpoint in `cre_collector/collect.ts`. This Python scraper is legacy support for source-specific experiments.

## Search API

- Endpoint pattern: `https://www.cbre.com/listings-api/propertylistings/query`
- Important filters:
  - `site=us-comm`
  - `Common.Aspects=isSale` for sale
  - `Common.Aspects=isLetting` for lease
  - `PageSize=200`
  - `Page=<1,2,3,...>`
- The endpoint returns `DocumentCount` and `Documents`.
- Local Firecrawl should use stealth proxy settings for CBRE.

## Data Shape

Rows carry address, coordinates, charges, agent data, brochure URLs, photos, usage type, and area fields. Normalize against the collector vocabulary before ingest.
