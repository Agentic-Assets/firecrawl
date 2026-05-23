# CRE Platform Access Matrix

This is a starting matrix for Commercial Real Estate sources, not a permanent truth table. Access rules, bot protection, paywalls, and page structure change often. Refresh with `scripts/firecrawl-ops/cre_access_matrix.py` before using the results for a serious workflow.

## Refresh

```bash
python3 scripts/firecrawl-ops/cre_access_matrix.py --sources all --output results/cre-access-matrix.json
python3 scripts/firecrawl-ops/platform_access_probe.py --url https://www.cbre.com/insights
```

The scripts use the local v2 scrape endpoint at `http://localhost:3002/v2/scrape`.

## Status Meanings

| Status | Meaning |
|---|---|
| `accessible` | Substantial markdown returned without obvious block/login text |
| `partial` | Useful teaser/summary content returned, but full detail may be gated |
| `login-gated` | Login or subscription flow appears before useful content |
| `blocked` | Bot protection or access-denied page detected |
| `minimal` / `empty` / `error` | Not enough content to rely on without manual follow-up |

## Historically Useful Sources

These have been useful in prior local probes and are good first-pass candidates:

- CBRE Insights
- Cushman & Wakefield MarketBeats
- Colliers research pages
- Savills research pages
- JLL research pages
- GlobeSt.com
- CommercialCafe
- FRED
- Census construction data
- REIT.com / NAREIT

## Historically Difficult Sources

Treat these as likely requiring manual access, authenticated browser flows, or another data source:

- CoStar
- LoopNet
- Reuters / WSJ / Bloomberg real-estate pages
- paid research portals such as PREA or Green Street
- property-data products with login walls such as Reonomy

## Recommended Workflow

1. Run `cre_access_matrix.py` against the current source set.
2. Keep URLs with `accessible` or `partial` status.
3. Save the output JSON with the run date.
4. Use `v2/map` or targeted `v2/scrape` on the promising domains.
5. Escalate low-content or blocked sources only if the user has a legitimate authenticated route.

## Notes

- Homepage and index pages often scrape better than deep article pages.
- Government and public-data sources are usually more stable than commercial platforms.
- Login prompts do not always mean zero value; teaser pages can still provide titles, dates, and source leads.
- Verify important numbers against the source page or downloaded document before using them in a final report.
