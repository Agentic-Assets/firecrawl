# C10 private receipt substrate

This TypeScript package is a sealed evidence substrate, not a collector or an
executable source registry. It has no source producer implementation and it
does not import `collect.ts`, ingestion, checkpoints, cache helpers, or normal
scrape helpers. Python `candidate_registry()` remains unverified and
`verified_registry()` remains closed.

A future, independently reviewed source module may implement only
`ReceiptProducer.produceEnumerationReceipt()` and
`ReceiptProducer.produceMemberReceipt()`. Its context supplies a source-bound
`SourceBoundOneShotTransport`: producers name a predeclared request-card ID,
never a URL, and each card can be consumed once. The required parser callback
receives a temporary body copy once, returns a canonical immutable projection,
and never returns a reusable raw-body handle. A status, challenge, redirect,
host, byte, time, cache, or attempt-bound failure is terminal with zero retries
and no fallback.

Initial cards are fixed enumeration cards. Native next-page and member cards
must be emitted by a source-specific request-graph factory from a sealed parent
event and parsed coordinate. Each append is privately sealed with the parent
event and projection hashes; member cards must be frozen before execution. POST
cards require exact canonical JSON, a bounded body hash, and JSON content type.

Private artifacts contain the request and response evidence under an absolute
0700 root. They are written through an exclusive no-follow temporary file and
atomically linked into an immutable 0600 sealed artifact. Public receipt and
accounting values contain hashes and request metadata only; they contain no URL
or response body.

Every public receipt binds plan, cohort, policy, source, arm, and implementation
SHA-256 values supplied by the canonical C10 coordinator. This package cannot
create a plan, acquire a lock, execute an arm, settle, roll back, quarantine,
or activate a source.

`strict_detail/` now contains source-local batch-A producers for JLL, JLL
Investor, Colliers SalesTracker, and Marcus & Millichap. They remain unregistered
and not fully verified. Avison Young and Colliers Main are explicit blockers,
not degraded receipts, until their browser-dependent detail paths can meet this
same ephemeral one-attempt evidence contract. See
[`strict_detail/README.md`](./strict_detail/README.md).
