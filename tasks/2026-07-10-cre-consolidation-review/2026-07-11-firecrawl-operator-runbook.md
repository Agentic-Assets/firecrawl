# Firecrawl CRE operator runbook: ordered next steps (2026-07-11)

**Start here for the live listing-pipeline handoff.** The Firecrawl safety
branch is review-ready, but it is not deployed and no production canary is
authorized. The current Mac mini has no active CRE job to pause, runs an older
checkout under `~/Documents`, and has stopped Colima and an unreachable local
API.

This runbook is the operational companion to
`OPTIMAL_EXECUTION_PLAN_2026-07-11.md`. It does not authorize a merge,
scheduler load, runtime change, database write, or canary.

## Do not do these yet

- Do not load or create a CRE launchd job.
- Do not start a production enrich, weekly, daily, or OM-parse run.
- Do not reactivate the retired daily tier.
- Do not apply collector SQL migration `015` to the observed production
  database.
- Do not move the Mac mini checkout, start Colima, modify secrets, or run a
  five-row canary without the separate approvals listed below.

## Required order

| Gate | Who must authorize or act | Evidence required before advancing | Pass evidence | Stop condition |
| --- | --- | --- | --- | --- |
| 1. Merge the reviewed repair | Cayman, using the literal phrase `Cayman approved this merge` for [PR #22](https://github.com/Agentic-Assets/firecrawl/pull/22) | PR is open, review-ready, mergeable, clean, and locally verified | Merge commit is available on `main` | No literal approval, merge conflict, or new review finding |
| 2. Recover the Mac mini safely | Cayman explicitly approves runtime recovery and checkout relocation or TCC authorization | Read-only preflight records checkout SHA, free disk, launchd state, Docker context, API health, and credential-file path only | Launchd-accessible checkout, Colima/Docker running, local API healthcheck passes, enough disk for artifacts | Missing credential file, TCC risk, stopped Docker, unhealthy API, or insufficient disk |
| 3. Deploy without scheduling | Authorized operator after gate 2 | Exact merged SHA is checked out; rendered plist paths and environment-file paths are inspected without loading jobs | `cre_status.sh --expected-sha <merged-sha>` reports the exact clean checkout and no accidental duplicate job | Wrong or dirty checkout, any unexpected loaded tier, template drift, or unresolved environment path |
| 4. Run the bounded enrich canary | Cayman explicitly approves the five-row production canary | Capture queue, stale-claim, dead-letter, listing, and validation baselines | Exit 0; fresh `ok:true` enrich marker; five completed claims; zero released claims; no constraint error, status change, or soft delete; clean validation | Any failed criterion, including an ambiguous result |
| 5. Approve and load one scheduler path | Cayman explicitly approves the named policy-compatible coordinator, owner, exact monitor/enrich/weekly labels, credential boundary, rendered plists, observation window, and rollback command | GitHub Actions remains manual-only; aa-hub is not an execution control plane; the retired daily label is absent from the load set | Approval and rendered configuration are attached to AGENTIC-1229 before the exact approved jobs are loaded | No named coordinator or owner, ambiguous credentials, any daily load, or any GitHub Actions/aa-hub unattended path |
| 6. Observe the listing pipeline | Authorized operator after gates 4 and 5 | Gates 4 and 5 evidence is attached to AGENTIC-1229 | Three scheduled enrich cycles, two monitor cycles, and one additive weekly cycle all pass | Run the recorded rollback command, preserve queue and artifacts, and investigate. Never reload daily as a fallback |

## Alert configuration

Failure alerts are optional and remain disabled until explicitly provisioned.
When approved, `install_launchd.sh --alert-webhook-file /absolute/path` renders
only an owner-only secret-file path into a plist. The URL must never appear in
Git, a plist, process arguments, or a Linear comment. A controlled failure must
prove one alert without changing the real tier exit code before alerting is
relied on.

## Cross-repository gates that remain independent

The listing canary does not authorize the market-data or product stages.

1. [AGENTIC-1230](https://linear.app/agenticassets/issue/AGENTIC-1230) must
   complete GetCREdata review, its approved producer DDL gate, a supervised
   first export, and the observation window on a separately approved
   policy-compatible coordinator.
2. [AGENTIC-1233](https://linear.app/agenticassets/issue/AGENTIC-1233) must
   record owner acknowledgement of a versioned property-type crosswalk. It must
   decide warehouse and industrial semantics, exact-match behavior, `all`
   fallback behavior, backfill, refresh, and rollback.
3. EQUIRE's market-context DDL is already recorded as applied. Only after the
   producer proof and crosswalk are proven may
   [AGENTIC-1232](https://linear.app/agenticassets/issue/AGENTIC-1232) complete
   consumer adoption and its release verification.
4. `cre-listings` repository creation and destructive cleanup remain last. They
   require explicit repository approval, stable canaries, a 30-day rollback
   window, and restore proof.

## Authoritative records

- [Firecrawl PR #22](https://github.com/Agentic-Assets/firecrawl/pull/22)
- `2026-07-11-execution-status-audit.md`: current branch, runtime, and gate
  evidence
- `2026-07-11-om-facts-writer-repair-closeout.md`: implementation and test
  proof
- `2026-07-10-safety-forward-queue.md`: deferred technical work
- [AGENTIC-1229](https://linear.app/agenticassets/issue/AGENTIC-1229): listing
  canary evidence and handoff
