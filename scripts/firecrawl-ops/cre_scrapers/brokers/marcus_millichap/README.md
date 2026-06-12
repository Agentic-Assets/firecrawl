# Marcus & Millichap Scraper Notes

Production bulk collection has limited Marcus & Millichap support in `cre_collector/collect.ts`.

## Site Structure

- The public property grid is investment-sale oriented.
- Lease inventory is not public in the same way and is skipped by the production collector.
- Some deal-room or financial details require registration.

## Current Limitation

The public collector should only ingest visible card and detail data. Do not synthesize financial fields that are gated or absent.
