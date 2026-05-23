# Firecrawl CRE Extraction Playbook

This playbook reflects the final local run on 2026-05-09 with `MODEL_NAME=deepseek/deepseek-v4-flash`.

## Compliance Gate

Run every source through a probe gate before extracting:

1. Check `robots.txt`.
2. Stop on 401, 403, CAPTCHA, paywall, login wall, repeated 429, or bot-protection text.
3. Use public pages only; do not use credentials, cookies, CAPTCHA solving, proxy rotation, or anti-bot evasion.
4. For public records, minimize PII and prefer organization/property-level fields.
5. Prefer official APIs/downloads when available.
6. Store `source_url`, `crawl_date`, `robots_url`, `access_status`, `confidence_score`, and `compliance_notes`.

## Verified Firecrawl Methods

Evidence: `method_test_results.json`.

| Method | Status | Best CRE use |
|---|---:|---|
| `POST /v2/scrape` with `formats:["markdown","links"]` | Passed | One-page public text, links, report pages, source probes. |
| `POST /v2/search` with `scrapeOptions` | Passed | Targeted source discovery such as broker PDF/report queries. |
| `POST /v2/map` | Passed | Link discovery on permissive hubs; JLL insights returned 5 links. |
| `POST /v2/batch/scrape` | Passed | Batch public pages after probe approval. |
| `POST /v2/crawl` | Passed | Small conservative crawls on allowed public domains. |
| `POST /v2/extract` with explicit schema | Passed | Structured extraction from approved pages; verified with DeepSeek V4 Flash. |
| `POST /v2/parse` multipart PDF | Passed | Local PDF parsing; Newmark PDF returned 23,790 markdown characters. |

## Recommended Workflows

### 0. Property Decision Packet

For a target property or market, build a packet in this order:

1. **For-sale/distress pass**: search and scrape auction, foreclosure, tax sale, FDIC, broker, and public listing pages.
2. **Public-record pass**: assessor, recorder, permit, zoning, tax, planning, and official open-data sources.
3. **Owner/operator pass**: owner, manager, developer, REIT, tenant, and investor-relations pages.
4. **Market/financial context pass**: broker PDFs, REIT supplements, FRED/Census/SEC official data, and public research pages.
5. **News/catalyst pass**: public CRE news, press releases, tenant movement, sale-leaseback, layoffs/expansion, portfolio disposition clues.
6. **Cited extract pass**: normalize to `SAMPLE_SCHEMA.json` and require `source_url`, snippet, confidence, crawl date, and compliance notes before using a signal.

### 1. Public Source Probe

Use `v2/scrape` with markdown and links only. Classify as `approved_public`, `partial_public`, `robots_disallowed`, `login_or_paywall_gated`, `blocked`, `minimal`, or `error`.

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.jll.com/en-us/insights","formats":["markdown","links"],"timeout":45000,"blockAds":true}'
```

### 2. Link Discovery

Use `v2/map` after a source passes the probe. Keep `limit` small during discovery and expand only on approved sources.

```bash
curl -sS -X POST http://localhost:3002/v2/map \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.jll.com/en-us/insights","limit":5}'
```

### 3. Batch Public Pages

Use `v2/batch/scrape` for approved URLs. Do not batch blocked, login-gated, or robots-disallowed URLs.

### 4. Structured Extraction

Use explicit schemas. Prompt-only extraction is less stable and harder to audit.

Best first fields:

- `source_type`
- `asset_types`
- `markets`
- `origination_signals`
- `distress_signal`
- `disposition_signal`
- `broker_or_team`
- `document_url`
- `evidence_snippet`
- `confidence_score`

Model policy:

- Primary: `deepseek/deepseek-v4-flash`.
- Escalate to `deepseek/deepseek-v4-pro` for noisy pages, malformed JSON, low confidence, or high-value records.

### 5. PDF Discovery And Parse

Preferred path:

1. Scrape landing page with `markdown` and `links`.
2. Filter links for `pdf`, `download`, `market-report`, `supplement`, `annual-report`, `offering`, `forecast`.
3. Check robots for the direct PDF URL.
4. Download the PDF only if public and allowed.
5. Parse locally with `v2/parse`.
6. Store both direct PDF URL and discovery page URL.

Important limitation: Firecrawl markdown is good for discovery and citations. Do not treat parsed PDF tables as final numerical truth without a table QA pass.

### 6. Public Record Ingestion

For assessor, recorder, permit, tax sale, foreclosure, and auction sources:

- Prefer official API/CSV/bulk download.
- If only HTML is available, scrape public instructions/index pages first.
- Route detail pages through source-specific parsers.
- Store public-record caveats and avoid collecting unnecessary personal contact fields.

## Reproducible Sprint Command

```bash
python3 tasks/2026-05-09-cre-data-source-discovery-sprint/run_cre_discovery_sprint.py
```
