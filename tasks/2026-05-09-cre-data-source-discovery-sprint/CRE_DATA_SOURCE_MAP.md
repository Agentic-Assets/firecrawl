# CRE Data Source Map

Run date: 2026-05-09  
Local API: `http://localhost:3002`  
Model profile: OpenRouter `deepseek/deepseek-v4-flash` primary, `deepseek/deepseek-v4-pro` escalation.

## Product Purpose

The useful product is a decision-support and property-finding system, not a scraper showcase. The pipeline should help an investor:

- Find properties that are explicitly for sale, for lease, in auction, in foreclosure, in tax sale, lender-owned, or otherwise likely to transact.
- Identify potential off-market targets from owner/operator portfolios, permits, zoning changes, distress records, financing clues, tenant movement, and public-company disclosures.
- Gather decision evidence for a specific property: public-record context, ownership clues, manager/operator clues, broker activity, financing/debt hints, tenant/lease movement, market comps/context, and cited PDF/report snippets.
- Separate actionable property-level signals from general market commentary.

## Sprint Coverage

Canonical row-level evidence is in `SOURCE_TEST_RESULTS.csv`.

| Metric | Result |
|---|---:|
| Representative sources tested | 31 |
| Requested source categories covered | 8 of 8 |
| Approved public sources | 13 |
| Partial public sources | 6 |
| Robots-disallowed sources | 12 |
| Current compliant sample extracts | 9 |

## Highest-Value Source Types

1. **Distress and public-sale records**: Harris County tax sales, Dallas County foreclosure notices, FDIC asset sales, Omni case pages. These directly surface distress, forced-sale, lender-owned, and bankruptcy signals.
2. **Broker PDFs and research pages**: Newmark PDF, JLL insights, Cushman MarketBeat, Marcus & Millichap research. These provide market context, asset-class language, rent/vacancy/tenant signals, and PDF citations.
3. **Capital-markets and brokerage service pages**: JLL capital markets and similar pages expose broker activity, investment-sales/debt-equity focus areas, and linked transaction content.
4. **REIT investor relations and public-company pages**: Simon IR and REIT.com public pages provide portfolio, occupancy, capital-market, issuer, and disclosure context. Prefer issuer pages and SEC APIs/downloads where robots permit.
5. **Owner/operator/developer sites**: Hines and Brookfield public pages provide portfolio/market/tenant clues, but they are higher-level and need cross-source confirmation.
6. **County and municipal public-record pages**: assessor, permit, zoning, planning, and auction surfaces are valuable but usually need source-specific APIs, forms, or manual review for property-level depth.
7. **CRE news/category pages**: Bisnow produced useful public article/link signals; use as a lead-generation layer, not as authoritative property records.
8. **Blocked listing marketplaces**: CoStar and LoopNet are low-value for compliant local scraping because robots/access controls stop automation. Use licensed feeds, partnerships, or manual review.

## Decision-Support Signal Map

| Investor question | Best source type | Tested examples | Fields/signals to extract |
|---|---|---|---|
| What is actually for sale or likely to sell? | Auctions, tax sales, foreclosure notices, broker pages, FDIC asset sales | Harris, Dallas, Orange County, FDIC, JLL/Marcus | `listing_status`, `distress_signal`, `disposition_signal`, `asking_price`, `sale_price`, `broker`, `source_url` |
| Who owns or controls the property? | Assessor/recorder/public records, owner/operator sites, REIT IR | Cook assessor, Hines, Brookfield, Simon | `owner`, `manager`, `property_name`, `address`, `source_type`, `confidence_score` |
| Is there financial stress or a forced-sale catalyst? | Foreclosure, tax delinquency, bankruptcy, lender-owned, debt/CMBS language | Dallas, Harris, Omni, FDIC, JLL capital markets | `distress_signal`, `lender`, `debt_maturity`, `extracted_text_snippet` |
| Does the property/market look attractive? | Broker PDFs, market reports, FRED/Census/REIT data | Newmark PDF, Cushman, JLL, FRED, REIT.com | `asset_type`, `occupancy`, `cap_rate`, `NOI`, market snippets, document URL |
| Are tenants expanding, contracting, or rolling? | CRE news, broker pages, owner/operator pages, public-company disclosures | Bisnow, JLL, Brookfield, Simon | `tenant`, `lease_expiration`, `occupancy`, `disposition_signal` |
| Is there redevelopment or land-use upside? | Permits, zoning, planning, auctions, public economic docs | NYC DOB, LA zoning, Orange County | `asset_type`, `address`, zoning/permit snippets, compliance notes |

## Tested Category Summary

| Requested category | Tested examples | Result | CRE origination value |
|---|---|---|---|
| Brokerage listing/research pages | CBRE, Cushman, JLL, Colliers, Marcus & Millichap | Mixed: robots blocks plus partial/approved public pages | Strong market/deal context; good for sector and broker signal discovery. |
| REIT/public-company filings, supplements, presentations, press releases | SEC Prologis JSON, Realty Income PR, Simon IR, REIT.com | Mixed; Simon approved, REIT.com partial, SEC/Realty robots-disallowed by generic probe | High if routed through official SEC/company APIs and public IR PDFs. |
| County assessor, recorder, tax, permit, zoning, planning, auction | Cook assessor, NYC DOB, LA zoning, Orange County auctions | Approved public pages | Strong for ownership clues, permit/zoning context, and auction triggers; needs source-specific parsers. |
| Foreclosure, bankruptcy, receivership, tax delinquency, lender-owned | Harris tax sales, Dallas foreclosure, FDIC asset sales, Omni cases | Approved/partial public | Highest direct distress value. |
| Owner/operator, developer, property manager, tenant websites | Prologis property search, Hines, Brookfield | Mixed; Hines/Brookfield approved, Prologis robots-disallowed | Useful for portfolio and tenant movement, but confirm with records/news. |
| Market reports, OM samples, broker PDFs, newsletters, research libraries | NYCEDC, Newmark PDF, NAIOP, Census, FRED | Mixed; Newmark PDF approved and parsed, FRED partial, several robots-disallowed | High for PDF-based market/deal intelligence; table QA required. |
| News, press releases, sale-leaseback, tenant movement | GlobeSt, Bisnow | Mixed; Bisnow partial, GlobeSt robots-disallowed | Good lead feed where public; avoid gated articles. |
| Industry directories and capital-markets pages | JLL capital markets, Colliers capital markets | Mixed; JLL approved, Colliers robots-disallowed | Good for broker/service coverage and transaction team mapping. |

## Production Source Tiers

| Tier | Source type | Why |
|---|---|---|
| Tier 1 | Distress/public-sale pages, official county/government APIs/downloads, public broker PDFs, public REIT IR PDFs | Direct property/investment signals, lower compliance risk, repeatable ingestion. |
| Tier 2 | Broker research hubs, capital-markets pages, owner/operator pages, CRE news pages | Useful lead/context surfaces; needs dedupe, confidence scoring, and citation review. |
| Tier 3 | Public teasers from gated research/news/listing sites | Only metadata/headline-level extraction unless terms/license permit more. |
| Do not scrape | CAPTCHA/paywall/login/robots-blocked marketplaces and proprietary data platforms | Use licensed data, API partnership, whitelisted access, or manual review. |
