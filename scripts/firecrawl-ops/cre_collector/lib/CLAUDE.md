# lib/ Module

## Most Critical Rule

**Shared collector primitives only.** Broker fetch, parse, and `external_id` logic stays in `../sources/`; add to `lib/` only when two or more adapters need the same helper. Types and adapter contracts: `../types.ts`.

## Module Map

| File | Owns |
|------|------|
| `config.ts` | CLI `parseArgs` (strict, import-time); `API_URL`, `flags`, `PAGE_CAP`, `CONCURRENCY`, `OUT_PATH` |
| `scrape.ts` | Firecrawl singleton; `scrapeRaw` / `scrapeDoc` / `scrapeJson` (3× retry); `parseJsonBody` for HTML-wrapped JSON. `scrapeDoc` requests `markdown`/`links`/`images`/`rawHtml` + an `attributes` selector block (video/iframe/anchor) for the harvester |
| `harvest.ts` | Pure `harvestDetail(doc, ctx)` -> `{media, links, documents, images}`. Classifies video/tour providers, documents, links, gallery images from a detail scrape; identity-dedups media (provider+id, vimeo hash preserved); feeds `cre_listing_media`/`cre_listing_links`. No argv/network import |
| `parse.ts` | Shared CRE text parsers (price/sqft/cap-rate/address); Python-mirrored by `cre_parse.py`, verified identical via golden vectors (`tests/ts/lib/parse.test.ts`) |
| `geo.ts` | Pure ZIP/geo normalizers (`zip5`, `geoKey`); the full ZIP->county+CBSA crosswalk lookup lives in Python `cre_geo.py` (backfill + ingest run there) |
| `enrich.ts` | Tier-B enrichment helpers: batch-claim SQL, URL-keyed completion, dead-letter filtering for `cre_enrich.py` |
| `broker.ts` | Run-wide `brokers[]` + `brokerRef()` dedupe (`email\|name\|company`); merged by `collect.ts` |
| `html.ts` | JSON-LD, sitemap XML, entity decode, `stripHtmlText`, `dedupeStrings` |
| `util.ts` | `clean`, `num`, `prune`, `pmap`, `moneyToNumber`, `isPerSfPriceText`, `boundedInt` |

## Naming & Boundaries

- ESM: `.js` import suffix. **`prune()`** every listing before emit. **`pmap(items, CONCURRENCY, fn)`** for detail batches.
- **Not here:** source keys, monitor matrix, ingest mapping, listing vocabulary, default `proxy` (callers pass `stealth` for CF).
- **`scrapeJson`:** use `jsonAttempts` / `jsonBackoffMs` only for Buildout interstitials; keep default 3× scrape retry elsewhere.

## Integration & Gotchas

- One `broker.ts` table per `collect.ts` run across all sources.
- Importing `config.ts` parses argv; tests needing scrape/util alone should avoid it.
- `num()` drops zero; `parseJsonBody` handles Chrome JSON viewer and bad quotes.
- Flat folder only; no submodules.

## References

- `../sources/CLAUDE.md` (adapters, monitor, `external_id`)
- `../CLAUDE.md` (orchestration, ingest, daily ops)
- `../../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md`
