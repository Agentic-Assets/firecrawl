Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895). Evaluation input for [W5](https://linear.app/agenticassets/issue/AGENTIC-5140) and [W6](https://linear.app/agenticassets/issue/AGENTIC-5141).

# Open-weight document extraction and provider routes

Checked November 1, 2026. This extends the [original thirty-document comparison](../README.md) with live model research, sixteen synthetic capability requests, and a deliberately small matched brochure challenge. It changes no production model, database, writer, scheduler or deployment.

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
* **PNG passed:** DeepSeek / Baseten and Morph; MiniMax / Fireworks; GLM / Baseten; Qwen 27B / Parasail; Nemotron / Baseten; MiMo / Xiaomi. All seven copied the marker and both associations correctly. Later generation receipts confirmed all requested serving providers.

Eight successful receipts totaled **$0.003849642**. The eight HTTP 400s had no generation IDs; their costs are unknown, not assumed zero. Capability proof on one page does not establish CRE quality, multipage limits, reliability or a production-ready route.

[Alibaba's direct Responses reference](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-responses) limits its documented PDF `input_file` models and excludes Qwen 27B despite Gateway's model-level PDF flag. Direct Alibaba Responses and Gateway Chat Completions are different API paths. Switching only the Qwen 27B serving provider did not produce a working PDF file-part route in our three probes; its image route did work.

## Brochure challenge method

The same `public-07`, `public-12` and `public-16` PDFs from the original corpus were chosen because their layout and associations had already been visually reviewed. They have 8, 5 and 4 pages, respectively: **three PDFs, seventeen pages**, not another thirty-document benchmark. These are public, source-hash-bound brochures, with no private deals, gated portals or production writes.

Four routes compare the original, unchanged AnyDoc text with **all** pages rendered at 150 dpi and sent as explicitly numbered PNG image inputs: DeepSeek / Baseten, DeepSeek / Morph, MiniMax / Fireworks and GLM / Baseten. Inkling / DeepInfra compares that same text with original PDF upload. There are thirty planned model/document/arm cells. No route silently replaces a failed PDF request with images.

The shared prompt requests full transcription, structured facts and all table rows, requesting medium reasoning and a 65,536-token output ceiling. Provider-effective reasoning settings were not independently audited. This is not a facts-only production extraction workload. No inference request is retried. Full outputs stay in ignored owner-only scratch; committed artifacts contain measurements, abstract document IDs and hashes. The original strict JSON result remains separate from a derived view that removes only one exact complete JSON fence.

An independent agent reviews the same six preselected source facts, including context-bearing table rows. A number appearing somewhere in transcription is insufficient. Qualifiers, units and label/value relationships matter. This purposive challenge is too small to estimate overall accuracy or statistical superiority. Each cell is one invocation; sampling defaults, provider preprocessing and checkpoint revisions were not pinned. Repeated trials are needed to assess reliability. Any Poppler token overlap remains an imperfect text-presence diagnostic, not complete-text recall or table accuracy.

API latency excludes page rendering, downloads, batching and cost reconciliation. Initial render time was not recorded. A separate repeated rendering took 4.08, 7.07 and 2.14 seconds for the three PDFs, respectively; every PNG matched the original render hash. These warm repeat timings are not measured end-to-end run times. Rendering cost and time must be considered before a deployment choice. Hosted provider identity is established by generation receipts, not only the requested pin; exact checkpoint parity is unverified.

## Prebuilt alternatives and deployment boundary

Document specialists are another useful open-weight category. [PaddleOCR-VL 1.6](https://github.com/PaddlePaddle/PaddleOCR) has a prebuilt PDF/layout pipeline; [Nemotron 3.5 Parse](https://huggingface.co/nvidia/Nemotron-3.5-Parse), released October 17, produces layout classes, bounding boxes and reading order; [olmOCR 2](https://github.com/allenai/olmocr) supplies a PDF-to-Markdown toolkit. These can improve the retained source representation before a general model extracts CRE facts. They were researched, not tested here. A page-image model still needs its documented PDF input pipeline, and olmOCR's default header/footer removal deserves explicit retention testing. No matching specialist model IDs were found in the live Gateway catalog name search; direct hosting availability does not establish a Gateway route. Paddle and olmOCR list Apache 2.0; NVIDIA lists OpenMDW-1.1 and has a tokenizer-license notice discrepancy that should be resolved before redistribution. None is established as a drop-in improvement over the current AnyDoc or Docling path by this experiment.

[OpenRouter's documented PDF file-parser](https://openrouter.ai/docs/guides/overview/multimodal/pdfs) can preprocess a PDF for a model using an explicit OCR or text engine, or pass it to a native file-input model. This is a prebuilt way to avoid owning page rendering, but it is a distinct extraction path that needs the same scan/table evaluation. Its text-only conversion does not establish visual layout fidelity. No OpenRouter inference was run here because an authorized usable credential was not available in the inspected configuration; no provider pricing from that path is treated as measured.

For commercial use, distinguish MIT and Apache checkpoints from custom licenses. MiniMax's community terms include attribution/notice provisions and an authorization threshold; GLM 5.3's larger model has different terms from MIT-licensed GLM Flash. Consult the linked creator licenses before a commercial route change. Endpoint privacy flags also vary: the tested Inkling / DeepInfra route lists no-training but not zero retention, and the tested MiMo / Xiaomi route lists neither. Their synthetic/public trials do not authorize confidential-document processing. There is no production change in this study.
