# JLL Scraper Notes

Production bulk collection uses the rendered JLL search pages in `cre_collector/collect.ts`. This Python scraper is retained for parser and detail-page experiments.

## Search Pages

- Public sale and lease inventory is exposed through rendered listing pages.
- Cards link to detail pages under JLL property paths.
- Firecrawl needs enough wait time for hydrated cards.

## Known Split

JLL Investor Center is handled separately in the production collector as `jll-investor`. Keep investment-center logic out of this legacy scraper unless a new shared JLL module is created.
