# lib/extract/ -- LLM extraction pipeline

Handles the `/extract` endpoint's multi-step LLM extraction flow.

## Entry point

`extractionService(options: ExtractServiceOptions)` in `extraction-service.ts` (1100+ lines). Call this, not sub-functions directly.

Options: `{ request, teamId, subId?, cacheMode?, cacheKey?, agent?, apiKeyId, createdAt? }`.

## Execution steps (tracked in Redis via ExtractStep enum)

1. `INITIAL` -- validates request, resolves URLs
2. `MAP` -- discovers URLs via sitemap/crawl (`url-processor.ts`)
3. `MAP_RERANK` -- ranks URLs by relevance (`reranker.ts`)
4. `SCRAPE` / `MULTI_ENTITY_SCRAPE` -- scrapes target documents (`document-scraper.ts`)
5. `EXTRACT` / `MULTI_ENTITY_EXTRACT` -- runs LLM completions (`completions/`)

## State (extract-redis.ts)

`updateExtract(id, fields)` / `getExtract(id)` -- stored in Redis with 6-hour TTL.
`ExtractStep` enum is the canonical step-name vocabulary; use it for `showSteps` responses.

## Schema helpers (helpers/)

| Module | Purpose |
|--------|---------|
| `dereferenceSchema` | Resolves `$ref` pointers in JSON Schema before passing to LLM |
| `spreadSchemas` | Splits a schema into per-URL sub-schemas for multi-entity extraction |
| `mixSchemaObjects` | Merges partial LLM responses into a single object |
| `transformArrayToObject` | Converts `[{key, value}]` responses to `{key: value}` |
| `deduplicateObjectsArray` | Removes duplicate objects from array results |
| `mergeNullValObjs` | Merges null-value responses with non-null responses |
| `SourceTracker` | Tracks which source URLs contributed to each schema field |

## Completions (completions/)

- `analyzeSchemaAndPrompt` -- classifies schema complexity, picks single vs batch strategy
- `batchExtractPromise` -- parallel multi-document extraction
- `singleAnswerCompletion` -- single-document extraction

## Cost tracking (usage/)

`calculateFinalResultCost`, `calculateThinkingCost`, `estimateTotalCost` from `llm-cost.ts`.
Model prices in `model-prices.ts`. Pass a `CostTracking` instance from `lib/cost-tracking`.

## fire-0/ (legacy engine)

`fire-0/` mirrors this entire directory with `_F0` / `-f0` suffix on all exports (e.g., `extractionService_F0`, `spreadSchemas_F0`). It is the older extraction engine, selected per-team via `CUSTOM_U_TEAMS` in `config.ts`. When changing shared schema-handling logic, check whether `fire-0/` has a parallel copy that also needs updating.
