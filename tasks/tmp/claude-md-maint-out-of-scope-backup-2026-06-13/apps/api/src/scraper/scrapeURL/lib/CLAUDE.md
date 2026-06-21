# scraper/scrapeURL/lib/ -- scraper utilities

Internal helpers used by engines and transformers. Do not bypass these with raw stdlib calls.

## HTTP client

`robustFetch<Schema>` from `fetch.ts` -- typed HTTP client built on undici (not node-fetch or axios).
- Accepts a Zod `schema` for response validation; throws `ZodError` on mismatch.
- Supports retry via `tryCount` / `tryCooldown`.
- Accepts an `AbortSignal` for cancellation.
- Uses `cacheableLookup` for DNS by default; pass `useCacheableLookup: false` to skip.
- `mock` parameter (pass `null` in production) enables test replay.

## Abort management

`AbortManager` from `abortManager.ts` -- 3-tier abort hierarchy: `external` (user cancel) > `scrape` (job timeout) > `engine` (engine-level timeout). Engines receive an `AbortManager` instance and must call `.dispose()` when done. `AbortManagerThrownError` is thrown when an abort fires.

## DNS

`cacheableLookup` from `cacheableLookup.ts` -- singleton cacheable DNS lookup. Passed to undici agents in `fetch.ts`. Engines that create their own undici Agent should pass `cacheableLookup.lookup` as the connect.lookup.

## HTML parsing helpers

| File | Export | Use |
|------|--------|-----|
| `extractLinks.ts` | `extractLinks(meta, html)` | Returns link list from raw HTML |
| `extractImages.ts` | `extractImages(meta, html)` | Returns image list |
| `extractMetadata.ts` | `extractMetadata(meta, html)` | Open Graph, JSON-LD, title, description |
| `extractAttributes.ts` | `extractAttributes(html, selector)` | CSS-selector attribute extraction |
| `removeUnwantedElements.ts` | `htmlTransform(html, options)` | Strips scripts/styles/noise before markdown conversion; call before `parseMarkdown` |

## Smart scrape

`extractSmartScrape` / `smartScrape` -- AI-powered content detection (used by transformers, not engines directly). `urlSpecificParams.ts` and `rewriteUrl.ts` inject per-URL parameter overrides.
