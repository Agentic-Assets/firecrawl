# lib/ Module

## Most Critical Rule

**Shared collector primitives only.** Broker fetch, parse, and `external_id` logic stays in `../sources/`; add to `lib/` only when two or more adapters need the same helper. Types and adapter contracts: `../types.ts`.

## Module Map

| File | Owns |
|------|------|
| `config.ts` | CLI `parseArgs` (strict, import-time); `API_URL`, `flags`, `PAGE_CAP`, `CONCURRENCY`, `OUT_PATH` |
| `scrape.ts` | Firecrawl singleton; `scrapeRaw` / `scrapeDoc` / `scrapeJson` (3× retry); `parseJsonBody` for HTML-wrapped JSON |
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
