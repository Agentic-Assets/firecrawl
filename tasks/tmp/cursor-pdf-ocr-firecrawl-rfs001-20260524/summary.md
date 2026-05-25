# Firecrawl PDF OCR Benchmark

## Recommended Mode

| PDF | Mode | Profile | Why |
| --- | --- | --- | --- |
| Biased_by_Choice_How_Financial_Constraints_Can_Reduce_Financ_10.1093_rfs_hhab073-28736cc2 | ocr | research-page-aware | manual_review: Only one parser mode produced markdown. |

## Raw Results

| PDF | Mode | Profile | Exit | Expected | Seconds | Markdown | Boundary | Pages | Tables | Docling Tables | Boilerplate | OCR Quality | Warnings |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| Biased_by_Choice_How_Financial_Constraints_Can_Reduce_Financ_10.1093_rfs_hhab073-28736cc2 | ocr | research-page-aware | 0 |  | 24.43 | 30767 | docling_markdown_page_break | 10 | 0 | 0 | 0 | ok | abstract_not_detected_early |

Note: `creditsUsed` is Firecrawl's local per-page accounting field in this self-hosted run; it is not Firecrawl cloud credit spend.
`Expected=yes` marks known-good failures such as `fast` mode refusing OCR-required PDFs.
