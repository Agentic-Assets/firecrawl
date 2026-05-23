# Blocked Or Low-Value Sources

This sprint did not bypass robots.txt, login walls, paywalls, CAPTCHA, IP blocks, or access controls.

## Fresh Run Findings

| Source | Status | Reason | Compliant alternative |
|---|---|---|---|
| CoStar | `robots_disallowed` | Marketplace/proprietary listing platform; prior repo matrix also flagged Akamai/EdgeSuite access denial. | Licensed CoStar feed/export, partnership, manual review, broker flyers, county records, issuer disclosures. |
| LoopNet | `robots_disallowed` | Marketplace/proprietary listing platform; prior repo matrix also flagged Akamai/EdgeSuite access denial. | Licensed LoopNet/CoStar access, public broker pages, county records, CRE news, manual review. |
| CBRE tested quarterly figures URL | `robots_disallowed` | Generic robots check disallowed this tested path. | Use allowed CBRE public pages only, official downloads where terms allow, or manual review. |
| Colliers research/capital-markets tested URLs | `robots_disallowed` | Generic robots check disallowed tested paths. | Use public pages allowed by robots, licensed feeds, or manual review. |
| SEC submissions JSON via generic Firecrawl probe | `robots_disallowed` | SEC fair-access rules and robots require specific access handling; generic scraping is not the right workflow. | Use SEC APIs/downloads with proper user agent, rate limits, and fair-access compliance. |
| Realty Income press release tested URL | `robots_disallowed` | Generic robots check disallowed the tested path. | Use issuer-approved feeds, IR downloads, SEC filings, or manual review. |
| Prologis property search | `robots_disallowed` | Property-search path blocked by robots. | Use public property pages allowed by robots, licensed owner data, broker pages, or manual research. |
| NYCEDC document library / direct PDF candidate | `robots_disallowed` | Robots disallowed the tested page/PDF candidate. | Use NYC Open Data, official downloadable datasets where allowed, or manual review. |
| NAIOP research page | `robots_disallowed` | Tested path disallowed by robots. | Use allowed public summaries, licensed/member access, or manual review. |
| Census construction page via generic scrape | `robots_disallowed` | Generic Firecrawl page scrape stopped at robots. | Use Census official APIs/downloads instead of page scraping. |
| GlobeSt | `robots_disallowed` | Tested public news page disallowed by robots. | Use licensed news, RSS/API if offered, press releases, Bisnow/public alternatives. |

## Partial Public Sources

Partial public sources may still be valuable, but only for visible public content:

- Cushman MarketBeat
- JLL insights
- REIT.com market data
- Harris County tax sales
- FRED
- Bisnow

Do not infer that partial public access permits bulk extraction of gated PDFs, subscriber-only articles, or account-only data.

## Common Failure Modes

- Robots disallowance.
- Login/newsletter prompts mixed into otherwise public pages.
- Dynamic listing/search UIs with limited static text.
- PDF table structure loss after markdown parse.
- Government/public-record systems requiring source-specific forms, APIs, or query parameters.
- Proprietary marketplaces where the compliant answer is a license, not a scraper.

