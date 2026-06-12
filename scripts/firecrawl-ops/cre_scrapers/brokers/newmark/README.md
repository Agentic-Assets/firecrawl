# Newmark Scraper Notes

Production bulk collection uses Newmark's Algolia search API in `cre_collector/collect.ts`.

## Algolia Discovery

- Read `algoliaAppId`, `algoliaSearchApiKey`, and `algoliaIndexName` from `https://www.nmrk.com/properties`.
- Query the Algolia index with:
  - `sectionGroup:Properties`
  - `saleOrLease:Sale` or `saleOrLease:Lease`
  - `country_code:US`
  - `siteHandle:enUs`
- Algolia caps retrievable hits per query, so the production collector splits by state and, when needed, property type.

## Data Shape

Hits expose title, slug, address, city, state, zip, coordinates, sale or lease mode, property types, thumbnails, and size fields.
