# Forward queue after CRE consolidation safety work (2026-07-10)

Candidate work surfaced during the safety pass. This is a prioritized menu,
not a delivery commitment.

For the current live execution order, approvals, pass evidence, and stop
conditions, use `2026-07-11-firecrawl-operator-runbook.md`. This forward queue
is only for deferred follow-up after those gates are satisfied.

## Correctness

- **ZIP/CBSA mini test fixture restored** (resolved 2026-07-15).
  The full Python collector suite now passes 1,416 tests. Keep the fixture and
  geo coverage required before future schema work.

- **Make `cre_market_index` choose one parser release** (confidence: verified
  cross-repository source gap). GetCREdata currently aggregates OM facts across
  parser versions with `max()`. Add a canonical parser-release view before
  relying on multi-version OM facts for market metrics.

## Hardening

- **Deploy and configure the tier alert path** (confidence: implemented but
  unconfigured). After approval, provision an owner-only webhook file and pass
  its path through `install_launchd.sh --alert-webhook-file`. Force one safe
  failure and verify exactly one alert while preserving the original tier exit
  code. Do not place the URL in an environment plist or Git-tracked file.

- **Ephemeral Firecrawl PostgreSQL contract test added** (partially resolved
  2026-07-15). The test now proves fresh five-column identity, default refusal,
  approved legacy alignment, and idempotency. Cross-repository producer and
  consumer dependency checks remain part of the GetCREdata and EQUIRE branch
  verification.

- **Add a generated status artifact** (confidence: verified documentation
  drift). Derive status from run markers, `cre_status.sh`, and a read-only DB
  probe instead of maintaining tier and OM-facts prose in `CLAUDE.md` files.

## Operations

- **Run one approved additive tier after runtime recovery** (confidence:
  current Mac mini runtime is unavailable and the write path is unverified).
  Follow the operator runbook's bounded five-row canary criteria and confirm a
  fresh `ok:true` marker after the restored Firecrawl API processes the batch.

- **Choose and implement GetCREdata scheduling** (confidence: verified gap).
  Keep GitHub Actions manual-only. aa-hub is historical source and runbooks,
  not an execution control plane. Cayman must approve one named,
  policy-compatible coordinator, owner, credential boundary, rendered
  configuration, observation window, and rollback before unattended execution.

## Architecture

- **Approve the schema ownership contract in both repositories** (confidence:
  proposed locally, external acknowledgement pending). Mirror the contract in
  GetCREdata and use it as the gate for cross-repository migrations.

- **Evaluate `cre-listings` extraction only after stabilization** (confidence:
  planned, approval-gated). Preserve collector history and prove dark-run
  artifact equivalence before changing the launchd checkout.
