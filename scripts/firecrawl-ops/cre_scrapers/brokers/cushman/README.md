# Cushman & Wakefield Scraper Notes

Production bulk collection currently lives in `../../../../cre_collector/collect.ts` under source key `cushman-wakefield`. This legacy Python scraper remains useful for detail-page experiments and parser prototyping.

## Public Search API

- Endpoint: `https://www.cushmanwakefield.com/api/properties/search`
- Required query shape:
  - `rfkId=property_search`
  - `view=pins`
  - `site_country=US`
  - `listing_type=Buy` for sale, `listing_type=Lease` for lease
  - `language=en`
  - `limit=100`
  - `offset=<0,100,200,...>`
- Verified totals on 2026-06-12: sale returned `2743`, lease returned `8574`.
- Direct non-browser `urllib` calls can receive Azure Application Gateway `403`; the local Firecrawl API can fetch the endpoint.

## Detail Pages

Detail URLs follow:

`/en/united-states/properties/for-sale/<type>/<state>/<city>/<slug>/<id>-s`

or:

`/en/united-states/properties/for-lease/<type>/<state>/<city>/<slug>/<id>-l`

Search API rows may use `sitecore-www.cushmanwakefield.com`; canonicalize those to `www.cushmanwakefield.com`.

## Rich Data

- JSON-LD often exposes `RealEstateListing`, `datePosted`, address, offer, and offered broker/person metadata.
- Visible markdown sections expose building size, lot size, sale price, rental price, year built or renovated, contact names, phone numbers, profile links, and VCard links.
- PDF document URLs can be present in raw HTML but absent from Firecrawl's extracted `links`. Always scan raw HTML for `assets.cushmanwakefield.com/-/pmedia/...pdf`.
- Property photos are also under `assets.cushmanwakefield.com/-/pmedia/<property-media-id>/...`. Related-listing cards may include other pmedia IDs, so prefer the pmedia IDs that contain PDFs, then fall back to the first property pmedia image group.
- Store document and image URLs only. Do not download or upload the actual PDFs or image binaries into Supabase storage during bulk collection.

## Contact Caveat

VCard URLs expose richer contact data in a browser session, but local Firecrawl and direct Python calls can fail on those endpoints. Store the VCard URL and visible contact fields during bulk runs; use a browser-backed enrichment pass only when email capture becomes necessary.
