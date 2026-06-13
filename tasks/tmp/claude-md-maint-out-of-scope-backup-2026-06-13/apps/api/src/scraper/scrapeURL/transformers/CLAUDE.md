# scraper/scrapeURL/transformers/ -- post-scrape document transformers

Transformers run sequentially after an engine returns raw HTML. Each takes `(meta: Meta, document: Document)` and returns a mutated `Document`.

## Transformer type

```ts
type Transformer = (meta: Meta, document: Document) => Document | Promise<Document>;
```

`Meta` carries `meta.options` (the ScrapeRequest), `meta.logger`, `meta.url`, and `meta.internalOptions`.

## Ordering constraint (critical)

`deriveMetadataFromRawHTML` and `deriveHTMLFromRawHTML` (defined in `index.ts`) **require `document.rawHtml` to be set**. Calling them before raw HTML is available throws. The pipeline in `index.ts` enforces ordering -- do not call transformers individually out of sequence.

## Transformer modules

| File | Format / trigger |
|------|-----------------|
| `llmExtract.ts` | `extract`, `json`, `summary`, `cleanedHtml` formats. Exports `performLLMExtract`, `performSummary`, `performCleanContent`, `generateCompletions`, `generateSchemaFromPrompt`. Uses `getModel` via `lib/generic-ai`. |
| `deterministicJson.ts` | `deterministicJson` internal format. `performDeterministicJson`. |
| `query.ts` | `query` format. `performQuery`. |
| `diff.ts` | `changeTracking` format. `deriveDiff`. |
| `agent.ts` | `agent` format. `performAgent`. |
| `performAttributes.ts` | `attributes` internal format. |
| `redactPII.ts` | Strips PII when `internalOptions.zeroDataRetention` is set. |
| `removeBase64Images.ts` | Strips base64 image data from markdown output. |
| `sendToSearchIndex.ts` | Queues document for search indexing when applicable. |
| `audio.ts` / `video.ts` | `audio` / `video` formats via media URL extraction. |

## Adding a new format/transformer

1. Create `<name>.ts` implementing the `Transformer` signature.
2. Register it in `index.ts` in the correct pipeline stage (after `deriveHTMLFromRawHTML` if you need `document.html`; after markdown derivation if you need `document.markdown`).
3. Guard on `hasFormatOfType(meta.options, "<name>")` from `lib/format-utils` before doing work.
