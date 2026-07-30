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

## Governed Buildout Restoration

All 25 historical single-token public feeds from commit `6245a7144` have been
ported into the current collector:

- `faris-lee`
- `fortis-net-lease`
- `unique-properties`
- `kiser-group`
- `pinnacle-rea`
- `cawley-chicago`
- `bradford-allen`
- `hudson-peters`
- `gibson-commercial`
- `leibsohn`
- `nai-hiffman`
- `nai-martens`
- `bull-realty`
- `tri-commercial`
- `berger-commercial`
- `nai-bergman`
- `nai-isaac`
- `trinity-partners`
- `metro-commercial`
- `33-realty`
- `nai-hallmark`
- `nai-plotkin`
- `greysteel`
- `nai-talcor`
- `nai-dominion`

The recovered company names, public plugin identifiers, and listings pages live
in `sources/buildout-registry.ts`. They route through the current shared
Buildout adapter with stable ordering, complete-page reconciliation,
single-source cache namespaces, optional validated detail coordinates,
targeted-detail enrichment, child preservation, and the existing sale/lease
identity collapse. Registry, SQL-seed, strict-freshness, cache-policy, detail,
and identity parity are covered offline.

This is code admission, not production-freshness proof. Run each source as a
bounded live canary, inspect its exact artifact and source totals, and require a
guarded database readback before lifecycle reconciliation or a freshness claim.

## Write Risk

`cre_ingest_rest.py` can delete and replace active brokerage rows. It must not be used until current-main mark-missing, source completeness, and dry-run gates are ported into it.
