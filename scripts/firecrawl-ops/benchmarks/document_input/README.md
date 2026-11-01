# Document input comparison

## Configurable native PDF trials

Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and
[AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).
[`native_pdf.py`](native_pdf.py) is the reusable finite benchmark for arbitrary
model/provider/effort grids. It accepts 1–1,000 explicitly selected PDFs, verifies
their SHA-256 and actual page counts with Poppler `pdfinfo`, seals source and
code identities, qualifies each route against the owned image-only PDF, and
preserves failed and unstarted cells. It reuses the original transport and
prompt. It creates no scheduler, production writer or persistent service.

The [example config and synthetic corpus walkthrough](examples/README.md) run
offline until paid execution is explicitly selected. Config entries specify
`model`, pinned `provider`, `effort` and `max_tokens`; high and xhigh for the same
model/provider produce different identities. Check current route capabilities
and provider terms for the intended corpus. Approval for the measured Meta
Contributor trial applies to its frozen public documents; the generic tool
does not extend that approval to other files.

```sh
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py prepare \
  --config /private/grid.json --manifest /private/corpus/manifest.json --out /private/new-trial
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py qualify --out /private/new-trial
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py run --out /private/new-trial
# Only these --execute commands invoke paid inference:
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py qualify \
  --out /private/new-trial --execute --env-file /private/gateway.env
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py run \
  --out /private/new-trial --execute --env-file /private/gateway.env
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py reconcile \
  --out /private/new-trial --env-file /private/gateway.env
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py report --out /private/new-trial
```

`prepare`, preview `qualify`/`run`, `report`, `adopt` and `compare.py` read no
credentials and make no network calls. Paid actions accept `AI_GATEWAY_API_KEY`
from the process environment or an owner-only `--env-file`; no machine-specific
credential path is built in. `reconcile` only GETs existing generation receipts.
A verified receipt must match generation, model, pinned provider and explicit
non-BYOK mode. Missing receipts retain the planning reservation; cost stays null.
The shared budget limits are planning controls, not provider-enforced caps.

The manifest uses `document-input-benchmark-corpus/v1`; each document has `id`,
corpus-relative `path`, `sha256` and `pages`, with optional paired `gold_path`
and `gold_sha256`. Inputs may not escape the corpus or use symlink components.
Run folders are owner-only. Sealed inputs/code/config cannot change during a
trial. A cell is intended before dispatch and published atomically. Interrupted
intents stop further dispatch; failures and unsupported inputs are never paid
retries. Start a separately identified trial for an intentional repeat.

`adopt` inspects older whole-PDF experiment artifacts in place, without copying,
rewriting or paying for them. Supply the exact historical manifest, protocol,
route config and executed script. It reconstructs requests and verifies
intent/result/response/cost bindings. Its report counts settled results only;
the original protocol/export remains the authority for the complete planned grid.

```sh
python3 scripts/firecrawl-ops/benchmarks/document_input/native_pdf.py adopt \
  --source /private/old/results --manifest /private/corpus/manifest.json \
  --protocol /private/old/protocol.json --config /private/old/routes.json \
  --script /private/old/dispatch.py
```

### Reusable evaluation and comparisons

[`compare.py`](compare.py) consumes saved `report`/`adopt` JSON and optional
source-reviewed decision files. It checks the manifest, prompt and PDF identities,
keeps model/provider/effort/output-cap groups separate, and pairs only common
documents. It reports HTTP/format denominators, actual cost coverage, median,
explicit nearest-rank and interpolated p90 latency, and matched cost/speed counts.
Unknown cost is never zero or a complete group total. Cost per selected-reference
pass counts all known group charges, including failed attempts.

```sh
python3 scripts/firecrawl-ops/benchmarks/document_input/compare.py \
  --report /private/high-report.json --report /private/xhigh-report.json \
  --review /private/high-review.json --review /private/xhigh-review.json \
  --gold /private/corpus/reviewed_associations.json \
  --out /private/new-comparison.json
python3 -m pytest scripts/firecrawl-ops/benchmarks/document_input -q
```

Review files have a `decisions` list. Each decision binds `document_id`, `model`,
`requested_provider`, `source_pdf_sha256`, `result_sha256`, `gold_sha256`, `kind`
(`fact` or `table_row`) and `probe_id`. Record `strict_decision` and
`semantic_decision` as `clean_correct`, `missing`, `wrong_value`,
`wrong_association`, `correct_with_conflict`, `wrong_unspecified`, `uncertain_output`,
`unscorable` or `uncertain_reference`. Legacy `wrong` maps to `wrong_unspecified`,
`uncertain` to `uncertain_output`, `incorrect_association` to `wrong_association`,
`incorrect_value` to `wrong_value` and `unscorable_strict_format` to `unscorable`.
Conflicted and uncertain outputs never qualify as clean passes. Record source ambiguity
as `uncertain_reference` in both views. Both denominators exclude that same target.
Keep private witnesses and source context separately; the report contains no
document text. Review evidence from different gold versions cannot be combined.
`--gold` is required with review files. Its exact bytes must match the review's
gold hash, PDF hashes and manifest. Each document must cover every fact and table
probe in that frozen roster before it can count as a selected-reference pass.
Incomplete review stays visible and contributes no pass. Raw HTTP-200 timing
comparisons are separate from pairs where both outputs pass format and the
complete selected-reference review.

Freeze references before reading outputs. Review labels, values, units, qualifiers,
table headers and page context against the original pages. A transcription-only
value is not a structured fact. Count facts in an appropriately associated table.
Preserve strict outcomes; permit exactly one complete outer JSON fence only as a
separate sensitivity. Otherwise readable fields of a wrong-shape JSON response
need an explicitly labeled field-level sensitivity. Never repair syntax or call
output-token truncation complete. Additional discovered errors stay in a separate
challenge set rather than silently expanding or changing the frozen gold.

Selected reference pass rates do not establish exhaustive accuracy. Retain AnyDoc
text independently; LLM transcription is not a lossless archive. A normal stop
does not prove full text recall. Record long-document controls, cache/reasoning
usage, provider mapping uncertainty and timing/concurrency differences separately.
Requested xhigh is a recorded request setting, not proof of extra provider compute.

The owned [fixture and its generating code](fixtures/README.md), examples,
transport, postprocessing, adjudication, comparison and synthetic tests are committed
together. Original document bodies, credentials and complete model responses stay
in private artifacts. Measured studies link durable body-free results from the
[findings index](../../../../docs/firecrawl-ops/references/document-input-benchmark/README.md).

## Original paired AnyDoc-text/PDF experiment

Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895). This opt-in benchmark compares the same 30 public PDFs in
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


## Measured findings and body-free export

The [30-document findings and committed measurements](../../../../docs/firecrawl-ops/references/document-input-benchmark/README.md)
record the corpus, costs, speed, visual association witnesses and limitations.
Retain the raw parser text independently of model-generated transcriptions.
`postprocess.py` preserves original hashes and strict outcomes while deriving a
separate view of one complete outer JSON fence. `summarize.py` explicitly scores
that recovered-content sensitivity, excludes truncated answers, validates the
prepared inputs and complete intent identity, and exports metadata only:

```sh
python3 scripts/firecrawl-ops/benchmarks/document_input/summarize.py \
  --manifest /private/corpus/manifest.json \
  --gold /private/corpus/reviewed_associations.json \
  --expected-gold-sha256 <reviewed-blind-gold-sha256> \
  --results /private/settled-run --out /private/new-measurements
```

Gold is independently agent-reviewed, not a human certification. Exact matches
are lower-bound probes, not semantic precision. A valid response and normal stop
also do not establish full text or fact coverage.
