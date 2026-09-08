# MR-01 source freshness closeout (2026-09-08)

**Branch:** `fix/mr-01-source-freshness`  
**Base:** `origin/main` at `b9882287a000f2dce74153201e3ca89b96b0aeab`  
**Implementation commit:** `f242eee6b`  
**Peer GetCREdata commit:** `5d2feea`  
**State:** Implemented and locally verified. Not deployed; no scheduler or canary was activated.

## Goal

Publish trustworthy source-observation health from the existing 51-source CRE
checkpoint workflow without letting a fresh cache conceal stale upstream data.

## What shipped

- `cre_source_health.py` projects checkpoint evidence into the versioned
  `producer-freshness-v1` contract. It preserves last-good observations across
  failed, stopped, and partial runs; separates attempt, observation,
  computation, and publication clocks; and classifies fresh, stale, and unknown.
- Canonical receipt publication is monotonic, cross-process serialized,
  symlink-safe, atomic, mode `0600`, and non-blocking to the authoritative
  checkpoint manifest.
- `cre_validate.py` reads queue health in its existing repeatable-read,
  read-only validation snapshot. Retryable, dead-letter, deterministic,
  transient, and unclassified counts flow into each source receipt without
  exposing URLs or raw errors.
- A complete source uses the oldest required inventory, detail, enumeration,
  or scope watermark and publishes a source vintage. Partial coverage cannot
  replace prior complete evidence or retire inventory.

## Verification

- MR-01 focused Firecrawl suites: 92 passed.
- `test_cre_checkpoint_refresh.py`: 262 passed; one time-expired July fixture
  failed, matching the verified `origin/main` stale-fixture class.
- Full `scripts/firecrawl-ops` suite: 2,294 passed, 18 skipped, 239 subtests;
  14 unrelated baseline failures remain. Thirteen are previously reproduced on
  pristine `origin/main`; the additional Bash empty-array healthcheck failure
  was reproduced directly on `origin/main` during closeout.
- Ruff, `py_compile`, and `git diff --check` passed for the changed surface.
- The final independent adversarial review reported no remaining actionable
  findings.

## Decisions

- Observation time, never cache or publication time, controls freshness.
- The weekly cadence plus three-day grace remains the existing policy; the
  five-minute allowance handles clock skew only.
- Queue failures are classified only from a known worker reason. Everything
  else stays explicitly unclassified instead of being guessed.
- Receipt publication is an operational sidecar. Failure cannot block the
  collection system of record.

## Left to the operator

- Authorize and run one bounded source canary, then a second consecutive canary.
- Confirm unchanged inventory reconciles across both receipts before enabling
  any producer runtime or scheduler change.
