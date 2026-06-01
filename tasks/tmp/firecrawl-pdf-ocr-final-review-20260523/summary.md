# Firecrawl PDF OCR Benchmark

## Recommended Mode

| PDF | Mode | Profile | Why |
| --- | --- | --- | --- |
| ssrn-fen-6255160-ai-driven-credit-intelligence-human-loop-d55489f2-1ce8edd5 | ocr | research-page-aware | Only ocr produced markdown; failed modes: fast. |

## Raw Results

| PDF | Mode | Profile | Exit | Expected | Seconds | Markdown | Page Breaks | Pages | Tables | Figures | Warnings |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ssrn-fen-6255160-ai-driven-credit-intelligence-human-loop-d55489f2-1ce8edd5 | fast | none | 1 | yes | 0.13 | 0 | 0 | None | 0 | 0 | abstract_not_detected_early |
| ssrn-fen-6255160-ai-driven-credit-intelligence-human-loop-d55489f2-1ce8edd5 | ocr | research-page-aware | 0 |  | 4.26 | 4171 | 1 | 2 | 0 | 0 |  |

Note: `creditsUsed` is Firecrawl's local per-page accounting field in this self-hosted run; it is not Firecrawl cloud credit spend.
`Expected=yes` marks known-good failures such as `fast` mode refusing OCR-required PDFs.
