# C10 private receipt substrate, protocol v3

This TypeScript package is a sealed evidence substrate, not a collector or an
executable source registry. The only executable TypeScript surface is
`issued_browser_child.ts`: it accepts exactly one Python-issued capability over
stdin and returns the sidecar response unchanged. It has no key generator,
receipt-store construction, lock interface, Compose control, or generic
transport constructor. The host coordinator is
`capacity_c10/host_session.py`; it owns those authority-bearing operations.
It does not import
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

Protocol v3 replaces the old shared-secret protocol completely. The coordinator
holds an ephemeral Ed25519 capability private key while the sidecar receives
only its public key. The sidecar separately holds an ephemeral Ed25519 evidence
private key while the coordinator receives only its public key. Each lifecycle
rotates both pairs; capabilities from an older lifecycle fail verification.
The sidecar's bounded in-memory nonce registry consumes an unexpired nonce
before page admission and prunes expired entries. It is intentionally not made
durable because the keys are ephemeral.

Private artifacts contain the request and response evidence under an absolute
0700 root. They are written through an exclusive no-follow temporary file and
atomically linked into an immutable 0600 sealed artifact. Public receipt and
accounting values contain hashes and request metadata only; they contain no URL
or response body.

Every public receipt binds plan, cohort, policy, source, arm, and implementation
SHA-256 values supplied by the canonical C10 coordinator. This package cannot
create a plan, acquire a lock, execute an arm, settle, roll back, quarantine,
or activate a source.

The only supported host-to-sidecar transport is the opt-in
`docker-compose.c10.yaml` overlay. Docker/OrbStack publishes its C10 listener
on `127.0.0.1` only; no Unix socket is mounted because the coordinator lock and
private receipt root must remain host-owned. A caller must prove the rendered
loopback port, hold the canonical `SharedLock`, durably claim an arm in
`C10SessionStore`, verify Linux `PrivateReceiptStore` support, and verify
signed, host-key-authenticated v3 health before execution. The coordinator
binds the durable claim digest, rather than a caller-controlled mutable ledger,
as `sessionSha256`; it never permits an alternate plan or ledger to resume a
claim. A lifecycle has one deadline covering Compose startup, health, browser
execution, evidence sealing, and cleanup. Any timeout, child failure, bad
signature, root replacement, or cleanup failure retains the canonical lock for
quarantine before it can be released.
The sidecar signs every evidence record with lease monotonic start/end values,
active/capacity observations, one exact engine attempt, ephemeral context/cache
semantics, and plan/cohort/card/manifest/session/arm/profile bindings.

`inventory.ts` contains source-local candidates for all eight
authoritative-inventory sources. `strict_detail/` contains Batch A candidates
for JLL, JLL Investor, Colliers SalesTracker, and Marcus & Millichap.
`sources/batch_b.ts` contains the sole currently representable Batch B
candidate, Foundry. They all require a future reviewed coordinator to supply a
concrete direct transport, and they remain unregistered and unverified. Avison
Young, Colliers Main, and the remaining Batch B sources are explicit blockers,
not degraded receipts, until their source-specific paths can meet this same
ephemeral one-attempt evidence contract. See
[`strict_detail/README.md`](./strict_detail/README.md).
