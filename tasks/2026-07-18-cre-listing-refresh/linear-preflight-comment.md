## 2026-07-18 live refresh preflight

Current Firecrawl `main` is clean at
`1bae23bcc9234d3a9c1731221b5290dc7a604484` and already contains merged PR #22.
The issue description and the July 11 runbook still describe PR #22 as
unmerged, so that part of the prior handoff is stale.

The requested CRE refresh is being run only through the supported
`cre_collector` path, with additive ingestion, status activation off, no
`--mark-missing`, no schema changes, and no Firecrawl OM extraction.

Read-only production baseline: 0 active listings refreshed in the prior 24
hours, 672 in the prior seven days, and 112,479 active listings are older than
seven days. The live monitor is fresh, but the local Firecrawl runtime is down
and queued enrichment has 76 pending rows after repeated empty artifacts.
Runtime recovery and the enrichment failure repair are in progress. This is
not evidence of a completed refresh or a production-stability claim.
