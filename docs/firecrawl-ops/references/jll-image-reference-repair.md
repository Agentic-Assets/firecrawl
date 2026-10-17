> Tracking issue: [AGENTIC-4371](https://linear.app/agenticassets/issue/AGENTIC-4371)

# JLL image-reference repair

`cre_repair_jll_images.py` is a dedicated operator tool for the exact, evidenced
malformed-reference cohort tracked on the issue. It is not part of an ordinary
refresh, a URL-pattern cleanup, or a generic database repair interface.

The production CLI accepts one immutable proposal and its exact candidate and
full-row preimage files. Preserve those originals, including their historical
`apply_authorized=false` fields. A separate execution contract records the
reviewed operation; editing an evidence file is not authorization.

## What the tool preserves

Only the approved nonprimary, positive-order image IDs may be deleted. The
complete candidate set is bound to historical source evidence, exact parent and
image identities, and PostgreSQL full-row digests. Valid, primary, ambiguous,
Investor, deleted-history and unstaged records are outside the repair scope.
The SQL does not write parents, other children, source indexes, jobs, history,
baselines or OM facts. Ordinary image-loss validation remains unchanged.

Native images and document/floorplan references are different from malformed
pieces of a comma-bearing `srcset`. A changed publication URL or lower raw child
count is not sufficient deletion evidence. Follow the
[listing fidelity standard](cre-brokerage-completion-playbook.md#comprehensive-listing-fidelity).

## Before using the tool

1. Read the issue's current reviewed evidence, implementation/test results and
   execution decision. Reconcile every material review finding.
2. Use a clean reviewed POSIX runtime at its exact selected commit. On Windows,
   execute inside the ext4 Ubuntu checkout. Preserve the separate native
   launcher checkout and the [runtime handoff procedure](development-and-runtime-checkouts.md).
   This tool does not update code or task pins.
3. Establish current sole-writer, resource, database-target and recovery state
   through `START_HERE.md`. Existing schedules and the Mac setup are separate
   operational contracts; do not change them to make a repair command pass.
4. Place the three hash-verified input files in private host-local storage.
   Inputs must be regular, singly linked, owner-only files under an owned
   directory with mode `0700`. Do not copy production credentials into a test
   clone or place raw preimages in Git, Slack or Linear.
5. Choose a new private work directory for each invocation. The tool creates it
   and refuses an existing directory. Keep all resulting preimages, SQL, logs,
   intent and terminal receipts together. An old failed invocation is immutable.

The large candidate JSONL is streamed; the bounded full-row preimage is loaded
in memory. These are calibrated evidence bounds, not listing-coverage limits.
No records are truncated or dropped to fit a bound.

## Isolated verification

Run collector tests only in a genuine standalone `git clone --no-hardlinks`,
as described in [SETUP](../../../scripts/firecrawl-ops/cre_collector/SETUP.md#isolated-verification).
From that clone's collector directory, the opt-in database contract is:

```bash
python3 tests/run_jll_image_repair_postgres_contract.py
python3 tests/run_jll_image_repair_postgres_contract.py --full-scale
```

The runner uses only the exact cached PostgreSQL image, synthetic data and an
owned disposable container with no network or published ports. It accepts no
database URL and never pulls an image. The optional full-scale case exercises
345,440 synthetic image references across 9,985 parents; its elapsed time is a
local fixture measurement, not a production-throughput guarantee. Preserve the
printed proof directory and terminal result, including failed attempts.

The ordinary Python suite separately covers private evidence handling and real
local client failures. Validation of the actual private input files is a third
proof: record their hashes, exact counts and staged manifest separately. A
combined execution receipt must describe those distinct scopes truthfully;
synthetic SQL tests alone do not validate the private production evidence.

## Modes and evidence

Run `python3 cre_repair_jll_images.py --help` from the collector directory for
the argument contract. Every invocation requires `--proposal`, `--candidates`,
`--preimage`, a new `--work-dir`, `--env-file` and `--expected-sha`.

| Mode | Effect |
|---|---|
| No mutation flag | Compares the staged evidence with the exact target in a `REPEATABLE READ READ ONLY` transaction. |
| `--verify-apply-rollback` | Executes the delete and its postconditions, then rolls the transaction back. This is still database DML. |
| `--apply` | Deletes the exact approved image IDs and commits only if every precondition and postcondition passes. |
| `--restore --receipt <applied-receipt>` | Restores the exact full image rows from the repair preimages, only while the original parent state and expected post-delete image cohort still match. |

All DML modes require `--approval` and `--verification-receipt`. The execution
contract binds the operation, reviewed executor and collector commit, immutable
evidence pins, staged manifest, isolated PostgreSQL verification and recorded
operator decision. The isolated receipt must distinguish synthetic transaction
tests from validation of the actual private evidence; it is not a production
execution result. Follow the current issue for concrete reviewed bindings.

The verification receipt must bind `postgres_terminal_proof_path` and
`postgres_terminal_proof_sha256` to the actual runner's `terminal-proof.json`.
Keep an exact-byte owner-private copy for execution. The tool verifies successful
exit, settled container cleanup and matching executor, test and runner hashes;
a self-declared successful verification object is insufficient.

For restoration, the current execution approval also supplies
`applied_receipt_sha256`. This must identify the exact original positive apply
receipt passed through `--receipt`, including its committed delete result and
code/approval provenance. A genuine receipt from an older reviewed executor is
allowed when its entire hash is explicitly bound by the current restoration
approval. This does not permit newer parent or image-cohort state to be
overwritten.

Before dispatch, the tool fsyncs complete six-column image restoration preimages
and an intent record. All modes take the canonical host lease. DML modes also
take the shared database lifecycle transaction lock, then deterministic
source-index and parent locks and an image-table write barrier. Mutations use
`READ COMMITTED` so that the
post-lock cohort check sees inserts committed before the barrier. Default
read-only comparison retains `REPEATABLE READ`.

Unexpected image triggers, rewrite rules, inbound foreign keys, schema changes,
parent-generation drift, changed/missing/extra images or changed approved IDs
refuse the operation. The image-table barrier briefly excludes other image
writers across brokerages; it is confined to this exceptional transaction.

## Failure and recovery

A durable positive receipt identifies `checked_read_only`, `verified_rollback`,
`applied` or `restored`. Inspect the exact requested operation, code and evidence
pins and actual database readback before reporting success.

A timeout, interruption, client failure or missing positive terminal result
leaves the mutation unverified and retains the recovery fence. Local process
settlement does not prove whether a remote transaction committed. Inspect the
private intent, transaction SQL and logs, then use the governed recovery and
independent readback procedure; never clear a fence or replay blindly.

Restoration is an exceptional reviewed database operation, not an automatic
error handler. It refuses a later refresh or any change to the expected image
cohort. If state has moved on, investigate and prepare a new exact decision
rather than overwriting newer listing data.

After an applied repair and independent preservation readback, capture a fresh
ordinary validation baseline and run a new supported JLL generation. Keep the
old failed generation and receipts unchanged. Code delivery, repair completion
and full-source/unattended acceptance are three separate claims.
