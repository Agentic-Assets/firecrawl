# C10 private receipt substrate

This TypeScript package is a sealed evidence substrate, not a collector or an
executable source registry. It includes source-owned candidate receipt
producers, but has no concrete `DirectProviderTransport` implementation, CLI
entrypoint, controller integration, or live execution path. It does not import
`collect.ts`, ingestion, checkpoints, cache helpers, or normal scrape helpers.
Python `candidate_registry()` remains unverified and `verified_registry()`
remains closed.

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
Large member graphs are sealed as bounded private shards plus a small root
manifest that commits to ordered shard digests, card count, member count, and a
graph-root digest; the per-artifact 2 MiB ceiling is never relaxed.
Stage receipts likewise retain only a bounded count/digest commitment to the
cumulative request-accounting ledger, whose individual accepted events are
already privately sealed by the one-shot transport.

Private artifacts contain the request and response evidence under an absolute
0700 root. They are written through an exclusive no-follow temporary file and
atomically linked into an immutable 0600 sealed artifact. Public receipt and
accounting values contain bounded hashes and request metadata only; they contain
no URL or response body.

Every public receipt binds plan, cohort, policy, source, arm, and implementation
SHA-256-shaped identifiers supplied by the canonical C10 coordinator. This
package validates their binding shape but does not yet define or verify an
implementation/source byte-hash recipe. This package cannot
create a plan, acquire a lock, execute an arm, settle, roll back, quarantine,
or activate a source.

`inventory.ts` contains executable source-local candidates for seven
authoritative-inventory sources. CBRE Deal Flow is an explicit blocked
descriptor, not an executable fallback: its ListingEngine needs a
provider-derived engine key and form-urlencoded POST response HTML. `strict_detail/` contains Batch A candidates
for JLL, JLL Investor, Colliers SalesTracker, and Marcus & Millichap.
`sources/batch_b.ts` contains the sole currently representable Batch B
candidate, Foundry. They all require a future reviewed coordinator to supply a
concrete direct transport, and they remain unregistered and unverified. Avison
Young, Colliers Main, and the remaining Batch B sources are explicit blockers,
not degraded receipts, until their source-specific paths can meet this same
ephemeral one-attempt evidence contract. See
[`strict_detail/README.md`](./strict_detail/README.md).
