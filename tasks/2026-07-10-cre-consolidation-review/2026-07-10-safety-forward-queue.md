# Forward queue after CRE consolidation safety work (2026-07-10)

Candidate work surfaced during the safety pass. This is a prioritized menu,
not a delivery commitment.

## Correctness

- **Restore the ZIP/CBSA mini test fixture** (confidence: verified gap).
  The clean branch runs 1,356 Python tests successfully but 30 geo tests fail
  because their expected fixture is absent. Restore or generate the 20-row
  fixture, then require the full suite before schema work.

- **Make `cre_market_index` choose one parser release** (confidence: verified
  cross-repository source gap). GetCREdata currently aggregates OM facts across
  parser versions with `max()`. Add a canonical parser-release view before
  relying on multi-version OM facts for market metrics.

## Hardening

- **Deploy and configure the tier alert path** (confidence: implemented but
  unconfigured). Set a reviewed `CRE_ALERT_WEBHOOK_URL`, force one safe failure,
  and verify exactly one alert while preserving the original tier exit code.

- **Add an ephemeral Postgres schema-contract test** (confidence: verified gap).
  Validate the GetCREdata PostgREST conflict target and consumer view
  dependencies against the collector migrations before either writer changes.

- **Add a generated status artifact** (confidence: verified documentation
  drift). Derive status from run markers, `cre_status.sh`, and a read-only DB
  probe instead of maintaining tier and OM-facts prose in `CLAUDE.md` files.

## Operations

- **Run one approved additive tier after runtime recovery** (confidence:
  runtime healthy, write path unverified today). Confirm a fresh `ok:true`
  marker after the restored Firecrawl API processes a bounded production batch.

- **Choose and implement GetCREdata scheduling** (confidence: verified gap).
  Its default-branch workflow is manual only and the aa-hub lane is disabled.
  Decide the operating owner and credential boundary before enabling either.

## Architecture

- **Approve the schema ownership contract in both repositories** (confidence:
  proposed locally, external acknowledgement pending). Mirror the contract in
  GetCREdata and use it as the gate for cross-repository migrations.

- **Evaluate `cre-listings` extraction only after stabilization** (confidence:
  planned, approval-gated). Preserve collector history and prove dark-run
  artifact equivalence before changing the launchd checkout.
