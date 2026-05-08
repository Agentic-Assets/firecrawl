# CRE Platform Access Matrix

**Last tested:** 2026-03-09

This reference documents which CRE (Commercial Real Estate) platforms are accessible via Firecrawl scraping without login or bot protection issues.

## Quick Reference

| Status | Meaning |
|--------|---------|
| 🟢 Accessible | Full content extractable, no blocks |
| 🟡 Partial | Teaser/summary available, full content gated |
| 🔴 Blocked | Bot protection blocks scraping |

---

## 🟢 Fully Accessible Sources

### Brokerage Research Reports

| Source | Content Type | Volume | Best For |
|--------|--------------|--------|----------|
| **CBRE Insights** | Quarterly sector figures | ~11K chars | Cap rates, vacancy, absorption, investment volume by sector (office, industrial, multifamily, retail, hotel, life sciences, medical outpatient, net lease) |
| **Cushman Wakefield** | MarketBeats | ~8-11K chars | Metro-specific market reports, Tulsa and other markets |
| **Colliers** | Research pages | ~11K chars | Market insights, property reports |
| **Savills** | Research | ~15K chars | Global CRE research, market reports |
| **JLL** | Research pages | ~7-8K chars | Market research, sector reports |
| **Marcus & Millichap** | Report teasers | ~27K chars | Investment forecasts, submarket dynamics (full PDFs gated) |

### Industry Publications

| Source | Content Type | Volume | Best For |
|--------|--------------|--------|----------|
| **GlobeSt.com** | News + analysis | ~31K chars | CRE news, trending stories, sector coverage, deal announcements |
| **CommercialCafe** | Listings + research | ~16K chars | Market reports, property search |
| **Crexi** | Listings + blog | ~1-13K chars | Property listings, market insights |

### Government & Academic Data

| Source | Content Type | Volume | Best For |
|--------|--------------|--------|----------|
| **Census.gov/construction** | Government data | ~126K chars | Building permits, housing starts, construction statistics |
| **FRED (St. Louis Fed)** | Economic data | ~5K chars per series | Economic time series, unlimited API access |
| **BIS.org** | Central bank research | Mixed (some gated) | BIS papers, global financial data |

### REIT Data

| Source | Content Type | Volume | Best For |
|--------|--------------|--------|----------|
| **REIT.com** | REIT data | ~8K chars | Index returns, sector breakdowns, market cap data |
| **NAREIT** | Industry statistics | ~7-8K chars | REIT performance data, industry metrics |

---

## 🟡 Partial Access (Teasers / Login-Gated)

| Source | What's Free | What's Gated |
|--------|--------------|--------------|
| **Marcus & Millichap** | Report summaries, submarket dynamics, headlines | Full PDF reports, detailed data tables |
| **NREI Online** | Headlines only | Full articles require login |
| **PI Executive** | Headlines | Full content gated |
| **PREA** | Some headlines | Research reports gated |
| **Reonomy** | Landing pages | Property data requires login |
| **ULI (Urban Land Institute)** | Event info, summaries | Full research gated |

---

## 🔴 Blocked Sources

| Source | Block Type | Notes |
|--------|------------|-------|
| **CoStar** | Akamai/EdgeSuite | "Access Denied" — requires browser session |
| **LoopNet** | Akamai/EdgeSuite | Same blocking as CoStar |
| **Knight Frank** | Cloudflare-style | Bot detection blocks scrapers |
| **Reuters Real Estate** | 401 Unauthorized | Requires auth |
| **WSJ Real Estate** | Paywall | Limited/no access |
| **Bloomberg Real Estate** | Paywall | ~900 chars only |

---

## Recommended Workflows

### For Quarterly Market Metrics
```
CBRE Quarterly Figures → Full executive summaries with hard numbers
URL pattern: https://www.cbre.com/insights/figures/q4-2025-us-<sector>-figures
```

### For Metro-Specific Reports
```
Cushman Wakefield MarketBeats → Tulsa and other markets
URL pattern: https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/<metro>-marketbeat
```

### For CRE News
```
GlobeSt → Full articles, deal announcements
URL: https://www.globest.com/
```

### For REIT Data
```
REIT.com → Index returns, sector breakdowns
URL: https://www.reit.com/
```

### For Economic Context
```
FRED → Economic time series
URL: https://fred.stlouisfed.org/
Census Construction → Building permits, housing starts
URL: https://www.census.gov/construction/
```

---

## Testing Script

Use `scripts/cre_access_matrix.py` to test accessibility of CRE sources:

```bash
python3 scripts/cre_access_matrix.py --sources all
python3 scripts/cre_access_matrix.py --sources news --output results.json
```

Output includes: status (accessible/blocked/login-gated), markdown length, and sample snippet.

---

## Notes

1. **Volume varies by page**: Homepage listings typically return more content than deep article pages
2. **Login-gated sources**: May still be useful for headlines/teasers; full content requires browser automation
3. **Government sources**: Generally fully accessible with no restrictions
4. **REIT sources**: Good for index-level data; company-specific data may require SEC filings

---

## Changelog

| Date | Change |
|------|--------|
| 2026-03-09 | Initial access matrix created from platform probe testing |