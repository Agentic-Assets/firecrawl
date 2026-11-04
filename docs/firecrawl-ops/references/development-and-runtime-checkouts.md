> Tracking issue: [AGENTIC-4282](https://linear.app/agenticassets/issue/AGENTIC-4282)

# Development and Runtime Checkouts

## Routine operation and updates

A healthy current Windows pipeline should run without reconstructing an old
handoff packet, obtaining a Mac readback, or waiting for all 51 sources to be
accepted. Establish the current local owner, task state, target and service
health; avoid overlapping writers. Hold or skip an affected source and continue
eligible sources where the runner supports it. An unknown write outcome remains
fenced until its actual result is established; do not blindly replay it.

For a routine reviewed update, select the intended merged main commit, wait for
an idle local writer boundary, and fast-forward the native and WSL runtime
checkouts. Install locked dependencies and rebuild only services whose actual
build inputs changed. Update the affected task pins, verify the resulting code,
dependencies and service health, then enable the intended tasks. Preserve the
active Compose overlay and unaffected image pins. Do not pull blindly under an
active writer or run an unattended pull. No Mac change is needed for this
Windows procedure.

For source changes and supported API or Playwright image changes, the foreground
reviewed updater accepts an exact reviewed current `origin/main` SHA from the
clean Windows development checkout, run as the configured Ubuntu user. Fetch
and review that SHA first; the command does not fetch the remote:


```sh
python3 -B scripts/firecrawl-ops/cre_collector/cre_reviewed_update.py --selected-sha <full-reviewed-sha>
```

It checks the current task definitions, holds their existing flags under the
shared writer lock, advances the native and WSL checkouts, repins, and restores
the original flags. For changed API or Playwright build inputs, it builds from
the selected tree, holds tasks, and checks local queue and browser idleness before
replacing only changed image IDs. That check is a point-in-time observation, not
a drain: another local API client can start work immediately afterward and its
request can be interrupted by recreation. Selected images run briefly with the
old collector checkout before code advances. Local API identity and queue response
or browser health are bounded readiness checks, not proof that a scrape succeeds.
A settled image failure restores the previous images and overlay when the
observed fleet remains within the captured old/selected transition. A mismatched
fleet or unknown native outcome retains the fence. After a verified rollback,
temporarily unavailable endpoints are reported as degraded while original task
flags are restored. If selected services become unready after code advances, the
command reports `completed_with_warnings` and does not claim service recovery;
the collector preflight continues to gate actual work.

The command refuses NuQ, Compose configuration, or collector dependency
changes before holding tasks; those updates need a separate reviewed procedure.
It reports existing image provenance as unknown and records bounded
service-health observations; a temporary unavailable service does not by
itself keep tasks disabled. The collector's own preflight remains responsible
for checking service health before collection. A failed native step is reconciled under the same owner only when it reports a
bound completion receipt and the actual checkout and task states match the
captured old or selected definitions. A settled hold failure restores the old
flags; a settled partial repin or restore can complete the selected pins and
original flags without stopping a successor. A successful selected-state
reconciliation returns `reconciled_after` identifying the failed stage; the
update is complete and does not need a retry. Uncertain task or Git steps retain
the lock fence; inspect the printed phase and output directory before recovery. If the owner has died, follow
[the known-settled dead-owner procedure](../../../scripts/firecrawl-ops/cre_collector/LOCK_AUTHORITY_RECOVERY.md#explicit-recovery-of-a-known-settled-dead-owner):
it requires three bound evidence files for the operation outcome, remote-work
settlement, and descendant settlement. That procedure clears only the owner
fence; it does not restore task definitions or flags. Inspect and separately
restore the actual task state before enabling schedules; there is no automatic
recovery for an uncertain post-advance step. Never blindly rerun or remove the
authority files.

The detailed packet, historical lineage and 900-second catch-up rules below
belong to the legacy guarded recovery or handoff paths that implement them;
they are not a general requirement for a settled current-state resume. Those
existing commands continue to enforce their own checks until changed. Their
forensic records remain available when an actual uncertain operation needs them.
A saved task definition may still be used to check today's task settings without
adopting its historical admission requirements. The command above does not implement a general image or dependency updater.

The CRE collector is one shared pipeline. A development branch, Windows launcher checkout, Linux
runtime checkout, and temporary test clone have different jobs; they are not separate pipeline
implementations or independent writer authorities.

## Checkout roles

- **Permanent development clone** (`%USERPROFILE%\GitHub\firecrawl-dev` on Windows): create
  branches, edit, review, and commit code. Keep its dependencies and build outputs local to this
  clone. Do not run the production refresh from it.
- **Native Windows launcher and audit checkout** (`%USERPROFILE%\GitHub\firecrawl`):
  open runtime/control tasks, retain launcher and audit files, and verify the intended source
  SHA. Run native launcher fixtures on Windows; POSIX collector execution and suites require WSL.
- **WSL2 ext4 runtime checkout** (for example `~/code/firecrawl` inside Ubuntu): run the approved Linux runtime
  and its foreground checkpoint owner. Keep Linux dependencies and runtime-owned files here.
- **macOS runtime checkout**: use the host-specific approved runtime with its own local
  dependencies and launcher state. Do not infer its path or state from Windows or WSL.
- **Temporary test clone** (WSL ext4 on Windows, host filesystem on macOS): run CRE collector suites against the exact candidate commit
  using the isolated-verification recipe in
  [SETUP](../../../scripts/firecrawl-ops/cre_collector/SETUP.md#isolated-verification).

In Codex, keep the existing `firecrawl` project associated with its launcher/runtime-control
checkout. Before starting a development task, open or register `firecrawl-dev` as a separate
project and select it for that task. This guide does not establish that the project has been
registered.

When a file-edit tool targets a checkout other than the chat's working directory,
use absolute file paths. A shell command's `workdir` applies only to that command;
it does not change the base directory of other tools. Verify edits with `git status`
in the intended development or test clone, and keep the runtime checkout clean.

On Windows, verify that `.claude/skills` is a real directory symlink to `../.agents/skills`; a
text file containing the target is not a working link. A checkout may need `core.symlinks=true`
and Windows symlink support when creating that link. This affects project skill discovery only;
ordinary pipeline work does not require Developer Mode or administrator changes.

## Branches, locks, and shared code

A branch names a line of source history; a checkout is a filesystem tree with its own
dependencies, runtime paths, and lock files. Two checkouts on the same branch or commit do not
share local lock ownership. The CRE lock and authority evidence protect the configured runtime.
A Windows launcher targeting that runtime uses its lock. Independent runtime clones and other
computers have separate locks; cloning does not provide shared production-writer exclusion.

Keep the collector and ingestor as one shared implementation. Linux/WSL and macOS may use
different launchers and host setup, but do not fork the pipeline by operating system. The native
Windows checkout is a control surface; execute supported runtime work through the approved
Linux/WSL or macOS runtime.

Create a fresh temporary clone with `git clone --no-hardlinks` for collector tests. Use WSL's
Linux filesystem on Windows and the host filesystem on macOS. Do not use a Git worktree or run these suites from the runtime checkout: lock
discovery can otherwise resolve to runtime-owned state. Install test dependencies in that clone
using SETUP; do not copy `node_modules`, virtual environments, database or webhook credentials,
or runtime artifacts between checkouts. Keep credentials in private host-local files, and keep
runtime outputs in their owning runtime directory.

## Independent verification captures

Give each independent observation a new, unique output directory under ignored
`.scratch/`, including another observation of the **same build**. Reuse reviewed
capture code, not its previous output paths. Before executing a copied capture,
statically inspect its dot-sourced helpers, `$PSScriptRoot` references, output
directory and expected SHA: a new script location does not redirect paths embedded
in its code or helpers. Confirm every output resolves to the new observation's
directory before starting it.

Keep captures writable only while their owning commands are producing evidence.
After those writers have settled, record the observation clocks, code and target
identity, then seal the files with their hashes and sizes. Never overwrite frozen
captures or reuse their directory for a later readback. A sealed packet's references
must continue to resolve to the exact bytes reviewed; a matching build SHA does
not make two observations interchangeable.

Use one declared observation directory throughout admission qualification,
copy construction and authorization. Verify that the qualification receipt's
named input hashes match the actual copied inputs before authorizing a handoff;
matching build SHAs alone cannot bind two different captures. Recheck the
existing freshness limits rather than treating a copied receipt as newly observed.

Before removing a temporary test clone, preserve any policy-permitted primary
research response needed for replay in a unique ignored `.scratch/` directory in
the stable development checkout. Keep response bytes, metadata, safe request
provenance and observation times together; verify copied bytes against their
original hashes and sizes. Keep credentials out of the packet, follow source
visibility and redaction rules, and identify any transformed evidence explicitly.
A hash or field projection cannot reconstruct a missing response. If retention
is not permitted, record that replay limitation rather than promising raw bytes.

If old capture bytes have already been overwritten, preserve the currently
available bytes in a new incident directory and report the lost-evidence gap on
[AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752). Do not fabricate
a restoration, reconstruct missing old bytes as proof, or relabel a new observation
as the old one. Preserve any surviving hashes and receipts with their actual scope.

These rules govern independent verification outputs. Runtime-owned mutable
manifests, state and receipt paths retain their existing owner and update contracts;
this guidance does not relocate them or introduce another writer or lease.

## Moving a reviewed build to a runtime

The maintained Windows/Ubuntu code-only route is
`scripts/firecrawl-ops/cre_collector/cre_code_handoff.py`, with native fleet
operations in `windows/Invoke-CreCodeHandoff.ps1`. AGENTIC-4845 owns the tooling;
AGENTIC-3752 owns each operation and independent readback. This first route
supports unchanged dependency manifests/locks only. It does not install, collect,
call providers/databases, create tasks, change cadence or roll back automatically.

Invoke tooling from a separate clean, pinned Windows-mapped tooling checkout that remains stable
while both runtime checkouts advance. Pass input/output paths explicitly.
The development and tooling roles may share that checkout when their declared
identities match. Windows and WSL spellings normalize to the same role path;
the native source root and its clean SHA/tree must match declared tooling before
dispatch. Tooling remains outside both updated runtime checkouts.
The default preview validates saved evidence with no scheduler, runtime, process
or network observation:

```bash
python3 -B scripts/firecrawl-ops/cre_collector/cre_code_handoff.py --release /path/release.json
```

The immutable `cre-code-release-v1` input contains distinct `deployed`, `selected`,
`development` and `tooling` SHA/tree objects, explicit checkout `roots`, native
tool paths and Ubuntu distro/user. It binds a NUL changed-path input, complete
before/after Git object map, bundle, evidence roles (path/hash/size), explicit
`runtime_env_file`/`preserve_files` private host-local references, QA scope and
exact three-task baseline. The baseline includes complete XML references,
Enabled/result/action tuple and last/next UTC clocks. Unexpected tasks refuse,
including disabled tasks. Nonzero results need original terminal, settlement and
partial-data proof plus every contiguous SHA-only XML/readback edge for the whole
fleet. A current baseline never replaces original failed-operation provenance.

Historical fleet edges retain their own task clocks. A later successful Interra
or Newmark run requires one `natural_advancements` entry for that changed member
at the final historical-to-current boundary. It pins the original six Scheduler
event XML records, live action-to-WSL PID/creation bridge and source/SHA/Windows
admission stamp, native audit and drained streams, series/run manifests, settled
parent/child controls, and terminal task observation/XML. Each proof qualifies
its own member; later progress by another member does not rewrite that snapshot.
Ready/result zero alone is insufficient. The failed Qualified operation/result
and last-run provenance remain exact; fresh admission still binds every task's
complete current tuple. Missing, ambiguous or reused operation evidence refuses.

`combined-candidate-v1` reads the actual historical combined QA producer.
`owned-gates-v1` reads new gate receipts with actual command/cwd/identity, exit,
unchanged-tree and settlement evidence. Checkpoint commands have one closed
merged log; native capture has actual separate-stream EOF/drain facts. Reuse
requires reviewed executed/final identities and unchanged input hashes.
PR327-specific experiments or test counts are release evidence, never mandatory
actions for future releases. Unknown producer shapes refuse.

Maintained transitions qualify the complete saved commit readiness and independent
observation against their original pinned container snapshot and task XML. They
record `observed_at` and the actual `qualified_at` boundary: historical age and
trigger margins use that recorded clock, while live admission still uses current
time. Commit readiness explicitly proves `owned-maintenance` with recovery
required; independent readback proves `released-clear`. Complete integer-zero
queue measurements, frontend visibility, host/native resources, selected identity
and the exact three dependency digests remain required. Original pinned bytes are
retained for the enclosing publication recheck; historical QA is not replayed.
Native tooling refuses effective Git hooks and fsmonitor before invoking status.
The native handoff fixture requires an independently verified Git executable with
one hard link and no reparse ancestors; a PATH shim may correctly fail this guard.
Select that executable explicitly when needed, in fresh PowerShell 7 and 5.1:
`./scripts/firecrawl-ops/cre_collector/windows/Test-CreCodeHandoff.ps1 -GitExecutable 'C:/Program Files/Git/bin/git.exe'`.
Run the offline restoration fault fixture in separate fresh PowerShell 7 and 5.1
processes with a new proof output each time:
`./scripts/firecrawl-ops/cre_collector/windows/Test-CreCodeRestoration.ps1 -Output '<fresh-proof-directory>' -GitExecutable 'C:/Program Files/Git/bin/git.exe'`.
Its task operations and child capture are mocked; it never restores live tasks.
Native JSON ingestion preserves exact clock strings using
`ConvertFrom-Json -DateKind String`, or the qualified Desktop Windows PowerShell 5.1 reader.
Modern readers without `DateKind` refuse; clock coercion or reformatting cannot
substitute for the original reviewed strings.
The pinned `runtime_snapshot` role holds the reviewed five-container identity,
image/start epoch/caps/environment-digest snapshot. Admission/restoration/readback
retain and qualify browser healthy/zero pages/four-page cap, normal Linux memory
with at least 8 GiB available and CPU below 90 percent, plus actual native
CheckOnly measurements. Docker raw environment values are never durable output.

After preview, use separate new exclusive outputs for read-only observation and
reviewed authorization publication:

```bash
python3 -B scripts/firecrawl-ops/cre_collector/cre_code_handoff.py --release /path/release.json --observe --output /mnt/c/path/new-observation
python3 -B scripts/firecrawl-ops/cre_collector/cre_code_handoff.py --release /path/release.json --authorize --observation /mnt/c/path/new-observation/observation.json --authorization /mnt/c/path/new-authorization.json --review /mnt/c/path/review-attestation.json --reviewer REVIEWER --approval-ref AGENTIC-3752 --output /mnt/c/path/new-operation
```

Authorization binds original release/capture/review bytes and one operation output.
Its separate `cre-code-review-v1` attestation binds the exact release digest and
tooling identity, review-body path/hash/size, reviewer, reference, approved verdict
and `code-only-release` scope. It lives outside the immutable release to avoid a
circular digest. QA has its own pinned attestation for exact executed identity,
tooling and QA scope. Intact old review bytes cannot approve a different subject.
Capture age stays below 300 seconds and enabled triggers at least 900 seconds away
at publication and apply boundaries. Expiry or incomplete publication requires new
outputs, never timestamp rewriting or generic resume.

Only a separately authorized apply acquires the canonical recovery-required
lease, holds the fleet, performs ff-only updates/read-only dependency checks,
repins while disabled, and durably records held readiness before restoring prior
Enabled states. The native `Invoke-CreCodeHandoff.ps1 -Mode Apply` foreground
wrapper records actual outer stream completion around the Linux operation.

Before commit, any child/task/publication failure invokes the exact inventoried
fleet's dedicated failure hold. Cleanup requires the immutable mutation-start
binding, so rejected pre-admission requests disable nothing. Cleanup ignores
admission expiry/current code pins, records per-task uncertainty and never stops
a running owner or reacquires a lock. The original error remains authoritative.
After restored-task readback and durable `commit-ready.json`, commit begins at
canonical recovery clearing. Clearing, lease exit or release-proof failure is
`commit-uncertain`; terminal publication and later outer native EOF failure are
postcommit confirmation failures. They attempt the same exact task hold but do
not claim that a cleared or unknown recovery fence was retained. Independent
readback/recovery remains necessary; there is no automatic code rollback.

A committed terminal plus successful native completion still needs independent
`--postreadback --operation /mnt/c/path/new-operation --output /mnt/c/path/new-independent-readback`
with the same release input. Postreadback emits `cre-code-transition-v1`, joining
the original mutation/auth/output tuple, commit-ready/terminal/native receipts,
complete before/final/independent XML and unchanged task tuples. This actual
producer feeds the next release's failed-task lineage directly; historical edges
retain their original producer schema. `commit-ready.json` alone is never a
successful transition. Disabled-task next-run semantics remain unproven; held
checks preserve trigger XML and recheck enabled clocks after restoration.
A merged tooling PR or prepared input authorizes no
runtime delivery, data acceptance or scheduling change.

The held-task restoration path accepts a distinct
`cre-code-restoration-v1` packet through `--restoration-packet` (native
`-RestorationPath/-RestorationSha`), with a new review, authorization and exclusive
output. It preserves the failed predecessor and uses separate restoration
mutation, ready, terminal, native-completion and reconciliation records. It never
updates code, registers tasks or reuses a spent code-release authorization.
The ordinary Enable boundary is `zero-missed-no-crossed-trigger-v1`: every
fresh held and sealed-audit missed count must be zero, current/audit/original next
clocks must match, and each original desired Enabled trigger must remain at least
900 seconds away. Empty instances or a bounded no-catch-up observation cannot
satisfy this ordinary boundary.

AGENTIC-4845 adds the finite `founder-approved-catchup-v1` boundary for the
explicit one-time human choice concerning the original Interra, Newmark and
Qualified Brokerage Pipeline jobs. Its packet binds that choice, original failed
A release, tooling and the then-current complete held fleet, missed counts,
history, XML and desired flags. Future nominal triggers still require a fresh
900-second margin before each Enable. Automatic missed-trigger execution may
occur inside that buffer; no task definition, source scope or action changes.
This implementation does not establish production acceptance or authorize any
additional operation without its independent review and fresh admission.

Restoration cleanup runs only while the same maintenance lease remains owned.
A durable `release-start.json` precedes clearing its recovery requirement; all
later failures, including native drain and terminal publication, are receipt-only.
Context-manager return does not prove release. Confirmation requires the exact
normal clear authority or an authentic distinct scheduled successor. The
in-process clear check retains its same-PID witness. Later independent postreadback
binds the authenticated terminal and native completion to that exact authority
tuple and inode, proves its original PID exited, and observes the available flock
with no canonical directory or recovery requirement. A foreign normal sidecar
does not qualify. A successor join uses the actual native instance/action/PID and WSL admission scalar bridge,
Linux process birth and parent/control/run identity, and canonical authority
token, generation and directory. Missing, foreign or racing evidence remains
unverified without disabling, stopping, clearing or reacquiring over that owner.
The original XML principal SID must match the OS-resolved COM principal SID;
bound resolver account names qualify event identity only after that SID match.

Bounded Scheduler watermarks, raw events and instance samples distinguish no
observed execution, running native execution and completed native execution with
its actual result. They do not prove an empty delayed queue or successful source
settlement. Original A remains failed; its provenance is retained separately from
new after-tuples. A future ordinary delivery cannot attach that historical failure
to a changed scheduled run and requires distinct current source settlement.
The canonical task contract treats omitted
`AllowHardTerminate` as its documented true default while preserving explicit
false. Diagnostic-only termination overrides do not prove production equivalence.

Before any runtime handoff, use the current runbook and host tools to establish fresh idleness
and ownership on the exact target host: inspect active run/lock authority and actual scheduler
state. Preserve the prior source definitions, deployed code SHA, and scheduler enabled/disabled
state before changing the runtime. This document is not approval to write to the database,
enable a schedule, or change a runtime.

After the candidate has passed its isolated checks and is merged or otherwise selected through
the approved release path:

Development may be newer than the selected tested release. Bind the exact clean
development SHA/tree and tracking identity separately from the selected release
SHA/tree, and prove the selected release is its ancestor. Revalidate those
development conditions before the apply path acquires ownership or mutates a
runtime. Keep each runtime's exact old-to-selected checks and forward-only
tracking updates; do not rewind development or tracking refs to make the identities
match. A selected release does not acquire the acceptance of newer unselected code.

1. Record the reviewed commit SHA and verify the development tree is clean. Enumerate **all CRE
   task definitions targeting either checkout**, including finite-cohort tasks and disabled tasks;
   save their identities, full definitions, prior pins and enabled states. Prevent new launches
   from every affected task and prove existing owned processes are settled with no unresolved
   recovery fence before changing either runtime tree.
2. Under the existing ownership procedure, fast-forward the stable native launcher checkout and the intended ext4 runtime checkout to
   the selected commit using ordinary Git history. Do not force-reset either checkout. Keep all
   host-local dependencies and credentials in place.
3. Update every inventoried task's expected SHA to the same reviewed commit, preserving its other
   settings and cadence. Read back each complete definition and pin. Keep scheduling disabled
   until both clean checkout SHAs and all affected pins agree.
   At the target host, run the read-only dependency check (`bash cre_setup.sh --check`) and
   runtime checks in cre_status.sh and START HERE. Recheck the expected source definitions and
   exact code SHA; the selected task SHA is only the code pin and grants no runtime or data
   admission.
   If dependency manifests or lockfiles change, include the required installation
   and installed-dependency verification in the reviewed maintenance procedure
   under the same task hold and ownership. `cre_setup.sh --check` alone does not
   prove installed dependency-tree parity or native-addon readiness. Keep host
   dependencies local; do not copy them from a test clone or install an unused
   experimental stack merely to deliver an unrelated fix. A separately tested
   selected release without those dependency changes may be used when appropriate.
4. Run only under the existing foreground-owner, database, and scheduler gates in [START
   HERE](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md). Restore each affected task to
   its recorded prior enabled state after the supervised operation; do not enable a previously
   disabled task as part of a code handoff.

Before enabling an additional CRE task, include it in the reviewed handoff tooling and inventory.
Historical packets sealed for only Interra and Newmark remain immutable evidence for those two
definitions; they do not cover a later cohort task. Do not reuse such a packet to change runtime
code while leaving another task's pin, launch window or prior state unaccounted for. The exact
surface and activation decision belong on [AGENTIC-3752](https://linear.app/agenticassets/issue/AGENTIC-3752).

This is a coordinated update across Windows and Linux, not an atomic deployment. Keep the saved
definitions and previous commits for recovery at another proven settled boundary; never force-reset
an uncertain writer. Exact host paths belong in the context root's ignored `local/machine.md`.

A code pin can select earlier code; it cannot undo database writes. Treat data repair or
reversal as a separate reviewed database operation with its own exact-generation evidence and
approval. Do not describe code rollback as database rollback.

## Held-fleet runtime maintenance

The `--maintenance-packet` selector in `cre_code_handoff.py`, exposed through
`Invoke-CreCodeHandoff.ps1 -MaintenancePath ... -MaintenanceSha ...`, handles a
reviewed dependency and image update while the complete three-task fleet is
already held. It records separate old identities and changed-object scopes for
the native launcher and Linux runtime; they need not start at the same commit.
Both advance through ordinary Git ancestry to one selected, tested release.

Prepare and review [source-bound image and configuration evidence](cre-maintenance-image-evidence.md)
before observation and short-lived authorization. The owned Apply operation
checks the held task definitions, updates the two checkouts, installs only the
reviewed collector dependency scope, verifies installed dependencies, and
recreates only the selected source-built services in the existing native
Compose project. Credentials, database volumes, resource limits, model routing
and unrelated services retain their existing contracts. A configuration
difference refuses the operation; a prepared image alone grants no permission
to recreate a container.

For a reviewed maintenance or restoration packet, the native frontdoor also
supports one finite foreground invocation. Supply `-Mode Apply` and
`-FiniteReviewed` with the appropriate `-MaintenancePath/-MaintenanceSha` or
`-RestorationPath/-RestorationSha`, plus `-ObservationOutput`, `-Observation`
(the `observation.json` inside that new directory), `-Authorization`, `-Output`,
`-Review`, `-Reviewer` and `-ApprovalRef`. The operation output, observation
directory and authorization must be new and distinct. The same foreground
process qualifies the retained evidence before taking its fresh observation,
publishes the bound authorization, and then applies it under the existing
lease and ownership checks. The observation and authorization remain subject
to the same under-300-second limits and current boundary checks; this route
does not extend them. Keep the original foreground invocation through exit and
stream settlement. Do not replay its paths or infer success from prepared input.
Maintenance leaves tasks disabled, and neither checkout nor runtime is updated
while a collection writer is active.

Apply leaves all tasks disabled. A distinct Postreadback operation must join
the settled foreground exit, released owner, selected checkouts, full container
configuration and task definitions before its transition can be consumed by
the existing restoration procedure. Restoration separately preserves the
original desired task states and the reviewed missed-trigger policy. An
uncertain operation must be reconciled; never replay its output directory or
clear its recovery state manually.

This route admits the retained failed handoff and its already-held fleet. It
does not yet provide a general dependency/image updater for an enabled fleet.
The ordinary code-only handoff remains appropriate only when its unchanged
dependency and container checks pass. For subsequent merged changes, select
and verify the new release, then use the applicable reviewed handoff under a
settled task hold. A checkout's code pin is the deployed version, not proof
that a newer remote main has been delivered. Never pull or rebuild underneath
an active collection merely to make that pin appear current.

## Qualified continuation after a stopped maintenance

A stopped maintenance can leave newer checkouts and images with older task
pins. Keep the original failed operation and its partial outcome immutable.
The runtime-before identity describes the current Linux checkout; the original
failed-handoff identity still describes its history and the held task pins.
A separately reviewed continuation must authenticate those distinct states.

When historical container differences cannot be reconstructed from retained
aggregate hashes, do not claim that the old configuration was preserved or
that the stopped operation succeeded. Qualify the current stack against its
approved Compose model and authenticated image source instead. That review
must cover effective security, privileges, commands, environment, ports,
mounts, networks, resource limits and restart settings, and state any remaining
historical uncertainty. Current volume identity and historical continuity are
different claims. Unknown current critical controls refuse admission.

A continuation that changes only operations code can retain the running
images when their exact IDs and source provenance match the new release's
unchanged application build inputs, Compose inputs and dependencies. It must
bind the failed operation, actual governed owner-recovery receipt and reviewed
current-stack qualification, then verify the same five container identities,
epochs and complete normalized configurations before and after its owned
code-and-pin update. It still requires installed-dependency checks, genuine
foreground settlement, independent readback and separate restoration gates.

If a later owned continuation stops after registering only part of the fleet,
retain that failed command and the exact per-task XML/readback. A successor
must authenticate the failed operation and its separately governed recovery,
then bind each actual `pin_before` before replacing that member's SHA. The
original failed handoff and every intermediate failure remain separate facts.
A subsequently crossed disabled trigger needs a new complete held-fleet
observation and closed native capture, including its source and Scheduler
interval; it cannot be inserted into the earlier audit. Counters alone prove
neither execution nor an empty catch-up queue. Image provenance still resolves
to the authenticated image-producing ancestor, even when the immediate failed
operation did not build images.

Native maintenance and restoration mutation checks ask the same live lease
owner to recheck its complete, already validated evidence through a private
ephemeral socket. This avoids reparsing the historical chain in a new process
at every task boundary. Every request still rechecks immutable inputs, source
and executable identity, current authorization and observation deadlines, and
canonical ownership. There is no reusable approval or relaxed expiry. The
owner endpoint closes when the operation ends; failure cleanup retains its
separate owner-only check.

Retain the exact active image overlay as a runtime-owned artifact. Every
approved recreation must use the bound base file, native override and active
image overlay. Record the overlay path/hash and running image IDs; preserve
that active input during scratch cleanup.
Any future image update must authenticate the previous file chain and replace
its image overlay deliberately. This held-fleet continuation does not establish
a general updater for enabled schedules.
