# C10 live calibration, JLL admission run attempt (2026-09-16)

Continuation of Phase 3 of the JLL admission live-run sequence
(`docs/firecrawl-ops/c10-jll-admission-lane.md`,
`docs/firecrawl-ops/c10-jll-admission-closeout-2026-09-16.md`,
`docs/firecrawl-ops/c10-live-calibration-phase2-2026-09-16.md`). **No live
network request against JLL was made in either sub-attempt below.** The
bounded admission action never reached sidecar startup on either occasion:
every attempt failed while acquiring the canonical CRE lock, before any
Docker Compose, browser, or provider work began. No database, cache,
listing, scheduler, or authority write occurred. No C10 sidecar
(`playwright-service-c10`) container was ever created this session
(`docker ps -a` shows none, both before and after); the generic Linux
runner container was brought up for preflight and the live-run attempt and
was torn down afterward (`run_linux_controller.sh down`, confirmed removed).

## Sub-attempt A (prior session): canonical lock quarantine, recovered

The first blocker (documented in the version of this file superseded by this
rewrite) was a pre-existing historic pytest quarantine residue in
`out/daily/.cre.lock` / `.cre.lock.authority`, unrelated to this branch's
code, that made `lock.acquire()` fail with `LockHeldError: CRE lock
authority is malformed` before any Docker Compose, sidecar, or provider work
began.

The orchestrator resolved this ahead of the current session:

1. `firecrawl-playwright-service-1` (the shared, general-purpose self-hosted
   Firecrawl playwright sidecar, not a C10-specific container) was recreated
   from the repo's governed `docker-compose.yaml` baseline (16 GiB
   mem/memswap, 8 GiB shm) so its live cgroup limits would exactly match the
   `bold-jll-128` benchmark profile the recovery tool validates against.
   Pre-recreate state was captured
   (`tasks/tmp/c10-live-calibration/playwright-pre-recreate-inspect.json`);
   the recreated container's healthcheck passed.
2. `cre_capacity_runtime.py recover-quarantine --execute` was run against
   the governed lock path. **Observed defect candidate:** the first
   `--execute` attempt failed pre-claim with `CpuTelemetryError: Darwin CPU
   tick counters did not advance` — the tool's CPU-idle sampler read two
   identical `host_processor_info` ticks under real timing on this
   Mac/OrbStack combination and treated that as a hard error rather than
   retrying or widening the sample window. A bare retry of the identical
   command succeeded on the next attempt (`phase: "completed"`, archive
   `out/daily/.cre-quarantine-forensics/e68ea0ebacf8ab39237934443b13bdcfc6cd48bdf853ead2015d3dc1f92e5ba5`,
   receipt sha256
   `b5544a427bbc01b5f7d3c0259afff3727ed58dd7d3872e64b0548baa5d3b7692`). This
   flakiness (transient non-advancing Darwin tick counters treated as fatal
   instead of retried) is a plausible root cause worth a separately reviewed
   fix in the CPU-idle sampler; it was not attempted in this session because
   reproducing and fixing a timing-sensitive macOS syscall wrapper safely
   requires dedicated test coverage outside this bounded run's scope.
3. Post-recovery, `out/daily/.cre.lock` and `.cre.lock.authority` were both
   confirmed absent (`find` over `out/daily/` shows no `.cre.lock` or
   `.cre.lock.authority` entries), and a `.cre-quarantine-recovery.json`
   terminal `"phase": "completed"` guard journal was durably retained per
   design ("retains a completed, identity-bound journal rather than
   unlinking evidence").
4. A session-wide pytest guard (commit `95167b66f`) now prevents test runs
   from leaking into the real canonical CRE lock path, closing the gap that
   produced the original quarantine residue.

## Sub-attempt B (this session): cross-mount identity mismatch, not resolved

With the canonical lock genuinely clear, this session:

- Verified HEAD `95167b66f` (matches PR #73), rebuilt both
  `firecrawl-playwright-service-c10:local` and
  `firecrawl-c10-linux-runner:local` from this exact checkout.
- Brought up the generic Linux runner, ran `npm install`, and confirmed all
  five `c10_linux_preflight.py` checks pass (`linux_platform`,
  `private_receipt_store`, `docker_socket`, `c10_image_present`,
  `compose_config_renders`).
- Ran the offline `collect-jll` dry-run inside the runner to reconfirm
  intent before touching the live path: `selection_rule:
  jll-canonical-url-lexicographic-v1`, 16 members, one sale/office/page-1
  enumeration card, `external_calls: false`. Adapter digest on this checkout,
  recomputed inside the runner: `1371211b54bc73d72b45c48daf241abf9588a7f20ec5dee76df567bcb065e390`.
  This is **not** claimed to "match" the Phase-3 value
  (`94c6dae7bda753e28f206c8e23e6f3d6f5a724689dfcb6f2a4588d4995789527`,
  `c10-live-calibration-phase2-2026-09-16.md`): the two digests were computed
  on different commits, and
  `capacity_c10.authority.repository_implementation_sha256("jll")` covers the
  whole verifier tree, not only `sources/jll.ts`, so an unrelated change
  anywhere in that tree between the two commits would change the digest even
  with no JLL-specific edit. Each phase must recompute and record its own
  digest on the exact commit it runs from rather than compare against a
  prior phase's number.
- Provisioned a fresh, empty, owner-0700 receipt root:
  `tasks/tmp/c10-live-calibration/receipts-run-20260916T103705/`
  (gitignored, verified via `git check-ignore`). Never reused the prior
  session's `receipts-run-20260916T100930/` root (left untouched, still
  empty).
- Ran the existing driver script
  (`tasks/tmp/c10-live-calibration/run_live_admission.py`, gitignored, not
  part of the reviewed collector surface) inside the Linux runner
  (`cwd=/workspace/firecrawl`).

### Attempt 1 (only attempt made)

| Field | Value |
| --- | --- |
| Outcome | Failure |
| Where it failed | `SharedLock.acquire()`, at the quarantine-recovery guard check, before `arm_benchmark`, before any Docker Compose or sidecar work |
| Wall time | 0.548 s |
| Error | `LockHeldError: CRE quarantine recovery requires operator completion` |
| Receipt root after | Confirmed empty (`ls -la` shows no entries besides `.`/`..`) |

No live network request was made; `external_calls` never left the dry-run
`false` state this session. No second or third attempt was made: the
blocking condition is a structural environment mismatch (below), not a
transient fault, and retrying identically would fail identically every
time, per this task's own "only retry after root-causing a failure" rule.

### Root cause

`SharedLock.acquire()` (`cre_checkpoint_refresh.py`) only trusts a retained
`"phase": "completed"` quarantine-recovery guard journal
(`.cre-quarantine-recovery.json`) after
`cre_quarantine_recovery.completed_guard_evidence_is_valid()` revalidates it
against the *current process's* live filesystem view. That revalidation
chain compares, by exact value, several fields that were captured by
sub-attempt A running **directly on the macOS host**:

- `state["lock_path"]` — an absolute host path
  (`/Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock`)
  compared with `!=` against the runtime `str(lock_path)`.
- `archive.parent` (parsed from `state["archive"]`, also an absolute host
  path) compared with `!=` against `lock_path.parent /
  QUARANTINE_ARCHIVE_DIR` computed at runtime.
- Inside `_validate_recovery_receipt`, the sealed receipt's own
  `lock_path`/`archive` fields (written by sub-attempt A, also absolute host
  paths) are compared the same way.
- `_archive_pair_is_exact()` additionally compares the archived lock/
  authority files' *current* `(st_dev, st_ino)` identity against the
  `(st_dev, st_ino)` pair captured by sub-attempt A at archive time
  (`_same_identity`, a deliberate anti-tamper/anti-hardlink-substitution
  check, not merely a path comparison).

This session's live-run driver necessarily executes inside the Linux runner
container (`/workspace/firecrawl/...`), per this repository's own documented
requirement that `capacity_c10.host_store.PrivateReceiptStore` is
Linux-only. The repo is reached there via an OrbStack bind mount of the same
physical macOS files, so the content is byte-identical, but:

- the absolute path prefix differs (`/workspace/firecrawl/...` vs.
  `/Users/caymanseagraves/Github/agentic-assets/firecrawl/...`), which fails
  every one of the string/`Path` comparisons above; and, independently,
- **`(st_dev, st_ino)` are not preserved across the bind mount.** Verified
  directly: `os.stat('out/daily')` on the host reports `(16777235,
  38957426)`; the identical directory, stat'd from inside the Linux runner
  container at `/workspace/firecrawl/scripts/firecrawl-ops/cre_collector/out/daily`,
  reports `(35, 5067)`. OrbStack's virtiofs-backed bind mount assigns the
  Linux VM its own inode numbers rather than passing through the macOS
  APFS ones.

So the recovery guard produced by sub-attempt A can never validate from
inside the Linux runner container, and the live admission call can never run
outside a Linux environment (the Linux-only receipt store). That Linux
environment does not have to be this specific container: a real Linux host
in the same lock domain as every other cooperating CRE process works too,
and is in fact the safer of the two (see the "established fact" this
session confirmed: `fcntl.flock` does not coordinate between a macOS host
process and a process reached through an OrbStack bind mount, so a
container reached that way must never hold the canonical CRE lock even once
this cross-mount identity gap is separately resolved). This is a structural
environment-topology gap between how quarantine recovery was documented and
run (directly on the macOS host) and how the live JLL admission call must
run (inside a Linux environment), not a defect introduced by this branch's
JLL admission code, and not something introduced by sub-attempt A's actions.

### Why no code fix was attempted this session

A path-only fix (normalizing the absolute-path comparisons to a
mount-prefix-invariant repo-relative form) was scoped and would have been
sufficient on its own, but `_archive_pair_is_exact()`'s `(st_dev, st_ino)`
identity check is a deliberate, separate anti-tamper measure guarding
against a hardlink/path-replacement substitution attack on the archived
forensic evidence, exactly the class of check
`LOCK_AUTHORITY_RECOVERY.md` says must fail closed on any mismatch
("A path or inode mismatch is a fail-closed stop and never modifies the
replacement"). Relaxing or bypassing that check to tolerate a legitimately
different (bind-mount-assigned) inode would blur the line between "the same
file, viewed through a different mount" and "a different file with the same
content," which this module is explicitly designed never to conflate
automatically. Doing that safely needs a deliberate, separately reviewed
design (e.g., an explicit, attested bind-mount-equivalence declaration
checked through its own protocol) rather than a same-session patch to a
security-critical identity check, which this task's instructions
specifically forbid weakening. No file under `out/daily/.cre.lock*` or
`.cre-quarantine-recovery.json` was modified, deleted, or read past its
already-known content; per `LOCK_AUTHORITY_RECOVERY.md`, "never delete the
sidecar as an automated repair step" and "it never unlinks or recursively
deletes lock artifacts" were both honored.

### What is unaffected

- No provider (JLL) request was made.
- No database/cache/listing/scheduler write occurred.
- No authority file was installed or edited.
- No collector/`capacity_c10` source file was changed this session; no test
  gate reruns were needed.
- The canonical lock, its authority, and the quarantine-recovery guard are
  byte-identical to their pre-session (post-sub-attempt-A) state.
- `firecrawl-playwright-service-1` remains up and healthy at the governed
  16 GiB/16 GiB/8 GiB profile the orchestrator recreated it at; this session
  did not modify or restart it or any other shared infra container.
- Both C10 images (`firecrawl-playwright-service-c10:local`,
  `firecrawl-c10-linux-runner:local`) were rebuilt from HEAD `95167b66f` and
  left present for the next attempt; no container from either image was
  left running (`docker ps -a` confirmed clean of both after teardown).

### Recommended next step (operator decision)

The cross-mount identity mismatch above is a symptom, not the root cause.
The root cause, confirmed this session, is that **`fcntl.flock` does not
coordinate between a macOS host process and a process reached through an
OrbStack bind mount**: a host process and a container process have both been
observed holding `LOCK_EX|LOCK_NB` on the same file at once. That makes the
generic Linux runner container (`docker-compose.c10-runner.yaml`,
`capacity_c10/tools/linux-runner.Dockerfile`, `run_linux_controller.sh`,
`c10_linux_preflight.py`) unsafe for holding the canonical CRE lock on a
macOS host, independent of whether the archived-evidence identity check is
ever fixed to tolerate a bind-mount-assigned inode. `SharedLock.acquire`
now refuses whenever `CRE_LOCK_DOMAIN_UNTRUSTED` is set (the runner sets it
unconditionally) and `c10_linux_preflight.py` reports failure in that state,
so neither path can silently report "ready" while this is true.

Normalizing the path comparison or accepting an attested bind-mount
equivalence, and running quarantine recovery from inside the container, are
each **unsafe on their own** for exactly this reason: either would let a
container process take the canonical CRE lock while a macOS host process
could independently believe it holds the same lock, which is the leak this
task exists to close, not merely a false-positive identity mismatch to
work around.

Valid options, in order of preference:

1. **A real Linux host.** Run the C10 controller, the sidecar, and any
   lock-holding recovery step on an actual Linux machine (not a macOS/
   OrbStack container reached over a bind mount), so every cooperating
   process and the canonical lock file share one kernel's `flock` table.
   This is the only option that needs no new code.
2. **A host-side lock broker.** A small service running natively on the
   macOS host (not inside any container) that owns the real
   `fcntl.flock` on `out/daily/.cre.lock` / `.cre.lock.authority` and
   arbitrates requests from both host and container processes over an
   explicit RPC protocol, so the flock itself is always held from a single
   process on a single kernel. This needs a separately reviewed design and
   implementation; it is not a same-session patch.
3. **Move every lock holder into one kernel.** Run the macOS-side
   collector/orchestrator processes themselves inside the same Linux
   environment as the runner (e.g. the whole CRE toolchain moves onto a
   Linux host or VM), eliminating the host/container split entirely rather
   than bridging it.

Do not pursue a fix that only normalizes path/inode comparisons or only
moves recovery into the container: both leave the underlying flock
non-coordination in place and would reintroduce exactly the concurrent-
acquisition risk `CRE_LOCK_DOMAIN_UNTRUSTED` now blocks.

Once one of the above is in place and the C10 Linux runner (or its
replacement) is confirmed to be operating inside the *same* trusted lock
domain as every other cooperating CRE process, the sequence in
`docs/firecrawl-ops/c10-live-calibration-phase2-2026-09-16.md` section 4
(fresh 0700 root, `execute_jll_admission_collection`, `build-jll-bundle`,
`render-jll-authority` (never install), reviewed pin PR) still describes the
right admission steps, but that section is marked superseded pending this
lock-domain decision; see its own dated note.

## Files this session added (gitignored, not committed)

- `tasks/tmp/c10-live-calibration/receipts-run-20260916T103705/` (empty)
- `tasks/tmp/c10-live-calibration/attempt1-run-20260916T103705.log`
