---
name: cre-source-investigation
description: Diagnose stale, failed, or incomplete commercial real estate brokerage feeds by comparing the collector with current source websites and stored listing data. Use when investigating source coverage, changed URLs, extraction failures, or missing listing fields.
---

# CRE source investigation

Follow the [brokerage completion playbook](../../../docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md#investigate-a-failed-or-incomplete-source) and the [collector guidance](../../../scripts/firecrawl-ops/cre_collector/AGENTS.md). Start with the current run, the source adapter, and its Linear issue; distinguish collection, admission, database writes, and readback.

Check whether the configured URL, provider, operation, or listing location still matches the brokerage's current public navigation. Inspect representative source pages and errors, including redirects and response bodies when available. Use the `agent-browser` skill, Firecrawl plugin or local API, and ordinary direct requests when available and useful; compare their actual results rather than assuming one succeeded or a page is gone. Respect provider pacing and access restrictions.

Trace identifiable records and useful fields, including addresses, coordinates, price/rate terms, spaces, contacts, and document/image URLs, from source through extraction and, when available, Supabase readback. Preserve raw observations and contradictions, distinguish source facts from supported inference, and keep incomplete identified records moving through a tested partial path. A partial sample does not establish full coverage or authorize retiring absent listings.

Investigate a redirect or one failed detail as a record-local problem when its
settled outcome permits; keep eligible peers moving and retain the failed
record's evidence. Preserve earlier listing details and child records when new
observations omit or replace them. After a complete, source-appropriate
inventory, record an absent previously seen identity as a disappearance and a
later sighting as a reappearance, without guessing a sale or lease. Partial or
ambiguous coverage cannot prove absence. These remain implementation goals, not
proof that current schedules record every event or preserve every historical
version: see [AGENTIC-5337](https://linear.app/agenticassets/issue/AGENTIC-5337)
and [AGENTIC-5255](https://linear.app/agenticassets/issue/AGENTIC-5255).

Record attempted URLs, methods, outcomes, limitations, and the next concrete fix in the owning Linear issue. Tell the user which feeds actually wrote and passed readback, which failed or are pending, what each tool revealed, and whether the proposed fix is merged, deployed, or still unverified. Do not report a site sample or an enabled schedule as proof of database ingestion.
