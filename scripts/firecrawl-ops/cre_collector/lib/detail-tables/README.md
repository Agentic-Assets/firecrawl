# Detail table observations

`captureDetailTables(html, {sourceUrl, observedAt})` returns ordered native HTML
rows and cells for caller-supplied detail HTML. It preserves DOM-decoded raw cell text and a trimmed display text,
header/cell tags, span attributes, captions, parent-table relationships and
explicit visibility hints, without interpreting values or declaring them public.
Nested tables are separate observations; their text is excluded from the parent
cell. No HTML, script, or network access is retained or executed. Source URL and
UTC timestamp are caller assertions, not identity or publication proof. The
SHA-256 binds the UTF-8 encoding of the supplied HTML string, not HTTP bytes.
Failed observations use a null hash.

Input, table, per-table and total row, per-row and total cell, text, context,
total character and serialized JSON byte bounds are explicit. If JSON escaping
would exceed the byte limit, whole cells or rows are removed from the end and
reported as omissions.
The omission counters report cut-off counts and truncated UTF-16 code units.
Truncation does not split a UTF-16 surrogate pair. Counts within omitted tables
or rows are unknown and flagged as such; an
oversized input is rejected whole with unknown omissions. Empty, invalid and
parse-error outcomes are explicit.

The shared Buildout detail enricher records complete observed tables under
`extraFacts.detail_tables`; empty, failed or bounded partial attempts use
`detail_tables_attempt` so an ordinary extra-facts merge preserves the previous
complete snapshot. These keys are latest snapshots, not an append-only history.
`runEnrichGroups` restores validated caller-owned snapshots after generic pruning
so false flags, null attributes and positional empty cells survive. Other source
families are not enabled by this integration.

When the scraped document carries a recorded detail observation, its original
clock is reused. Otherwise the origin field explicitly identifies the collector's
result-read time. Neither clock establishes source freshness or publisher identity.

`preserveDetailTables(snapshot)` validates the bounded observation shape and
provenance before a caller restores its own snapshot after generic listing
pruning. It is not an authenticity check: never use it to bless externally
supplied data. It preserves empty text, nulls, false flags and span literals.
