# Firecrawl PDF OCR Benchmark

## Recommended Mode

| PDF | Mode | Profile | Why |
| --- | --- | --- | --- |
| ssrn-fen-6255160-ai-driven-credit-intelligence-human-loop-d55489f2-1ce8edd5 | ocr | research-page-aware | Outputs were similar enough; prefer the fastest successful mode. |
| ssrn-fen-6537058-marketsense-do-small-ai-models-understand-968a3055-e4cfde12 | ocr | research-page-aware | Outputs were similar enough; prefer the fastest successful mode. |
| ssrn-fen-6733378-patient-access-readiness-governable-signal-conceptual-c12b89cd-6b077191 | ocr | research-page-aware | Outputs were similar enough; prefer the fastest successful mode. |

## Raw Results

| PDF | Mode | Profile | Exit | Expected | Seconds | Markdown | Page Breaks | Pages | Tables | Figures | Warnings |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ssrn-fen-6255160-ai-driven-credit-intelligence-human-loop-d55489f2-1ce8edd5 | fast | none | 1 | yes | 0.13 | 0 | 0 | None | 0 | 0 | abstract_not_detected_early |
| ssrn-fen-6255160-ai-driven-credit-intelligence-human-loop-d55489f2-1ce8edd5 | ocr | research-page-aware | 0 |  | 22.28 | 25821 | 13 | 14 | 0 | 0 |  |
| ssrn-fen-6537058-marketsense-do-small-ai-models-understand-968a3055-e4cfde12 | fast | none | 1 | yes | 0.09 | 0 | 0 | None | 0 | 0 | abstract_not_detected_early |
| ssrn-fen-6537058-marketsense-do-small-ai-models-understand-968a3055-e4cfde12 | ocr | research-page-aware | 0 |  | 12.19 | 8252 | 5 | 6 | 0 | 7 |  |
| ssrn-fen-6733378-patient-access-readiness-governable-signal-conceptual-c12b89cd-6b077191 | fast | none | 1 | yes | 0.09 | 0 | 0 | None | 0 | 0 | abstract_not_detected_early |
| ssrn-fen-6733378-patient-access-readiness-governable-signal-conceptual-c12b89cd-6b077191 | ocr | research-page-aware | 0 |  | 22.18 | 28549 | 9 | 10 | 23 | 1 |  |

Note: `creditsUsed` is Firecrawl's local per-page accounting field in this self-hosted run; it is not Firecrawl cloud credit spend.
`Expected=yes` marks known-good failures such as `fast` mode refusing OCR-required PDFs.
