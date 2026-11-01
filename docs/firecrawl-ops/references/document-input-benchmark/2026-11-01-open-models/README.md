Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895). Evaluation input for [W5](https://linear.app/agenticassets/issue/AGENTIC-5140) and [W6](https://linear.app/agenticassets/issue/AGENTIC-5141).

# Open-weight document extraction and provider routes

Checked November 1, 2026. This extends the [original thirty-document comparison](../README.md) with live model research, eighteen synthetic capability requests, and a deliberately small matched brochure challenge. It changes no production model, database, writer, scheduler or deployment.

## Research scope

The research snapshot records Artificial Analysis Intelligence Index **v5** scores, their effort labels, Gateway release dates, selected endpoint prices, and creator model-card/license links. Scores describe general intelligence, not document extraction. The pilot requests medium reasoning; a Max or Xhigh benchmark score is not the score of that tested configuration. Prices below are USD per million input/output tokens, checked live and subject to change.

| Candidate | Release in Gateway | AA v5, named effort | Selected provider prices | License / role |
|---|---|---|---|---|
| DeepSeek V4.1 Flash 1017 | October 17 | 32.5 Max | Baseten 0.30 / 1.20; Morph 0.05 / 0.192 | MIT; tested text and page images |
| MiniMax M3.1 Flash | October 19 | 28.9, effort unspecified | Fireworks 0.15 / 0.60 | Community license; tested text and page images |
| GLM 5.3 Flash | August 26 | 27.8, effort unspecified | Baseten 0.15 / 0.50 | MIT; tested text and page images |
| MiMo V2.6 Flash | September 21 | 26.7, effort unspecified | Xiaomi 0.14 / 0.28 | MIT Flash-RL checkpoint; synthetic image test only |
| Qwen 3.8 27B | August 14 | 20.1 Xhigh; 17.5 Medium | Alibaba 0.50 / 3.00; Parasail 0.24 / 2.20; DeepInfra 0.15 / 1.875 | Apache 2.0; synthetic route tests |
| Nemotron 3.5 Super VL | October 15 | 13.6, effort unspecified | Baseten 0.20 / 0.50 | Official NVFP4 checkpoint OpenMDW-1.1; synthetic tests only |
| Inkling Small | July 30 | 12.9, effort unspecified | DeepInfra 0.45 / 1.20 | Apache 2.0; tested text and PDF upload |

Attribution: [Artificial Analysis](https://artificialanalysis.ai/data-api/docs#overview-hero). Serving metadata: [Gateway catalog](https://ai-gateway.vercel.sh/v1/models) and its per-model `/endpoints` resources. The compact research artifact is a selected factual snapshot, not a full API dump. Creator model cards are linked there. Open weights do not establish equivalence between a hosted checkpoint and a downloaded checkpoint; no weights were downloaded or self-hosted in this experiment.

The larger text-first options were also checked. GLM 5.3 Max scored 31.7 at catalog 1.40 / 4.40, and DeepSeek V4 Pro 0813 Max scored 23.1 at 0.66 / 1.98. They did not exceed the newer DeepSeek Flash's Max score in this index. Qwen 3.8 Max 0902 scored 33.6, but its creator distinguishes the hosted Max product from the related downloadable 2.4T weights, so this score is not assigned to those weights. Kimi K3.1 Pro's catalog open-weights flag was insufficient to establish currently downloadable weights and a license. These models were researched, not brochure-tested. Ling 3.1 Flash 1019 is a newer text-only option, with a 29.5 score and a zero-price catalog route; sustained free service and document quality are unproved.

Haiku 5.5 is already inexpensive: current catalog 0.10 / 0.50, AA medium 24.6 and Max 36.2. Terra 6.1 is 1.00 / 5.00, AA medium 38.1 and Max 51.0. Open-weight models are candidates for measured value; open availability alone does not make them cheaper or better.

## PDF support depends on the complete route

The canary was the same synthetic image-only PDF used in the earlier study. Poppler found no text layer. Each response had to copy a marker and correctly associate two unit labels with their rents. The second input was its page rendered as PNG. All calls used provider-only pins, medium effort, a 4,096-token output ceiling and no inference retries.

* **PDF upload passed:** Inkling Small / DeepInfra copied all three checks correctly. This proves that exact Gateway Chat Completions path could read the image-only PDF. It does not reveal whether a provider wrapper rasterized it or prove that model weights consume PDF bytes directly.
* **PDF file-part requests returned HTTP 400:** Qwen 27B / Alibaba, Parasail and DeepInfra; Gemma 4 31B / Parasail; DeepSeek / Baseten; MiniMax / Fireworks; GLM / Baseten; Nemotron / Baseten. Errors differed: unsupported file parts, content-template errors, deserialization errors and request validation errors. This is rejection of the submitted request shape on those routes, not proof that every API for those models lacks PDF support.
* **Same-model provider difference:** the exact Inkling PDF request that passed through DeepInfra returned HTTP 422 file-part validation on the Thinking Machines route. DeepSeek / Morph also rejected the PDF file part (HTTP 400), while its image input passed. The extension changed only the provider pin in each corresponding earlier request; original evidence remains immutable.
* **PNG passed:** DeepSeek / Baseten and Morph; MiniMax / Fireworks; GLM / Baseten; Qwen 27B / Parasail; Nemotron / Baseten; MiMo / Xiaomi. All seven copied the marker and both associations correctly. Later generation receipts confirmed all requested serving providers.

Eight successful receipts totaled **$0.003849642**. Ten rejected requests (nine HTTP 400 and one HTTP 422) had no generation IDs; their costs are unknown, not assumed zero. Capability proof on one page does not establish CRE quality, multipage limits, reliability or a production-ready route.

[Alibaba's direct Responses reference](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-responses) limits its documented PDF `input_file` models and excludes Qwen 27B despite Gateway's model-level PDF flag. Direct Alibaba Responses and Gateway Chat Completions are different API paths. Switching only the Qwen 27B serving provider did not produce a working PDF file-part route in our three probes; its image route did work.

## Brochure challenge method

The same `public-07`, `public-12` and `public-16` PDFs from the original corpus were chosen because their layout and associations had already been visually reviewed. They have 8, 5 and 4 pages, respectively: **three PDFs, seventeen pages**, not another thirty-document benchmark. These are public, source-hash-bound brochures, with no private deals, gated portals or production writes.

Four routes compare the original, unchanged AnyDoc text with **all** pages rendered at 150 dpi and sent as explicitly numbered PNG image inputs: DeepSeek / Baseten, DeepSeek / Morph, MiniMax / Fireworks and GLM / Baseten. Inkling / DeepInfra compares that same text with original PDF upload. All thirty planned model/document/arm cells settled. No route silently replaces a failed PDF request with images.

The shared prompt requests full transcription, structured facts and all table rows, requesting medium reasoning and a 65,536-token output ceiling. The nested `reasoning.effort` field is a [documented Gateway Chat Completions extension](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/reasoning), with `reasoning_effort` as an alias. Gateway translates effort for each serving provider; equal labels do not establish equal thinking-token budgets, and effective settings were not independently audited. This is not a facts-only production extraction workload. No inference request is retried. Full outputs stay in ignored owner-only scratch; committed artifacts contain measurements, abstract document IDs and hashes. The original strict JSON result remains separate from a derived view that removes only one exact complete JSON fence.

An independent agent reviews the same six preselected source facts, including context-bearing table rows. A number appearing somewhere in transcription is insufficient. Qualifiers, units and label/value relationships matter. This purposive challenge is too small to estimate overall accuracy or statistical superiority. Each cell is one invocation; sampling defaults, provider preprocessing and checkpoint revisions were not pinned. Repeated trials are needed to assess reliability. Any Poppler token overlap remains an imperfect text-presence diagnostic, not complete-text recall or table accuracy.

API latency excludes page rendering, downloads, batching and cost reconciliation. Initial render time was not recorded. A separate repeated rendering took 4.08, 7.07 and 2.14 seconds for the three PDFs, respectively; every PNG matched the original render hash. These warm repeat timings are not measured end-to-end run times. Rendering cost and time must be considered before a deployment choice. Hosted provider identity is established by generation receipts, not only the requested pin; exact checkpoint parity is unverified.

## Measured outcomes

All thirty brochure requests returned HTTP 200 and have result-bound, non-BYOK generation receipts confirming the requested provider. Their total charge was **$0.250102286**. Including successful synthetic probes, the new study's known charges are **$0.253951928**, with ten rejected synthetic requests lacking cost receipts. Historical comparators below were not called again and are excluded from new spend.

There are **23 whole-response schema successes, five valid JSON objects with schema mismatches, and two empty final-content responses**. No pilot response was recovered by removing a JSON fence. The five shape failures include list/dictionary transcriptions, string page numbers, a missing fact label and a null table cell. They are not all malformed JSON, and a shape failure does not mean every fact is absent.

The table separates accepted whole responses from a second, **field-level** review of readable fact/table objects. That sensitivity review recovers nine correct associations and one wrong value from the five schema-invalid JSON objects without rewriting any original output. Empty final responses remain unscorable. Clean counts exclude conflicting values. Each row covers the same three documents and six preselected associations, not all source facts.

| Model / serving provider | Input | Whole responses usable / 3 | Clean field-level associations / 6 | Median API seconds | Total USD, three documents |
|---|---|---:|---:|---:|---:|
| DeepSeek V4.1 Flash / Baseten | AnyDoc text | 3 | 6 | 106.68 | 0.0747234 |
| DeepSeek V4.1 Flash / Baseten | Page images | 2 | 6 | 95.46 | 0.0660606 |
| DeepSeek V4.1 Flash / Morph | AnyDoc text | 2 | 4 | 51.89 | 0.005003274 |
| DeepSeek V4.1 Flash / Morph | Page images | 2 | 4 | 78.38 | 0.007603882 |
| MiniMax M3.1 Flash / Fireworks | AnyDoc text | 2 | 3 | 44.71 | 0.01486959 |
| MiniMax M3.1 Flash / Fireworks | Page images | 3 | 5 | 31.99 | 0.01100784 |
| Inkling Small / DeepInfra | AnyDoc text | 3 | 6 | 10.85 | 0.0193255 |
| Inkling Small / DeepInfra | PDF upload | 3 | 6 | 10.51 | 0.03044425 |
| GLM 5.3 Flash / Baseten | AnyDoc text | 2 | 6 | 17.69 | 0.00711335 |
| GLM 5.3 Flash / Baseten | Page images | 1 | 6 | 33.85 | 0.0139506 |
| Haiku 5.5 / Anthropic, historical | PDF upload | 3 after fence recovery | 6 | 20.71 | 0.0137842 |
| Terra 6.1 / OpenAI, historical | PDF upload | 3 | 6 | 38.24 | 0.11171525 |

The independent field-level review finds **52 clean associations, one correct value with a conflicting second claim, three incorrect associations/values/qualifiers, and four unscorable probes** across sixty comparisons. The strict whole-response gate accepts 43 of those associations and leaves fourteen unscorable. The alternate review does not turn schema-invalid outputs into production-compatible responses. Historical Haiku and Terra PDF outputs were re-reviewed against the same six criteria and each retained six correct associations; Haiku needed fence removal on two of three documents.

Notable failures and limits:

* Both Morph responses for `public-16` had empty final `content` despite HTTP 200, `stop`, nontruncated status and billed reasoning tokens. They are empty answers, not malformed nonempty JSON, and were not silently retried or mined for facts from reasoning. Low token prices alone did not establish a reliable result on this route.
* MiniMax supplied a wrong source qualifier in the image arm. Its text arm also supplied a contradictory building-size claim, assigned a warehouse value to the wrong size field, and gave a wrong height value with a wrong page locator. A correct number elsewhere does not cancel these errors.
* DeepSeek / Baseten text and Inkling text retained correct height facts while a separate table row was malformed. Clean probe counts are not a claim that every table cell or page citation is correct.
* Transcription-only Poppler overlap medians for structurally usable outputs ranged from about 58% to 80%; Inkling PDF was 59.3% on three documents. This diagnostic ignores text in separate facts/tables and does not measure semantic completeness. Together with the original long-document study, it supports retaining parser text independently rather than treating a model-generated transcription as the full archive.

## Decision and founder direction

**Cayman's November 1 direction:** use the latest verified **DeepSeek Flash** release as the text-model evaluation baseline. As checked for this study, that is `deepseek/deepseek-v4.1-flash-1017`. Refresh catalog discovery before future work and preserve the exact tested release/provider in each receipt. This is a model-selection direction, not proof that the existing production profile was changed or approval for an unattended floating-version updater.

No further MiniMax or GLM experiments are planned from this work. Their already-completed observations are retained. Future model screening should focus on high-intelligence, inexpensive and fast **direct document/file-input** routes, with open and closed models both eligible. Do not expand general text-model testing beyond the DeepSeek Flash baseline on the strength of this study.

**Inkling Small / DeepInfra is the successful open-weight PDF candidate to carry into a larger representative trial.** It combined three schema-valid PDF responses, all six selected associations and a 10.5-second median at roughly one cent per document here. Its low general-intelligence score did not predict this narrow extraction result. This is a candidate, not evidence of parity across the original thirty documents or a production model choice. Haiku remains an important already-tested direct-PDF comparator: its same-document charge was lower, while Inkling's median request time was lower.

Keep AnyDoc's retained text and source hashes as a separate evidence layer. The next document trial should use a typed facts-only extraction schema plus full retained parser text, test scans and dense tables, score table cells and page citations explicitly, and qualify the actual provider's direct file input. This is evaluation input for W5/W6, not a second extraction writer or a new production router.

## Durable evidence and reproducibility

* [Research snapshot](research.json): selected live scores, releases, provider prices and creator licenses.
* [Capability probes](capability-probes.json): eighteen outcomes, receipt hashes and the same-model provider-change witnesses.
* [Pilot measurements](pilot-measurements.json): all thirty cells, ten three-document groups, syntax/schema distinctions, hashes, costs and API timing.
* [Association audit](association-audit.json): sixty strict decisions, sixty separately labeled field-level decisions, and twelve historical native-PDF comparisons.
* [Historical measurements](historical-comparators.json): twelve prior calls, including text arms, with result/receipt bindings; excluded from new spend.
* [Provenance](provenance.json): study scripts, prompt, input and artifact bindings and scope limits.

Raw PDFs, AnyDoc text, responses and detailed witnesses remain in ignored owner-only scratch. Billing generation IDs are omitted from public exports; immutable result and receipt hashes bind the measurements. Reproduction requires the original source hashes and privately reviewed gold described in the original study. The finite scratch dispatch scripts are recorded by hash rather than installed as a new operational runner; the committed runner and prompt supply the shared request/measurement helpers. There is no one-command reproduction claim for the extension. The first dispatch script stopped on a tuple/list resume-metadata mismatch; a separately hashed resume script verified every prior request identity and skipped completed calls, without overwriting intents or repeating inference. All thirty planned cells are settled and no paid process remains active.

## Prebuilt alternatives and deployment boundary

Document specialists are another useful open-weight category. [PaddleOCR-VL 1.6](https://github.com/PaddlePaddle/PaddleOCR) has a prebuilt PDF/layout pipeline; [Nemotron 3.5 Parse](https://huggingface.co/nvidia/Nemotron-3.5-Parse), released October 17, produces layout classes, bounding boxes and reading order; [olmOCR 2](https://github.com/allenai/olmocr) supplies a PDF-to-Markdown toolkit. These can improve the retained source representation before a general model extracts CRE facts. They were researched, not tested here. A page-image model still needs its documented PDF input pipeline, and olmOCR's default header/footer removal deserves explicit retention testing. No matching specialist model IDs were found in the live Gateway catalog name search; direct hosting availability does not establish a Gateway route. Paddle and olmOCR list Apache 2.0; NVIDIA lists OpenMDW-1.1 and has a tokenizer-license notice discrepancy that should be resolved before redistribution. None is established as a drop-in improvement over the current AnyDoc or Docling path by this experiment.

[OpenRouter's documented PDF file-parser](https://openrouter.ai/docs/guides/overview/multimodal/pdfs) can preprocess a PDF for a model using an explicit OCR or text engine, or pass it to a native file-input model. This is a prebuilt way to avoid owning page rendering, but it is a distinct extraction path that needs the same scan/table evaluation. Its text-only conversion does not establish visual layout fidelity. No OpenRouter inference was run here because an authorized usable credential was not available in the inspected configuration; no provider pricing from that path is treated as measured.

For commercial use, distinguish MIT and Apache checkpoints from custom licenses. MiniMax's community terms include attribution/notice provisions and an authorization threshold; GLM 5.3's larger model has different terms from MIT-licensed GLM Flash. Consult the linked creator licenses before a commercial route change. Endpoint privacy flags also vary: the tested Inkling / DeepInfra route lists no-training but not zero retention, and the tested MiMo / Xiaomi route lists neither. Their synthetic/public trials do not authorize confidential-document processing. There is no production change in this study.
