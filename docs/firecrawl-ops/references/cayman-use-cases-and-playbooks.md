# Cayman Firecrawl Use Cases and Playbooks

This reference maps directly to your latest comments and decisions.

## 1) Full PDF + text extraction for working papers
Yes. Local Firecrawl `scrape` can pull full-text from PDFs and return markdown text extraction.

Recommended pattern:
- Persist both `pdf_url` and extracted text
- Keep `extraction_quality` fields (length, section-hit score)
- Retry hard files with `deepseek/deepseek-v4-pro`

## 2) Full ARES conference metadata extraction
Feasible with a staged approach:
1. `search` for canonical conference pages
2. `map` conference site/event pages
3. `scrape` all relevant pages and PDFs
4. Parse sessions/speakers/times/rooms into structured table

If program details are inside login-only systems, combine public pages + downloadable PDFs + manual seed URLs.

## 3) Corbis DB enhancement without duplication
Do incremental enrichment only:
- Join on stable identifiers (DOI, OpenAlex ID, SSRN/ArXiv URL)
- Upsert only missing/low-quality fields
- Keep provenance (`source_url`, `scraped_at`, `confidence`)

## 4) OpenData table enrichment
High-value additions:
- `api_endpoint`
- `auth_type`
- `format` (csv/json/parquet/pdf/html)
- `update_frequency`
- `coverage_geo`
- `coverage_time`
- `license`
- `access_method`
- `last_verified_at`

## 5-6) CRE platform accessibility + privacy/VPN
Observed reality: some major CRE platforms are bot-protected/blocking.
Use a probe-first approach and only pursue sources that are accessible and policy-compliant.

Privacy architecture:
- Route traffic through managed VPN egress by default
- Keep fixed egress region where possible
- Log egress + source + timestamp for auditability
- Respect site terms and robots/legal constraints

## 7-8) EDGAR already exists; extend to fund/manager intelligence
Focus on complement, not replacement:
- Manager websites, investor presentations, portfolio pages
- Fund strategy pages and holdings summaries
- Normalize manager/fund entities and link back to EDGAR records

## 9) Use Firecrawl only where MCP tools are weaker
Decision rule:
- Use Exa/Context7 for docs/search when they already solve the task well
- Use Firecrawl for deep extraction, multi-page crawling, and custom structured ingestion workflows

## 10-12) Value proposition vs GitHub CLI/MCPs
Firecrawl differentiates on:
- Multi-page crawl orchestration
- Unified extraction from arbitrary web/PDF sources
- Consistent markdown/JSON normalization
- Batch automation and recurrent ingestion patterns

GitHub CLI/MCP tools remain best for first-party GitHub operations and doc-native retrieval.

## Recommended prebuilt utilities
- `bulk_triage_runner.py` -> tiered escalation at scale
- `crawl_swarm.py` -> parallel discovery + scrape batches
- `platform_access_probe.py` -> quickly test accessible vs blocked sources
