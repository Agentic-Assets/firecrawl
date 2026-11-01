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

Meta Muse Spark 1.3 Contributor was initially screened using our generated synthetic PDF. It passed, with `meta` confirmed and a $0.0002739 receipt. Its 0.10 / 0.20 token rate is distinct from Standard mode's 1.25 / 4.25: [Contributor pricing permits use of submitted files and responses for future training](https://dev.meta.ai/docs/pricing-rate-limits). Cayman then explicitly authorized using this Contributor route for the same public corpus and required it in the test. It is included as a separately sealed thirty-document supplement, with the training terms recorded. This approval does not extend to private deal files. The Standard product's AA Max score is not a separately measured Contributor score or a score for this experiment's requested high effort.

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

The initial private evaluation sketch mentioned an interpolated p90; the final analysis uses explicitly labeled nearest-rank p90. Both variants are retained in the measurements. Reported provider token/cache fields are usage observations, not a reconstruction of charges. For example, Luna reports 590,470 cache-creation tokens; that number does not establish an extra billable fee or a cold-cache estimate. The authoritative costs here are the actual non-BYOK generation receipts.

## Source-reference correction discovered before adjudication

`core-10` contains two different source ranges: a headline and an offering-summary field disagree. The frozen reference's generic label does not distinguish them. The original gold is unchanged; that one probe is marked `uncertain_reference` consistently for all five models and excluded from the scorable semantic denominator. Its source-supported alternatives are retained, rather than labeling a model's faithful headline reading an invention. The original ninety-eight-probe diagnostic denominator remains available.

## Measured cost and speed

**All 150 cohort calls completed**, with matching requested model, provider and non-BYOK cost receipts. Every response reported HTTP 200, normal `stop` and `truncated: false`, including responses that were not usable. No client inference was retried and neither finite dispatcher remains active. Cohort spend was **$1.631255416**; five canaries add $0.00531625, for **$1.636571666 total**. No charge remains unknown.

**Meta Contributor is the strongest cost/speed candidate in this experiment.** Its thirty-document bill was **71.0% lower** than fresh Haiku, and its median request time was **26.2% lower**. These are observations from one small corpus, not a randomized latency study or proof of equal extraction reliability. It ran later with two workers rather than four. Its recorded input-cache hits were 1,683 tokens; Haiku reported zero.

| Route | Cost, all 30 PDFs | Median API seconds | p90 API seconds | Format and reviewed-check passes / 30 | Cost per such pass |
|---|---:|---:|---:|---:|---:|
| Haiku 5.5 / Anthropic | $0.2852334 | 26.77 | 43.38 | 28 | $0.010187 |
| GPT-6 Luna / OpenAI | $0.1753763 | 31.39 | 49.67 | 29 | $0.006047 |
| Gemini 3.8 Flash / Vertex | $0.72137625 | 29.77 | 71.13 | 30 | $0.024046 |
| Gemini 3.5 Flash-Lite / Vertex | $0.3665205 | 15.86 | 21.16 | 25 | $0.014661 |
| Muse Spark 1.3 Contributor / Meta | $0.082748966 | 19.76 | 33.59 | 29 | $0.002853 |

The passing-document gate requires a usable whole response after the permitted single-fence recovery and all scorable selected fact/table references for that document to be correct. The numerator includes **every paid cohort attempt**, including unusable outputs. This is cost per passing **reviewed checks**, not a claim that the entire document was extracted correctly. Known errors outside the references remain outside this gate and are disclosed below.

The cost/speed advantage is not only the long control: excluding that one document leaves Meta at **$0.060131268 versus Haiku $0.1383154** across 29 documents, with medians **19.74 versus 26.58 seconds**. Across the **27 same documents where both models passed format and selected-reference checks**, Meta was cheaper on all 27 and faster on 20; its paired-set median was 19.27 seconds versus Haiku 24.68. Its lower cost and time therefore persist on matched usable cases, with the later-run timing caveat intact.

Luna's full-cohort bill was **38.5% lower** than Haiku's, but median latency was **17.3% higher**. Flash-Lite was faster but cost more and missed or failed to provide more reference facts. Gemini 3.8 Flash improved whole-response format consistency but cost about **2.53 times** Haiku and was not faster in this run.

## What the quality review establishes

The independent review records **490 fact decisions and 80 selected table-row decisions**. After the one source-ambiguous reference is excluded consistently, there are **97 scorable fact references per model**. The field-level semantic view below can inspect otherwise-readable facts/tables in a valid JSON object whose sibling fields violate the whole-response schema. That view does not silently turn a schema-invalid response into a usable one.

| Route | Clean selected facts / 97 | Missing / unscorable facts | Correct selected table rows / 16 | Strict / fence-recovered whole responses |
|---|---:|---|---:|---|
| Haiku | 97 | 0 / 0 | 16 | 9 strict + 19 fence-recovered; 2 schema failures |
| Luna | 97 | 0 / 0 | 16 | 29 strict; 1 schema failure |
| Gemini 3.8 Flash | 97 | 0 / 0 | 16 | 30 strict |
| Gemini 3.5 Flash-Lite | 85 | 4 / 8 | 10; 6 unscorable | 28 strict; 2 JSON syntax failures |
| Meta Contributor | 96 | 1 / 0 | 16 | 30 strict |

Haiku's two whole-response schema failures still contain all selected fact associations in the separate readable-field view. Luna's one schema failure is likewise kept separate from its semantic fact matches. Thirteen headerless table rows were judged against their manually inspected source context; this is a separate manual selected-row result, not an expansion of the original three-row strict matching proxy.

Meta's scored omission was an IRS form-attachment association. Across the **27 non-IRS listing/city-record documents**, it and Haiku both supplied all selected references in the field-level view; Meta passed the whole-response gate on all 27, versus Haiku on 25. This does **not** establish that every property fact in those documents was correct.

**Important errors beyond the frozen checklist:**

* In `core-05`, Luna reversed office and warehouse areas on a plan despite getting all three frozen target facts correct.
* In `core-06`, Meta misread a total-building-area numeral in both a fact and a table while correctly identifying the distinct suite area and the frozen targets. The source plan was visually checked.
* Flash-Lite omitted some structured details even when their literals remained in transcription. Matching text presence is not the same as producing a usable fact.
* Luna and Meta sometimes used a printed folio instead of the physical PDF page index. Examples include the long control's table, where the printed page is 1 but the PDF page is 5, and Meta's `public-15` printed folio 17 on the first PDF page. The prompt did not define this convention precisely, so these are locator mismatches to resolve, not invented numeric values.

These counterexamples are retained in [additional findings](additional-findings.json) and the per-reference flags. The references are not exhaustive, and false-positive precision, unreviewed table cells and complete page citation accuracy were not measured. A 97/97 score cannot be relabeled as error-free extraction.

## Long-document retention remains a separate problem

All five routes received the intact 84-page `external-21` PDF. All reported normal stop without output-limit truncation. Those signals did not establish full transcription:

| Route | Control API seconds | Control cost | Transcription-field Poppler overlap proxy |
|---|---:|---:|---:|
| Haiku | 43.38 | $0.146918 | 2.33% |
| Luna | 43.10 | $0.0798496 | 2.24% |
| Gemini 3.8 Flash | 164.92 | $0.117462 | 4.21% |
| Gemini 3.5 Flash-Lite | 140.26 | $0.0276618 | unavailable: unusable JSON |
| Meta Contributor | 52.37 | $0.022617698 | 3.81% |

For the same source, independently retained AnyDoc text has **287,750 characters** and **97.81%** multiset token overlap against Poppler's 46,194-token reference. Poppler is imperfect; these are diagnostics, not semantic completeness percentages. The model metric measures only the transcription field and ignores additional text in facts/tables. Even so, normal stop, a large output ceiling, newer models and direct PDF input did not establish full text retention. The original parser text must remain independently saved.

## Decision for the existing pipeline

Carry **Muse Spark 1.3 Contributor / Meta** forward as the leading inexpensive, fast candidate from this explicitly approved **thirty-document public corpus**. Its measured cost/speed advantage warrants a production-shaped evaluation; its omitted instruction and confirmed area error prevent calling it a drop-in reliability upgrade over Haiku. This study establishes no private-document approval or production routing decision. Retain Haiku as the reference and escalation candidate. Luna is a lower-cost option with a speed tradeoff, not a winner on both requested dimensions here. Neither tested Gemini route beat Haiku on both cost and speed.

Keep the latest verified **DeepSeek Flash** as the text-model baseline, per the preceding founder direction. This document comparison does not reopen general text-model testing or change a production model profile.

The next W5/W6 evaluation should use the actual typed facts-only schema while retaining AnyDoc/parser text separately, require source evidence and explicit physical-PDF versus printed-page locators, and test numerical/layout grounding on the observed failure cases. Preserve original and conflicting facts rather than discarding useful data because a sibling field is malformed. No extra inference or production change was performed to make these follow-up recommendations.

## Durable artifacts

The report and body-free exports preserve all planned outcomes, actual charges, request timing, source/request/result/receipt hashes and review decisions:

* [Research snapshot](research.json), [primary protocol](protocol.json), [Meta supplement protocol](meta-protocol.json), [exact shared prompt](prompt.txt) and [five qualified input probes](qualification.json).
* [All 150 measured cells and five model summaries](measurements.json), including token/cache counts, both p90 conventions, control outcomes, matched pairs and denominator-aware costs.
* [All 570 source-review decisions](semantic-review.json), with immutable source/result/witness bindings and document-level selected-reference gates.
* [Confirmed errors outside the frozen references](additional-findings.json) and [study provenance](provenance.json).

Raw PDFs, extracted text, full responses, billing generation IDs and private review witnesses remain in owner-only ignored scratch. The original corpus index supplies source URLs and hashes. Private reviewed references and exact source bytes are required for reproduction. Finite scratch dispatch and analysis scripts are identified by hash rather than installed as another operational runner; there is no one-command reproduction claim. No production extraction runner or second OM writer is introduced.
