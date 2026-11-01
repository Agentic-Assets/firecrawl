Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895). Evaluation input for [AGENTIC-5140](https://linear.app/agenticassets/issue/AGENTIC-5140) and [AGENTIC-5141](https://linear.app/agenticassets/issue/AGENTIC-5141).

# Native PDF models on the full thirty-document cohort

November 1, 2026. This extends the [original comparison](../README.md) and [three-brochure model/provider screening](../2026-11-01-open-models/README.md). The question is whether a current direct-PDF configuration can improve on Haiku's measured task cost and request time while preserving useful extracted information. It changes no production model profile, collector, writer, database, service, deployment or schedule.

## Selection and qualification

The current Gateway catalog and serving endpoints were checked on November 1. Artificial Analysis Intelligence Index **v5** data was read from the same-day API snapshot. Scores describe general intelligence at the named effort; they are not CRE extraction scores. Rates below are USD per million input/output tokens and do not establish cost per completed document.

| Model | Requested provider | Latest-family release | Input / output rate | AA v5 |
|---|---|---|---|---|
| Claude Haiku 5.5, fresh reference | `anthropic` | October 7 | 0.10 / 0.50 | 29.6 High |
| GPT-6 Luna | `openai` | September 22 | 0.10 / 0.50 | 21.4 High |
| Gemini 3.8 Flash | `vertex` | September 2 | 0.75 / 3.75 | 30.5 High |
| Gemini 3.5 Flash-Lite | `vertex` | July 21 | 0.30 / 2.50 | 11.7, effort unspecified |
| Muse Spark 1.3 Contributor | `meta` | September 2 | 0.10 / 0.20 | Standard product Max 36.4; no separate Contributor score |

Sources: [Gateway catalog](https://ai-gateway.vercel.sh/v1/models) and model-specific endpoint metadata; [Artificial Analysis](https://artificialanalysis.ai/data-api/docs#overview-hero). There was no GPT-6.1 Luna PDF model in the checked catalog. Gemini 3.5 Flash-Lite was the current PDF-capable language-model release in that family; the similarly named 3.8 Flash-Lite entry was speech. Flash-Lite is included as a cost/speed-tier candidate, not a general-intelligence leader. No MiniMax or GLM experiments were added.

Each of the four selected routes passed an actual image-only PDF canary: exact marker, two label/value associations, HTTP 200 and normal stop, with a generation receipt confirming the selected provider. These four requests cost **$0.00504235** in total. Qualification establishes only this simple input path, not completeness on long documents. [Google's document documentation](https://ai.google.dev/gemini-api/docs/document-processing) describes native PDF text and vision; the route-specific requests supply the evidence for the tested Gateway interface.

Meta Muse Spark 1.3 Contributor was also screened using only our generated synthetic PDF. It passed, with `meta` confirmed and a $0.0002739 receipt. Its 0.10 / 0.20 token rate is distinct from Standard mode's 1.25 / 4.25: [Contributor pricing permits use of submitted files and responses for future training](https://dev.meta.ai/docs/pricing-rate-limits). Cayman then explicitly authorized using this Contributor route for the same public corpus and required it in the test. It is included as a separately sealed thirty-document supplement, with the training terms recorded. This approval does not extend to private deal files. The Standard product's AA Max score is not a separately measured Contributor score or a score for this experiment's requested high effort.

## Matched protocol

The original thirty public PDFs are unchanged and hash-verified: **189 pages**, including the complete **84-page `external-21`** control. The convenience sample contains eighteen flyers from one brokerage, nine other listing or city-record documents, and three IRS controls. It is not representative of all brokerage PDFs, filled questionnaires, password-protected portals, DOCX or spreadsheets.

Each model receives the whole original PDF byte upload through Gateway Chat Completions, the original shared full-transcription/facts/tables prompt, `response_format: json_object`, **requested high reasoning**, and **65,000 maximum output tokens**. The original four-route protocol has **120 planned cells**. The later user-authorized Meta supplement adds thirty cells with the same files and settings: **150 planned cells across five models**. The original protocol, requests and results remain unchanged; the supplement has its own protocol, dispatcher and output hashes. Its later start is a timing/cache confound, not a simultaneous five-model randomized trial. No pages or input text are sliced, no unsupported PDF is silently replaced with page images, and no client inference retry occurs. Provider-only pins prevent silent cross-provider fallback.

This is a **fresh matched Haiku reference**, not a cost comparison against the historical medium-effort/65,536-output-token run. A common 65,000 ceiling fits both Google routes; high is a documented effort option on every selected model. Equal effort labels do not imply equal thinking-token budgets or establish provider-effective settings. Sampling defaults and exact provider checkpoint revisions were not pinned.

The prompt asks models to reproduce all text as well as facts and tables. This makes it comparable across these configurations but does not reproduce a facts-only production extraction workload. AnyDoc's source text remains independently retained; model transcription is not substituted for that evidence layer.

The manually supervised finite run permits four concurrent requests and a **$30 cohort planning ledger**, reserving $3.50 for each in-flight or unresolved call. Only a non-BYOK, result-bound cost receipt with the expected provider releases its reservation. This is a client planning control, not a provider-enforced spending cap. Synthetic qualification costs are additional. The Meta supplement has a separate $15 planning ledger and two workers; the original four-worker run remains unchanged. Both use the same no-retry and unresolved-cost rules, with at most six concurrent requests across the two runs. Intents precede network calls; partial/unsettled dispatches stop resumption instead of being charged again. All outcomes retain their thirty-document denominator.

## Quality and comparison definitions

The source manifest and two pre-existing references are frozen before execution: 150 text-reference probes and independently source-reviewed **98 label/value fact probes plus sixteen table rows**. These are selected references, not exhaustive ground truth. No alias or gold value is retuned to fit model outputs.

Whole-response usability means required JSON member shapes, nontruncation and an acceptable normal stop. A separate derived view may remove one exact complete outer JSON fence; it cannot change the immutable original strict result. Syntax failures, schema mismatches, empty final answers, errors and unavailable receipts stay distinct. A valid JSON object with a malformed sibling field can still contain useful facts, so independent field-level semantic review is recorded separately without declaring that response fully usable.

Two independent agent reviewers split the original thirty documents and assess the frozen facts across all five models, including source label, value, units and context. Correctly contextualized table rows may satisfy a fact probe; a literal elsewhere in transcription alone cannot. Conflicting source claims are kept distinct from invented model claims. Page locators and additional facts outside the references are not exhaustively certified. These are agent reviews, not human-certified accuracy.

For tables, the sixteen frozen reference rows require source-supported row/column context and units. Unscorable or ambiguous reference context remains explicit. The earlier strict title/header matching proxy covered only three rows; it is not relabeled as full table accuracy. Any transcription-to-Poppler overlap is an imperfect text-presence diagnostic, not complete text recall, semantic correctness or evidence of all page coverage.

Actual task-cost comparisons include the cost of failed calls, with unknown charges kept unknown. Format-usable tasks and documents passing the reviewed fact checks are reported separately. Median and nearest-rank p90 request times exclude downloads, queueing and cost reconciliation. Successful matched-document comparisons use the same document IDs on both sides; a fast error is not a performance win. Cache-token usage is retained because billed charges are not necessarily cold-cache prices. The full thirty-document cohort remains primary; the long control is also reported separately.

## Source-reference correction discovered before adjudication

`core-10` contains two different source ranges: a headline and an offering-summary field disagree. The frozen reference's generic label does not distinguish them. The original gold is unchanged; that one probe is marked `uncertain_reference` consistently for all five models and excluded from the scorable semantic denominator. Its source-supported alternatives are retained, rather than labeling a model's faithful headline reading an invention. The original ninety-eight-probe diagnostic denominator remains available.

## Durable artifacts

The report and body-free exports preserve all planned outcomes, actual charges, request timing, source/request/result/receipt hashes and review decisions. Raw PDFs, extracted text, full responses, billing generation IDs and private review witnesses remain in owner-only ignored scratch. The original corpus index supplies source URLs and hashes. Private reviewed references and exact source bytes are required for reproduction. No production extraction runner or second OM writer is introduced.
