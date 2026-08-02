# CRE complete freshness refresh

Date: 2026-07-29

Branch: `fix/cre-refresh-freshness`

Draft PR: <https://github.com/Agentic-Assets/firecrawl/pull/25>

Linear: [AGENTIC-1229](https://linear.app/agenticassets/issue/AGENTIC-1229/restore-cre-listing-pipeline-correctness-and-prove-the-om-facts-canary)

## 2026-08-02 execution amendment

The collector registry now contains 51 source keys. The 20-source scope and
matrix below describe the July 29 hardening snapshot and are no longer the
complete execution scope. The August 2 refresh must use bounded source
generations in the canonical `SOURCE_KEYS` order so every admitted generation
can finish within the 24-hour observation window. A single `--sources all`
generation is not acceptable for this runtime envelope. The supported command
is `cre_checkpoint_series.py --sources all`, which creates one serial
checkpoint generation per source and stops on global or resource failures.

Every new checkpoint also uses the mandatory host CPU guard: Mach CPU samples
every five seconds, refusal to start at or above 80 percent, interruption after
30 sustained seconds at or above 80 percent, and fail-closed interruption when
telemetry is unavailable. The API and Playwright containers remain capped at
two CPU cores each. Resource trips preserve the exact checkpoint, write
`logs/host-cpu-guard.jsonl`, release the canonical lock, and exit `75`.

The abandoned `2026-08-02T045442Z` manifest used an explicit legacy 20-source
list and stopped after read-only pre-validation. It recorded no source attempt,
artifact, gate, dry-run, ingest, or readback. Do not resume it as the complete
refresh. Start new bounded generations from the clean, pushed CPU-guard SHA.

## Scope and safety

This refresh covers the 20 source keys supported by the TypeScript collector.
It is additive: it does not pass `--mark-missing`, activate statuses, run OM
parsing, or write market-data objects. Unsupported legacy brokerages are
reported separately and are not labeled fresh.

The four CRE launch-agent labels were verified not loaded during supervised
refresh work:

- `ai.agentic.cre-daily`
- `ai.agentic.cre-weekly`
- `ai.agentic.cre-enrich`
- `ai.agentic.cre-monitor`

## Starting database snapshot

Read-only snapshot at the start of the final hardening pass:

- Active canonical listings: 115,161
- Total canonical listings: 120,483
- Active supported-scope listings: 104,017, or 90.32%
- Active unsupported legacy listings: 11,144, or 9.68%
- Listings refreshed since 2026-07-29 00:00 UTC: 24,812, or 21.55% of active
- Listings created since 2026-07-29 00:00 UTC: 677
- Contacts: 182,318
- Documents: 72,853
- Images: 559,748
- Links: 469,211
- Media: 11,024
- Market-index rows, unchanged ownership layer: 3,251
- OM-facts rows, unchanged ownership layer: 398,040

These values are a starting snapshot, not the final refresh result.

## Freshness contract

Strict sources:

- `cbre`
- `jll`
- `jll-investor`
- `colliers-main`
- `cushman-wakefield`
- `svn`
- `lee-associates`
- `franklin-street`
- `newmark`
- `savills`
- `transwestern`
- `marcus-millichap`
- `nai-global`
- `matthews`
- `srs`
- `hanley`
- `kidder-mathews`

Scoped supported sources:

- `cbre-dealflow`: provider cards without a canonical public detail identity
  remain inventory-only source-index rows.
- `colliers`: SalesTracker cards without a canonical public detail identity
  remain inventory-only source-index rows.
- `avison-young`: proves current inventory and property-detail observation;
  existing contacts are preserved when the supplemental team feed is
  unavailable, so that state is not a fresh-contact claim.

Every admitted strict artifact must use current-source observations, an
immutable generation ID and start time, `maxAge: 0` on Firecrawl reads, exact
source identity reconciliation, and no hidden truncation. Resumes expire after
24 hours. Live readback must match the expected generation ID, exact active
canonical count, exact inventory-only count, and observation-time lower bound.
The checkpoint manifest also binds resume and every database child operation
to a credential-free database-target fingerprint so one generation cannot be
split across database environments. Ambiguous libpq multi-host or
target-overriding URI forms fail closed. Observations beyond a five-minute
clock-skew allowance are rejected.

Child handling is explicit. CBRE and the Buildout sources replace their
collector-owned children. SRS, Hanley, and Kidder Mathews preserve existing
children. Other strict sources require an admitted current detail observation
and cannot use preservation to hide a failed detail read.

## Code verification

- TypeScript typecheck: passed
- TypeScript unit tests: 594 passed, 0 failed
- Python tests: 1,611 passed, 17 skipped, 0 failed
- Independent source-adapter review: no remaining P1/P2 after fixes
- Independent checkpoint/ingest/readback review: no remaining P1/P2 after
  database-target, recovery, snapshot, and future-time fixes
- Live production writes from the final hardening pass: none
- Read-only production validation: passed in one repeatable-read snapshot; all
  11 query groups completed and active listing/view counts reconciled at
  115,161

Starting-snapshot evidence was generated at
`2026-07-29T15:48:06.016050-04:00` with a read-only production query and saved
locally as `/tmp/cre-refresh-baseline-2026-07-29.json`. The final supervised
run must save its manifest, generated `report.md`, validation output, and exact
pushed SHA under the checkpoint run directory before this document is treated
as terminal evidence.

## Live source matrix

The final supervised run will append exact generation, collected, staged,
inventory-only, new, changed, and readback results for every source. No source
is marked final until its checkpoint and live database readback are terminal.

| Source | Contract | Final generation | Staged | Inventory-only | New | Changed | Exact readback |
|---|---|---:|---:|---:|---:|---:|---|
| CBRE | strict | pending | pending | pending | pending | pending | pending |
| CBRE Deal Flow | scoped | pending | pending | pending | pending | pending | pending |
| JLL | strict | pending | pending | pending | pending | pending | pending |
| JLL Investor | strict | pending | pending | pending | pending | pending | pending |
| Cushman & Wakefield | strict | pending | pending | pending | pending | pending | pending |
| Colliers SalesTracker | scoped | pending | pending | pending | pending | pending | pending |
| Colliers Main | strict | pending | pending | pending | pending | pending | pending |
| Newmark | strict | pending | pending | pending | pending | pending | pending |
| Marcus & Millichap | strict | pending | pending | pending | pending | pending | pending |
| Avison Young | scoped | pending | pending | pending | pending | pending | pending |
| Savills | strict | pending | pending | pending | pending | pending | pending |
| SVN | strict | pending | pending | pending | pending | pending | pending |
| NAI Global | strict | pending | pending | pending | pending | pending | pending |
| Lee & Associates | strict | pending | pending | pending | pending | pending | pending |
| Transwestern | strict | pending | pending | pending | pending | pending | pending |
| Matthews | strict | pending | pending | pending | pending | pending | pending |
| Franklin Street | strict | pending | pending | pending | pending | pending | pending |
| SRS | strict | pending | pending | pending | pending | pending | pending |
| Hanley | strict | pending | pending | pending | pending | pending | pending |
| Kidder Mathews | strict | pending | pending | pending | pending | pending | pending |

## Adjacent producer status

GetCREdata is not a brokerage-listing enumerator. It owns market-data and OM
producer layers. Its `main` branch is clean and current at
`aec740e967455693d11ae4ba857f0bd7738a57f2`, but the audited local checkout has
no repository virtual environment, no local environment file, and none of the
seven required unattended variables in the current shell. Therefore this
listing refresh cannot honestly claim a current GetCREdata market refresh.

Unsupported legacy inventory is tracked separately in
[AGENTIC-1972](https://linear.app/agenticassets/issue/AGENTIC-1972/restore-governed-adapters-for-legacy-active-cre-inventory).

## Remaining gates

1. Resolve every confirmed P1/P2 review finding and rerun affected tests.
2. Commit and push the exact verified code.
3. Run fresh supervised source generations from the clean pushed SHA.
4. Append exact source and database evidence to this report.
5. Update the draft PR and Linear evidence.
6. Recheck live scheduler state after every supervised generation is terminal.
   Do not install or load a CRE launch agent without the separately required
   named recovery approval.
