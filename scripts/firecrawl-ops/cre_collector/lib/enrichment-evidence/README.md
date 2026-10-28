Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Enrichment evidence

The optional enrichment evidence channel preserves each claim separately,
including repeated URLs and conflicting transaction assertions. The client
capture clock is observed after `scrapeDoc` returns; provider freshness, cache
state, listing identity and visibility remain unverified. Scrape failures carry
only bounded failure metadata, with no invented document or provider clock.
The page-context `detailObservedAt` and table timestamps, when present, carry
that same collector receive clock, never a provider modification timestamp or
evidence of a fresh fetch. `pageContextClockOrigin` records this explicitly.

The shared page-context helper retains bounded source-native scripts, metadata,
links, images and table observations. HTML above 2,000,000 UTF-8 bytes is not
parsed for context. Each supplied JSON-serializable `ScrapedDoc` field is kept
exactly as its JSON representation up to 256 KiB, with explicit omission
reasons for larger or non-JSON fields. Nonfinite numbers, negative zero,
undefined values, functions, symbols, sparse arrays, nonplain objects and
other values JSON would transform or omit are not silently converted. A single observation is capped
at 2,500,000 bytes; if larger, its context and fields are omitted explicitly.
Groups cap observations at 10,000 and 8 MiB, retaining the bounded prefix and
marking `evidenceIncomplete` with an `evidenceError` on the source result.
This limit does not discard independently mapped listing rows. Generic JSON-LD
mapping and URL completion behavior remain separate. This is collector-supplied
content, not raw HTTP bytes or proof of complete extraction or publisher access.
Existing content and credential admission remains mandatory.

This draft is unverified under the active test and service hold; it is not
deployment or database-write evidence.

Targeted enrichment artifacts remain private local custody. The enrichment
worker does not automatically append this observation channel to the evidence
database. Observations alone never complete a canonical queue claim; absent
canonical output keeps the worker's existing retry and dead-letter accounting.
