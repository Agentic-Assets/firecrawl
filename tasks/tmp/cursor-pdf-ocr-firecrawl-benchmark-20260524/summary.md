# Firecrawl PDF OCR Benchmark

## Recommended Mode

| PDF | Mode | Profile | Why |
| --- | --- | --- | --- |
| journal-ree-early-termination-small-loans-multifamily-mortgage-68095636-7a952f5a | ocr | research-page-aware | manual_review: Only one parser mode produced markdown. |
| journal-rfs-preplay-communication-participation-restrictions-effic-1a829cd3-62f41f61 | ocr | research-page-aware | manual_review: Only one parser mode produced markdown. |

## Raw Results

| PDF | Mode | Profile | Exit | Expected | Seconds | Markdown | Boundary | Pages | Tables | Docling Tables | Boilerplate | OCR Quality | Warnings |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| journal-ree-early-termination-small-loans-multifamily-mortgage-68095636-7a952f5a | ocr | research-page-aware | 0 |  | 14.27 | 14194 | docling_markdown_page_break | 5 | 0 | 0 | 0 | ok | abstract_not_detected_early |
| journal-rfs-preplay-communication-participation-restrictions-effic-1a829cd3-62f41f61 | ocr | research-page-aware | 0 |  | 12.16 | 11604 | docling_markdown_page_break | 5 | 0 | 0 | 0 | ok | abstract_not_detected_early |

Note: `creditsUsed` is Firecrawl's local per-page accounting field in this self-hosted run; it is not Firecrawl cloud credit spend.
`Expected=yes` marks known-good failures such as `fast` mode refusing OCR-required PDFs.
