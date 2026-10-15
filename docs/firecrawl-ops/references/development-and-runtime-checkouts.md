> Tracking issue: [AGENTIC-4282](https://linear.app/agenticassets/issue/AGENTIC-4282)

# Development and Runtime Checkouts

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

## Moving a reviewed build to a runtime

Before any runtime handoff, use the current runbook and host tools to establish fresh idleness
and ownership on the exact target host: inspect active run/lock authority and actual scheduler
state. Preserve the prior source definitions, deployed code SHA, and scheduler enabled/disabled
state before changing the runtime. This document is not approval to write to the database,
enable a schedule, or change a runtime.

After the candidate has passed its isolated checks and is merged or otherwise selected through
the approved release path:

1. Record the reviewed commit SHA and verify the development tree is clean. Save the exact task
   definitions and prior pins. Prevent new scheduled launches and prove existing owned processes
   are settled with no unresolved recovery fence before changing either runtime tree.
2. Under the existing ownership procedure, fast-forward the stable native launcher checkout and the intended ext4 runtime checkout to
   the selected commit using ordinary Git history. Do not force-reset either checkout. Keep all
   host-local dependencies and credentials in place.
3. Update each existing task's expected SHA to the same reviewed commit, preserving its other
   settings and cadence. Keep scheduling disabled until both clean checkout SHAs and pins agree.
   At the target host, run the read-only dependency check (`bash cre_setup.sh --check`) and
   runtime checks in cre_status.sh and START HERE. Recheck the expected source definitions and
   exact code SHA; the selected task SHA is only the code pin and grants no runtime or data
   admission.
4. Run only under the existing foreground-owner, database, and scheduler gates in [START
   HERE](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md). Restore the scheduler to
   its recorded prior state after the supervised operation; do not turn a previously disabled
   scheduler on as part of a code handoff.

This is a coordinated update across Windows and Linux, not an atomic deployment. Keep the saved
definitions and previous commits for recovery at another proven settled boundary; never force-reset
an uncertain writer. Exact host paths belong in the context root's ignored `local/machine.md`.

A code pin can select earlier code; it cannot undo database writes. Treat data repair or
reversal as a separate reviewed database operation with its own exact-generation evidence and
approval. Do not describe code rollback as database rollback.
