# Stace June20 Recovery Notes

## Verdict

Use `origin/stace-june20` as source research and adapter source code only. Do not merge it wholesale.

## Confirmed Useful Adapters

- Matthews: sitemap enumeration plus throttled plain fetch.
- Franklin Street: dual Buildout plugin tokens.
- SRS: open Cloud Run search API.
- Hanley: embedded `rethink_properties` JSON. Unit covered, but live access on 2026-06-21 returned Cloudflare 403 for direct fetch, WordPress REST, and Firecrawl raw fallback.
- Kidder Mathews: open public listing API.

## Live Supabase Read-Only Check

Checked project `fhqycqubkkrdgzswccwd` on 2026-06-21 with SELECT-only queries. The live database already contains data for the branch-added brokerages:

- Matthews: 3,563 active listings.
- Franklin Street: 413 active listings.
- SRS: 2,122 active listings.
- Hanley: 102 active listings.
- Kidder Mathews: 3,108 active listings.

This means the stale branch was operationally useful even though its code never landed cleanly. Current `main` was behind the live data model for these sources.

## Local Live Smoke Checks

- `srs/sale --max-items 1`: passed, source total 2,122, collected Carson, CA sale listing.
- `kidder-mathews/sale --max-items 1`: passed, source total 3,107, collected Calimesa, CA sale listing.
- `hanley/sale --max-items 1`: blocked on 2026-06-21 by Cloudflare 403 and Firecrawl raw scrape failure.

## Research To Preserve

- Buildout firm token list and discovery workflow.
- Top-30 feasibility notes.
- Voit LoopLink and CoStar dead-end warning.
- Generic sitemap plus LLM extraction design.

## Write Risk

`cre_ingest_rest.py` can delete and replace active brokerage rows. It must not be used until current-main mark-missing, source completeness, and dry-run gates are ported into it.
