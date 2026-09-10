# CRE throughput implementation closeout, 2026-09-09

This closes the verified implementation block, not the full CRE refresh.

- Branch: `codex/cre-scrape-throughput`, based on `fix/cre-refresh-test-determinism`.
- Implementation: `7f04c9e3cdac502d92c0fcb6ff3bb79d50edaab5`, pushed to origin.
- Local verification: collector typecheck and 814 tests; sidecar build and 25 tests; independent review; 360 successful patched-browser test requests. See [experiment report](experiment-report.md) for test limits and rejected profiles.
- Runtime: patched local browser admitted only after checkpointing the collector, draining its queue, and passing health checks. Original collector SHA and production database schema are unchanged.
- Completion boundary: only two of 51 source checkpoints certified at recovery; JLL and the full registry are not complete. Default-off direct transport is not active in this generation. GitHub CI and merge remain separate gates.

## Decisions

Use four browser pages with 500 ms start pacing, not the rejected unpaced four-page profile. Keep one absolute request deadline and explicit DNS-unavailability errors. Preserve the 75% watchdog. Defer broad upstream integration because its 23 conflicts span unrelated runtime and deployment surfaces; it contains no direct JLL browser lifecycle fix.

## Remaining authority and proof

No merge is authorized by this record. Continue the existing visible refresh monitor and establish source certificates, readback, and application-facing validation before claiming the original task complete. Runtime rollback is documented in the experiment report. Candidate follow-ups are in [forward queue](forward-queue.md); Linear remains the company work ledger.
