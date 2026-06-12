# CBRE Commercial Real Estate Scraping

Verified working 2026-06-11 against local self-hosted Firecrawl at `http://localhost:3002`.
Cloudflare bypass confirmed via playwright-extra stealth. See `playwright-stealth-cloudflare.md`
for the three code changes that make this work.

## URL Structure

```
Search / listing page:
  https://www.cbre.com/properties/properties-for-lease/commercial-space
  https://www.cbre.com/properties/properties-for-sale/commercial-space
  https://www.cbre.com/properties/properties-for-lease/commercial-space?aspects=isSale

Single property detail:
  https://www.cbre.com/properties/properties-for-lease/commercial-space/details/{ID}/{address-slug}

  ID format:   {COUNTRY}-{TYPE}-{NUMBER}
               US-SMPL-6130     US = United States, SMPL = property type code, 6130 = numeric ID
               IDs are NOT sequential in practice; known range runs at least 6130 to 206075.
               The address slug is purely SEO; only the ID matters for routing.

Property documents (brochures, flyers):
  https://www.cbre.com/resources/fileassets/{ID}/{hash}/{uuid}.pdf
  The ID in the PDF path matches the property ID, so you can associate docs to listings.

Property images:
  https://www.cbre.com/resources/fileassets/{ID}/{hash}/{uuid}_Photo_N_large.jpg
```

## Required Firecrawl Settings

CBRE sits behind Cloudflare Managed Challenge. These settings are required:

```json
{
  "proxy": "stealth",
  "waitFor": 6000,
  "timeout": 60000
}
```

- `proxy: "stealth"` triggers the playwright stealth engine (see the fix doc).
- `waitFor: 6000` lets the React SPA hydrate after the Cloudflare check passes. Lower values
  may return incomplete content or the challenge page HTML.
- `timeout: 60000` is needed because stealth rendering takes longer than a plain fetch.

## Scrape a Single Listing

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-6130/6201-east-43rd-street-tulsa-ok-74135",
    "formats": ["markdown", "links"],
    "proxy": "stealth",
    "waitFor": 6000,
    "timeout": 60000
  }'
```

Using the Python helper:

```bash
python3 scripts/firecrawl-ops/cbre_scrape.py scrape US-SMPL-6130
```

## Discover Listings From the Search Page

For production bulk collection, prefer `scripts/firecrawl-ops/cre_collector/collect.ts`.
It uses CBRE's internal JSON API through local Firecrawl stealth and collects
both sale and lease inventory. The search-page method below is still useful for
single-page debugging and detail-page enrichment, but it is not the daily path.

The search page returns ~24 property cards per page load, each with a full detail URL.
Scrape it to seed a batch job.

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.cbre.com/properties/properties-for-lease/commercial-space",
    "formats": ["links"],
    "proxy": "stealth",
    "waitFor": 8000,
    "timeout": 60000,
    "onlyMainContent": false
  }'
```

Filter the returned links for `/details/` to get property URLs. The search page also
surfaces IDs in the page markdown.

## Batch Scraping

Use `/v2/batch/scrape` and poll until complete. The batch endpoint is async.

```bash
# Submit
JOB=$(curl -sS -X POST http://localhost:3002/v2/batch/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://www.cbre.com/.../US-SMPL-191142/...", "..."],
    "formats": ["markdown"],
    "proxy": "stealth",
    "waitFor": 6000,
    "timeout": 60000
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Poll
until curl -s http://localhost:3002/v2/batch/scrape/$JOB | \
  python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['status']=='completed' else 1)"; do
  sleep 8
done

# Fetch results
curl -s http://localhost:3002/v2/batch/scrape/$JOB
```

See `scripts/firecrawl-ops/cbre_scrape.py` for the full Python workflow.

## What the Markdown Contains

A successfully scraped property page includes:

- Property name / address / city / state / zip
- Transaction type (For Sale, For Lease, For Sale/Lease)
- Size (sqft)
- Sale price (when listed) or "Contact Broker For Pricing"
- Lease rate (when listed) or contact broker
- Photo URLs (`resources/fileassets/{ID}/...`)
- Brochure/PDF links (`resources/fileassets/{ID}/...`)
- Property description
- Amenities / features
- Broker name, phone, email
- Location map embed
- Share links (Facebook, LinkedIn, Twitter  -  contain the canonical property URL)

## Extracting Structured Data

Use `json` format with a schema to pull structured fields in one pass (requires `OPENAI_API_KEY`):

```json
{
  "url": "...",
  "formats": [
    {
      "type": "json",
      "schema": {
        "type": "object",
        "properties": {
          "property_id":    { "type": "string" },
          "name":           { "type": "string" },
          "address":        { "type": "string" },
          "city":           { "type": "string" },
          "state":          { "type": "string" },
          "zip":            { "type": "string" },
          "transaction_type": { "type": "string", "enum": ["sale", "lease", "sale_lease"] },
          "size_sqft":      { "type": "number" },
          "sale_price_usd": { "type": ["number", "null"] },
          "lease_price":    { "type": "string" },
          "brokers":        { "type": "array", "items": { "type": "string" } },
          "pdf_urls":       { "type": "array", "items": { "type": "string" } }
        }
      }
    }
  ],
  "proxy": "stealth",
  "waitFor": 6000,
  "timeout": 60000
}
```

## Known Gotchas

**Cloudflare.**
All of `www.cbre.com` and all subpaths are behind Cloudflare Managed Challenge.
`proxy: "stealth"` is required. Without it you get `SCRAPE_RETRY_LIMIT / document_antibot`.
`proxy: "basic"` and `proxy: "auto"` with no fire-engine both fail.

**waitFor must be >= 6000.**
The page is a React SPA. Under ~6 seconds the DOM may still be in the Cloudflare
challenge state even after the JS challenge resolves. 8000 is safer for first-use;
6000 works when the IP is already warmed.

**The address slug in the URL is ignored by CBRE.**
`/details/US-SMPL-6130/anything-here` routes to the same property as the real slug.
You can construct a URL from just the property ID: use any slug placeholder.

**IDs are not sequential.**
Known active IDs span roughly 6130 to 206075 with huge gaps. Brute-force enumeration
over the full range wastes requests. Seed from the search page instead.

**PDF documents are also behind Cloudflare.**
Direct `curl` returns 403. To download a brochure, scrape the detail page first to get
the PDF URL, then pass it to `/v2/parse` using the same stealth settings  -  or download
it inside a Playwright page session that already has Cloudflare cookies.

**The search page shows ~24 listings per load.**
There is no visible pagination in the URL. The underlying data API (Next.js)
uses `/_next/data/` routes that are also behind Cloudflare. Scraping the rendered
search page per filter combination is a fallback discovery path. The current
production collector uses CBRE's internal listings JSON API instead.

**Rate limiting.**
No hard rate limit observed in initial testing, but stagger batch requests at
2-3 per second for sustained runs to avoid IP flagging. The stealth proxy engine
is slower than plain fetch  -  budget ~15-20 seconds per property for a stealth scrape.

## Scale Approach

1. **Bulk inventory phase:** Use `cre_collector/collect.ts` for CBRE sale and
   lease inventory through the internal JSON API.
2. **Enrich phase:** Batch-scrape selected detail URLs only when an EQUIRE
   workflow needs richer markdown or documents.
3. **Document phase:** For each promoted property with PDF links, scrape brochures
   via `/v2/parse` with `proxy: "stealth"` and `parsers: [{"type": "pdf", "mode": "auto"}]`.

The latest full multi-source collector run took `27:01.56` at
`--page-cap=400 --concurrency=3`. CBRE itself is much faster through the JSON API
than through detail-page scraping.
