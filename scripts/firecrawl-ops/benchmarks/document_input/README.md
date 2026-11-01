# Document input comparison

Part of AGENTIC-2895. This opt-in benchmark compares the same 30 public PDFs in
three models, each with two input arms: pinned AnyDoc Markdown and the original
PDF. It does not change the collector, extraction service, scheduler, database,
or model profile. Only use reviewed public source documents and a private run
directory; the artifacts contain full document text and model responses.

The corpus `manifest.json` has schema `document-input-benchmark-corpus/v1` and
exactly 30 documents. Each needs a unique short `id`, a corpus-relative `path`,
`sha256`, a corpus-relative `gold_path`, and `gold_sha256`. Every PDF and gold
file is rehashed. Gold uses `document-input-benchmark-gold/v1`, with `facts`
containing `id`, `description`, `aliases` and `tables` containing `id`,
`description`, `rows` of `{cells: [...]}`. Empty or unverified gold remains
empty; this is a partial reference, not an exhaustive ground truth.

Install the collector's existing `document_text` package with its own pinned
lock (`npm ci --ignore-scripts` in that directory) and make `node` and
`pdftotext` available. Preparation is offline: it creates one AnyDoc result and
both plain and layout Poppler references per PDF, then seals their hashes.
AnyDoc errors and OCR-needed results are retained. Native-PDF arm remains
eligible; the text arm is recorded as unavailable for each model, never
silently replaced with a PDF or removed from the denominator.

```sh
python3 scripts/firecrawl-ops/benchmarks/document_input/runner.py prepare \
  --manifest /private/corpus/manifest.json --out /private/new-run-directory
python3 scripts/firecrawl-ops/benchmarks/document_input/runner.py run \
  --manifest /private/corpus/manifest.json --out /private/new-run-directory \
  --env-file /private/gateway.env \
  --unsupported-native-evidence /private/qwen-route-qualification.json
python3 scripts/firecrawl-ops/benchmarks/document_input/runner.py reconcile \
  --manifest /private/corpus/manifest.json --out /private/new-run-directory \
  --env-file /private/gateway.env
python3 scripts/firecrawl-ops/benchmarks/document_input/runner.py report \
  --manifest /private/corpus/manifest.json --out /private/new-run-directory
```

`run` makes paid Gateway calls. Defaults are one shared `medium` reasoning
setting, 65,536 maximum output tokens, four workers and a $50 local budget.
Each in-flight or unresolved request reserves $3.50; a verified non-BYOK,
nonnegative Gateway cost replaces its reservation. BYOK or missing credential
mode remains unknown, because the upstream bill can differ from Gateway debit. This is a conservative planning hold, not a
provider-enforced spending ceiling: tokenization and uncancellable concurrent
requests can exceed the estimate. A Gateway-side key budget is needed for a
hard spending cap. The limit owner is `scripts/firecrawl-ops/limits_and_budgets.py`.
The credential file must be owner-only and contain one `AI_GATEWAY_API_KEY`;
the value is never persisted or logged. No local credential file is created.

The optional Qwen qualification binds a separate actual native-PDF refusal by
hash. Its exact model, provider, HTTP status, error type, and empty-text PDF
conditions must validate. It creates 30 `unsupported_not_run` cells without
additional paid calls; the canary used the equivalent `reasoning_effort` field,
while this runner uses `reasoning.effort`. It does not prove all Qwen routes or
other input types are unsupported. Without the evidence option, native Qwen
requests are attempted like the other cells.

Call intents are published before network dispatch and results are atomically
published with private permissions. A missing result after an intent is
**unsettled**: resume stops for explicit reconciliation rather than charging
again. Completed failures, unsupported input, invalid JSON, and output-token
truncation are never silently retried. The `reconcile` action only calls the
read-only Gateway generation lookup and saves hash-bound cost sidecars; it
never invokes a model. An unavailable cost is `null`, never zero. Run settings,
input, document, gold, prompt, runner and converter hashes prevent accidental mixing.

The report retains all 180 planned cells, including unsupported and failed
ones. Token recall compares model transcription to imperfect Poppler text;
value presence can match the wrong field, and ordered-row matching can match
the wrong table. These are diagnostic proxies, not fact precision or table
accuracy. Review the full private outputs and source pages independently before
making accuracy claims. Images, diagrams, scanned pages, reading order and
AnyDoc extraction completeness are not established by token counts.

Gateway wire format and generation lookup: [Chat Completions PDF inputs](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/images),
[provider options](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/advanced),
and [generation lookup](https://vercel.com/docs/ai-gateway/observability-and-spend/usage).
