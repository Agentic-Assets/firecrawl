> Tracking issue: [AGENTIC-4282](https://linear.app/agenticassets/issue/AGENTIC-4282)

# Development and Runtime Checkouts

The CRE collector is one shared pipeline. A development branch, Windows launcher checkout, Linux
runtime checkout, and temporary test clone have different jobs; they are not separate pipeline
implementations or independent writer authorities.

## Checkout roles

- **Permanent development clone** (`C:\Users\CSEAGRAVES2018\GitHub\firecrawl-dev`): create
  branches, edit, review, and commit code. Keep its dependencies and build outputs local to this
  clone. Do not run the production refresh from it.
- **Native Windows launcher and audit checkout** (`C:\Users\CSEAGRAVES2018\GitHub\firecrawl`):
  open runtime/control tasks, retain launcher and audit files, and verify the intended source
  SHA. Native Windows is not a supported collector runtime or CRE test environment.
- **WSL2 ext4 runtime checkout** (`/home/cayman/code/firecrawl`): run the approved Linux runtime
  and its foreground checkpoint owner. Keep Linux dependencies and runtime-owned files here.
- **macOS runtime checkout**: use the host-specific approved runtime with its own local
  dependencies and launcher state. Do not infer its path or state from Windows or WSL.
- **Temporary ext4 test clone**: run CRE collector suites against the exact candidate commit
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
share local lock ownership. The CRE lock and authority evidence protect only the configured
checkout and host. They do not coordinate the Windows launcher, a second WSL clone, or a Mac.

Keep the collector and ingestor as one shared implementation. Linux/WSL and macOS may use
different launchers and host setup, but do not fork the pipeline by operating system. The native
Windows checkout is a control surface; execute supported runtime work through the approved
Linux/WSL or macOS runtime.

Create a fresh temporary clone with `git clone --no-hardlinks` on the Linux filesystem for
collector tests. Do not use a Git worktree or run these suites from the runtime checkout: lock
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

1. Record the reviewed commit SHA and verify the development tree is clean.
2. Fast-forward the stable native launcher checkout and the intended ext4 runtime checkout to
   the selected commit using ordinary Git history. Do not force-reset either checkout. Keep all
   host-local dependencies and credentials in place.
3. At the target host, run the read-only dependency check (bash cre_setup.sh --check) and
   runtime checks in cre_status.sh and START HERE. Recheck the expected source definitions and
   exact code SHA; the selected task SHA is only the code pin and grants no runtime or data
   admission.
4. Run only under the existing foreground-owner, database, and scheduler gates in [START
   HERE](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md). Restore the scheduler to
   its recorded prior state after the supervised operation; do not turn a previously disabled
   scheduler on as part of a code handoff.

A code pin can select earlier code; it cannot undo database writes. Treat data repair or
reversal as a separate reviewed database operation with its own exact-generation evidence and
approval. Do not describe code rollback as database rollback.
