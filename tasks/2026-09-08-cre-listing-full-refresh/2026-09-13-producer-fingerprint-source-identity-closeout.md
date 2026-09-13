# Producer fingerprint source identity closeout

**Branch:** `fix/cre-fingerprint-source-identity`

**Current main integrated:** `3daa1746622607c3421ceb28c284bf7742efa2fe`

**Implementation commit:** `2659d71ce0085fce5ef0cf34181a8f708ecfcf25`

**Integration commit:** `d3675018806e7b6152549f00a462558b88f7b8ca`

**Review:** [PR #45](https://github.com/Agentic-Assets/firecrawl/pull/45)
**State:** Code and read-only inventory validation complete. No new source
observation or freshness receipt was created.

## What changed

- Centralized the canonical listing source-key precedence shared by ingest SQL,
  inventory fingerprint SQL, and fixture callers.
- Made the inventory-generation fingerprint derive identity from the listing's
  folded external-ID namespace, persisted raw `sourceKey`, then brokerage slug.
- Limited `cre_source_index` to an observation clock whose `source_key` matches
  the canonical listing source ID. An absent or stale index identity now falls
  back to the listing observation clock and cannot rename the inventory row.
- Added fixtures for legacy raw shapes, absent index rows, stale index identity,
  and CBRE, JLL, and Colliers folded namespaces.

## Verification

- Focused collector suite on current main: 277 passed.
- Full collector suite: 2,207 passed and 18 skipped. One Colliers checkpoint
  test failed identically on untouched `origin/main`; it is outside this diff.
- Python compilation, import-order Ruff, and diff checks passed.
- Full Ruff comparison found 19 existing findings on this branch versus 20 on
  `origin/main`; the change introduced no new finding.
- Read-only validation of the configured shared database returned `ok: true`,
  51 of 51 required sources with nonzero fingerprints, and 119,046 active rows.
  The only warning was the known database collation-version mismatch.

## Boundaries

- No collection, ingest, shared-database mutation, receipt publication, merge,
  or deployment was performed.
- The current inventory can prove the identity query, but it cannot substitute
  for a new complete 51-source observation under the v2 freshness contract.
- Runtime readback requires Python 3, `psql`, and the approved read-only database
  credential path. Production collection retains its separate runtime and
  approval requirements.
