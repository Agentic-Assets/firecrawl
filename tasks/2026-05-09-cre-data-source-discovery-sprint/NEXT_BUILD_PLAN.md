# Next Build Plan

## MVP Objective

Build a compliant CRE investment-decision and property-finding pipeline that turns public web pages, public records, public PDFs, and licensed/official feeds into cited prospecting leads and property decision packets.

The first useful product should answer:

- Which properties are for sale, in auction, in foreclosure, tax-delinquent, lender-owned, or otherwise likely to transact?
- Who appears to own, manage, broker, lend against, or occupy the property?
- What public-record, financing, tenant, market, zoning, permit, and PDF evidence matters for an investment decision?
- What is the confidence level, source, and compliance status for every extracted claim?

## Recommended Ingestion Pipeline

1. **Source registry**
   - Store domain, category, robots URL, terms status, allowed workflows, refresh cadence, and owner.
   - Seed from `SOURCE_TEST_RESULTS.csv`.

2. **Probe and compliance gate**
   - Run a lightweight `v2/scrape` probe and robots check before extraction.
   - Stop on robots disallow, CAPTCHA, login wall, paywall, 401/403, repeated 429, or proprietary marketplace blocks.

3. **Discovery**
   - Use `v2/search` for targeted source discovery.
   - Use `v2/map` for approved hubs.
   - Use small conservative crawl limits first.

4. **Extraction**
   - Use `v2/scrape` for page text and links.
   - Use `v2/batch/scrape` for approved URL sets.
   - Use `v2/parse` for allowed PDFs and office documents.
   - Use `v2/extract` with explicit schemas and DeepSeek V4 Flash.
   - Escalate only failed/high-value items to DeepSeek V4 Pro.

5. **Normalization**
   - Normalize to `SAMPLE_SCHEMA.json`.
   - Add entity tables for property, organization, contact-minimized actor, source document, signal, and evidence span.
   - Preserve raw source hash, crawl date, URL, and compliance notes.

6. **Signal scoring**
   - Score signals by source quality, field confidence, recency, distress/disposition language, and cross-source corroboration.
   - Downgrade homepage/category snippets and table-derived PDF numbers until QA passes.
   - Prioritize property-level sale/distress/public-record signals over broad market commentary.

7. **Human review**
   - Queue high-value but low-confidence signals for analyst review.
   - Require citations before showing deal leads in the prospecting UI.

8. **Property decision packet**
   - Aggregate all evidence for a property or owner into a single cited packet.
   - Include source excerpts for ownership, sale/distress status, broker activity, debt/financing hints, tenant movement, public-record context, market context, and open diligence questions.

## First Production Connectors

1. Dallas County foreclosure notices.
2. Harris County tax sales.
3. FDIC asset sales.
4. Omni case pages.
5. Newmark/JLL/Cushman public broker research and PDFs.
6. Simon/public REIT IR pages and approved issuer PDFs.
7. Bisnow/public CRE news pages where allowed.
8. FRED/Census/SEC through official APIs/downloads, not generic page scraping.

## Data Model Slices

- `sources`: URL/domain, source type, compliance status, refresh cadence.
- `documents`: source URL, document URL, title, content hash, fetched at, parser, raw path.
- `entities`: property, owner/operator, broker, lender, tenant, issuer.
- `signals`: distress, disposition, tenant movement, financing, portfolio change, market report.
- `evidence_spans`: snippet, source URL, document URL, confidence, extraction model.
- `reviews`: analyst decision, notes, false-positive reason.
- `decision_packets`: property or owner target, ranked evidence, missing diligence items, analyst disposition.

## Verification Gates

- Unit: schema validation for every extract.
- Integration: local Firecrawl scrape/search/map/batch/crawl/extract/parse smoke checks.
- Compliance: robots and block-classification tests for every source.
- Quality: random sample review for PDF tables and public-record fields.
- Product: every surfaced lead must include a source URL, snippet, confidence score, and compliance note.

## Build Sequence

1. Convert `run_cre_discovery_sprint.py` into a reusable source-registry runner.
2. Add JSON-schema validation against `SAMPLE_SCHEMA.json`.
3. Implement source-specific adapters for the top four distress/public-record sources.
4. Add PDF discovery and parse pipeline with table-QA flags.
5. Persist normalized records and evidence spans.
6. Build a review queue for high-value signals.
7. Add scheduled refresh with conservative per-domain rate limits and cache/dedupe.
