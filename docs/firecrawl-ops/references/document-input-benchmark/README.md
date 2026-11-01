Related program: [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134); Firecrawl experiment ledger: [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Document text and native PDF comparison

This reference preserves measured comparisons for the listing-brochure program. The reusable runner is [document_input](../../../../scripts/firecrawl-ops/benchmarks/document_input/README.md). It makes no collector, production model-profile, database, OM-writer or scheduler changes.

The [open-weight model and provider follow-up](2026-11-01-open-models/README.md) adds current model research, synthetic PDF/image capability checks and a small matched brochure challenge. It is separate from the thirty-document cohort below.

The [full-cohort native-PDF comparison](2026-11-01-native-pdf-30/README.md) tests current Luna and Gemini routes against a fresh, matched Haiku reference on all thirty PDFs, with independent source-fact review and actual task costs.

The [three-model XHigh follow-up](2026-11-01-native-pdf-xhigh-30/README.md)
adds 90 whole-PDF calls, source review and matched High/XHigh comparisons for
Meta Contributor, Haiku and Luna. It also preserves the configurable runner,
qualification fixture and generator, offline adoption, complete-reference checks
and comparison code as a [reusable system](../../../../scripts/firecrawl-ops/benchmarks/document_input/README.md).
XHigh was slower and more expensive for all three in this cohort; its quality
changes were mixed, including two truncated Haiku outputs.

## November 1, 2026: 30 documents, three models

**AnyDoc is a useful text-retention baseline. Native PDF input still adds value for visual associations.** Haiku 5.5 was a lower-cost native-PDF candidate worth further testing. This is a provisional result from a convenience sample, not a production-default decision or a representative accuracy ranking.

The same 30 frozen public PDFs contained 189 pages. They included 18 flyers from one brokerage, nine other listing/city-record documents and three IRS controls, including an 84-page publication. Original inputs were independently acquired anonymously, hashed and screened; one accessible but confidentiality-labeled document was excluded. The index spans 11 source hosts. All inputs were PDFs; this does not test Word, Excel, filled risk questionnaires or interactive form-state fidelity.

Each document had six planned cells: AnyDoc text or original PDF, each with current Haiku 5.5, GPT-6.1 Terra and Qwen3.8 Flash. All used the same extraction prompt, `medium` reasoning, a 65,536-token output ceiling, four workers and pinned Anthropic/OpenAI/Alibaba providers. Each prompt requested the full transcription, facts and tables. This deliberately demanding task costs more than a facts-only extraction; these totals are not a forecast for the production brochure worker.

There were **147 cohort API attempts**: 146 HTTP-200 responses and one failed Qwen long-document request. One OCR-needed document prevented all three text-arm calls. Qwen's Alibaba Gateway route rejected a separate image-only PDF capability probe, so its 30 native-PDF cells are explicitly **unsupported, not run**. They were not silently replaced with rendered images. The three capability calls are additional to the cohort. Local outcomes are settled; the failed request's remote billing remains unknown.

## Measured cost and speed

| Model | Input | Structured response recovered / 30 | Median request + parser time | Known cohort Gateway charge |
|---|---|---:|---:|---:|
| Haiku 5.5 | AnyDoc text | 29 | 11.82 s | $0.1380 |
| Haiku 5.5 | Native PDF | 30 | 15.05 s | $0.2255 |
| GPT-6.1 Terra | AnyDoc text | 28 | 19.39 s | $0.4869 |
| GPT-6.1 Terra | Native PDF | 30 | 22.46 s | $1.4722 |
| Qwen3.8 Flash | AnyDoc text | 27 | 21.84 s | $0.0353 plus one unknown charge |
| Qwen3.8 Flash | Native PDF | Unsupported | Not measured | Not measured |

“Recovered” means structurally valid JSON after removal of at most one complete outer `json` fence, HTTP 200 and a normal stop. It does **not** mean all text or facts were captured. Haiku required that fence removal for 23 text and 24 PDF outputs despite JSON-object mode. Strict JSON counts are preserved in the measurements. Two other text-arm outputs were valid JSON but failed the requested member shape; their useful text/facts remain in the private original responses and are not called factual omissions.

Request + parser time is API request latency plus recorded AnyDoc extraction time for the text arm. It excludes downloads, reference preparation, request serialization, batch queueing and cost reconciliation. Medians and p95 values use HTTP-200 requests, including strict format failures; they exclude the failed request, which ended after 340.05 seconds with a transport-or-invalid-JSON outcome. No specific timeout/root cause is established. Request order was fixed, there was one trial per cell, and cache/provider effects were not randomized.

Known cohort debits total **$2.35805111** across 146 priced requests. The two successful capability probes add **$0.00330450**, making **$2.36135561 known**. The failed long Qwen text call and rejected Qwen capability probe have no generation-cost receipt; no zero charge is invented. Generation lookups, explicit non-BYOK status, record hashes and unknown costs are retained. Client-side $50 budget reservations are planning holds, not a provider-enforced hard ceiling.

Median measured per-request charges were $0.001673/$0.00240965 for Haiku text/PDF, $0.009066/$0.020528 for Terra text/PDF and $0.000853245 for Qwen text. The long control materially affects group totals, especially Terra PDF.

## Text retention and missed content

AnyDoc 0.2.4 produced text for **29/30** files and returned OCR-needed for one. Against independent Poppler text, converted documents had median **97.5% token overlap**, with a **38.7% to 100%** range. The comparator is imperfect; repeated text, letter spacing and encoding influence this metric. Overlap ignores reading order, row/column relationships, images and meaning. It is not complete-text recall.

Converted AnyDoc output was 378,412 UTF-8 bytes versus 93,975,967 input PDF bytes for those converted files, about 0.40% by size. That is a comparison of formats, not lossless compression. Conversion-process time totaled 4.73 seconds across the 30 files on this host; it is not end-to-end production throughput.

The 84-page control exposes the danger of retaining only a model's output. AnyDoc's text had 97.81% overlap with Poppler. Model transcription overlap was only 1.71% for Haiku text, 1.22% for Haiku PDF, 4.71% for Terra text and 2.96% for Terra PDF. All four returned normal stops and stayed below the output ceiling. Qwen's text request failed after 340.05 seconds without a usable response. These are observed omissions in model transcription relative to the reference, not proof of a provider page limit. Preserve the parser text independently of the model's structured answer or summary.

Across structurally usable responses, median model-transcription overlaps were 94.78%/100% for Haiku text/PDF, 96.77%/94.10% for Terra text/PDF, and 95.71% for Qwen text. These medians hide severe long-document omissions and must not be presented as full-document extraction accuracy.

## Facts and tables

Before reading model outputs, an independent agent froze 150 reference-text probes and a separate set of 98 field/value probes. Exact field matching is intentionally conservative and materially undercounts correct paraphrases or values with extra context. The full diagnostics retain exact matched/missed/unknown/candidate dispositions; **their “missed” and “wrong association candidate” fields are not counts of proven semantic errors**. Facts stored only in a correctly titled table can be correct even when absent from the standalone `facts` array.

For that reason, a second agent visually checked **six preselected source facts** across the five eligible model/input arms, 30 comparisons in total. Correct includes either a fact or a correctly associated table row.

| Input arm | Correct structured association / 6 | Structured omissions | Incorrect associations |
|---|---:|---:|---:|
| Haiku text | 4 | 2 | 0 |
| Haiku PDF | 6 | 0 | 0 |
| Terra text | 4 | 2 | 0 |
| Terra PDF | 6 | 0 | 0 |
| Qwen text | 5 | 0 | 1 |

This is a small purposive visual challenge, not an accuracy estimate for the 30-document cohort or all brokerages. Some Haiku responses were evaluated only after the explicitly recorded fence recovery.

Two useful failure classes were directly observed:

- **Parser association loss, `public-16`:** the source page pairs clear height with its value. AnyDoc separated that label and placed the value beside parking. Haiku and Terra text outputs omitted the clear-height fact; Qwen text assigned the value to parking. Both native-PDF models retained the correct clear-height association.
- **Model structuring omission, `public-12`:** AnyDoc retained the second building's warehouse information in nearby text. Haiku and Terra text outputs kept the value in transcription but omitted its structured warehouse association. Haiku PDF returned the fact and row; Terra PDF retained the correct source-context table row. Qwen text retained the correct standalone fact.

The other selected visual associations, including `public-07` zoning and boatyard facts, were retained across the eligible arms. Finding a value somewhere in transcription was insufficient: all five noncorrect structured outputs above contained their value somewhere in transcription.

The frozen table reference contained 16 visually checked rows, but only three had sufficient explicit title/header context for the strict automated table metric. Haiku and Terra each matched 2/3 with text and 3/3 with PDF; Qwen's long-control failure leaves those three unknown. The 13 headerless rows remain unscorable under that metric. This denominator is far too small for a general table-accuracy claim.

## Implication for the existing pipeline

Keep AnyDoc text as a separately retained, searchable observation with source hash and parser provenance. Do not replace it with an LLM transcription. Preserve subsequent versions and conflicting facts independently of the current selected value.

For structured brochure facts, **Haiku native PDF deserves a larger representative trial**: its added cost was small in this sample and it recovered associations missed by both text arms. A simple candidate is AnyDoc for retention plus native PDF for layout-heavy brochure enrichment, with text input for straightforward prose and Terra reserved for unresolved cases. Qwen remains a cheap text candidate on this tested route; native PDF was unavailable. Routing and model defaults should follow a larger, stratified, fact-only evaluation rather than a custom heuristic built from these 30 documents.

These findings are inputs to [W5](https://linear.app/agenticassets/issue/AGENTIC-5140) and [W6](https://linear.app/agenticassets/issue/AGENTIC-5141). GetCREdata remains the sole OM-facts writer. This PR activates no retention writer, production model, migration, backlog or schedule.

## Durable artifacts and reproduction

The [run folder](2026-11-01/) contains:

- [Public corpus index](2026-11-01/corpus.json): exact PDF hashes, page/byte counts and safe public URLs. URLs can change; one URL with noncanonical query components is omitted.
- [Per-cell JSON measurements](2026-11-01/measurements.json) and [CSV](2026-11-01/measurements.csv): all 180 planned cells, strict/recovered outcomes, cost, speed and diagnostic metrics. No source text or model response bodies.
- [Parser baseline](2026-11-01/parser-baseline.json), [source-bound visual audit](2026-11-01/visual-association-audit.json) and [run provenance](2026-11-01/provenance.json).

The paid runner was frozen at commit `d352e26caeace55f7134919998f565419e94ae19`, runner SHA-256 `e38889e58214897a374315e59ffdadee64429334def5215eb665cd37b13f5f5b`. Later provenance checks and equivalent limit centralization did not repeat the paid run. The private corpus manifest SHA is `702a4fbd57f56a513da16933d9da988ded70f368b4941dd18d9e88e5e8a8af6d`; the independently reviewed association gold SHA is `0f11938434dad29d0b4972d33386b18f3571e6071babc39e119173d9d9f2a497`.

Original PDFs, parser text, reviewed gold and raw model outputs remain in private ignored experiment artifacts, not in Git. Reproduction requires obtaining the exact source bytes and recreating or obtaining the separately reviewed private gold; the public index alone is not a complete fixture bundle. The harness README documents preparation, supervised paid execution, read-only cost reconciliation and body-free export. No production services need to run for this benchmark.
