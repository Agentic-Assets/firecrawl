# NAI Global Scraper Notes

Production bulk collection has limited NAI Global support in `cre_collector/collect.ts`.

## Site Structure

- Public listings are exposed through a widget-style page.
- Some cards do not expose stable per-listing detail URLs.
- Cookie or consent behavior can affect rendered extraction.

## Ingest Consequence

When a card lacks a detail URL, the collector uses a synthesized external ID and may retain the shared widget URL as `source_url`. This is documented in validation as a source limitation.
