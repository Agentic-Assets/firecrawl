# CRE lock authority and recovery

`out/daily/.cre.lock` remains the compatibility directory for the legacy lease,
PID, and benchmark markers. Its sibling `.cre.lock.authority` is now a
**persistent** regular file, not an artifact to delete during normal release or
stale-directory reclaim.

Each `SharedLock` opens the sidecar with `O_CREAT|O_NOFOLLOW`, takes an
exclusive nonblocking `fcntl.flock`, and retains that file descriptor until the
lock instance exits. The sidecar's versioned owner, token, generation, and
recovery-required state are read and written only through that held descriptor;
each state write is file-fsynced. Every directory mutation, interlock action,
partial recovery, rollback, recovery clear, and release also verifies that the
canonical authority pathname still names the held regular-file inode. A path or
inode mismatch is a fail-closed stop and never modifies the replacement.

The advisory flock is released automatically if the owner process exits or
crashes. A successor may then take the same persistent authority and reclaim a
dead, unmarked ordinary directory lock. It must not reclaim a
recovery-required authority state, a corrupt authority state, an active or
quarantined directory, or a live/starting directory owner. Recovery-required
content is durable and intentionally blocks automatic acquisition even after
the original process dies; an operator must inspect and recover it.

Legacy directory-only locks remain compatible. A stale, non-recovery legacy
directory can be reclaimed after the existing dead-owner checks and is then
migrated into the persistent sidecar format while the caller holds its flock.
The prior three-field sidecar format is read only for that controlled migration.
Malformed, empty, or replacement sidecars are not rewritten automatically.

When a caller first encounters a legacy directory, it fsyncs a versioned
`v1 neutral` sidecar before returning any live-owner or starting-owner stop.
That neutral state is reusable and never changes the legacy directory. For a
verified stale directory, the flock holder completes the durable reclaim
handoff first, then writes the successor generation to the held sidecar. A
crash or injected failure before that generation fsync therefore leaves either
the prior generation and a resumable reclaim state, or a fail-closed sidecar,
never a new generation that claims an old directory. Sidecar creation and
generation writes are fault-tested; a failed fsync is not represented as a
durability success.

Stale reclamation itself is a versioned, fsynced `v1 reclaiming` sidecar state,
not a best-effort recursive deletion. The record binds the old authority and
the stale directory's device/inode before the canonical directory is renamed
to `.cre.lock.reclaim` and the parent is fsynced. A new flock holder resumes
only that recorded inode: canonical source before rename, exact tombstone after
rename, a partially deleted exact tombstone, or neither after deletion. It
then fsyncs, restores the prior authority state, and only afterwards commits a
successor generation. A foreign, replaced, malformed, live, interlocked, or
recovery-required path fails closed untouched. The older empty `.reclaim`
sentinel is recognized only for a dead, authority-matching partial legacy
directory with no entries except safe matching `pid` and `lease` files. Its
transition has two durable prefixes: the reclaim record first marks the
original sentinel pending, then after its parent-fsynced rename to a
token-derived forensic name records that moved phase before the canonical
handoff. A successor accepts only the matching pending sentinel or matching
forensic guard for that exact stale source, so a crash in the guard-move window
resumes safely and a foreign guard is never deleted.

If initialization of a newly created sidecar fails after exclusive creation,
the same process retains the descriptor, inode, token, and generation. It can
finish the exact sidecar and obtain the directory lock for mandatory rollback.
Other processes are excluded by the flock. If that process dies before a
durable authority write, the resulting empty or corrupt sidecar is a manual
recovery stop rather than a reason to unlink it.

`fcntl.flock` is supported on the macOS and Linux local filesystems used by the
collector. It coordinates cooperating processes that use this protocol. It is
not a distributed lock and does not defend against a malicious same-UID actor
with direct filesystem access who can replace paths outside the protocol. In
that case, or after any authority mismatch, stop the collector and perform
manual recovery. Never delete the sidecar as an automated repair step.

## Historic pytest quarantine residue

`cre_capacity_runtime.py recover-quarantine` is the only operator path for the
historic pre-persistent-authority pytest residue. Its CLI wrapper injects live
runtime observation into the dedicated `cre_quarantine_recovery.py` archive and
replay module, which owns the filesystem state machine and forensic receipt
contract. It is dry-run by default,
accepts no alternate lock path, and requires the exact canonical directory and
its legacy two-field authority sibling, coherent active/quarantine marker
hashes, a dead matching owner, a fresh 90-percent/30-second CPU observation,
exact baseline resources, and idle API, browser, RabbitMQ, NuQ, crawl, and
collector evidence. It rejects malformed, live, recovery-required, replaced,
or non-pytest residue.

With `--execute`, it first writes and parent-fsyncs the private
`.cre-quarantine-recovery.json` guard through an atomic exclusive create and
strict readback. Before that claim, and throughout archive/replay, it holds the
stable private `.cre.lock.recovery-sync` flock. Every normal
`SharedLock.acquire` holds that same flock for its own lifetime before it can
inspect a guard or create an authority, so a recovery cannot archive the old
authority while a cooperating acquisition creates a new canonical lock. The
synchronizer is never moved or replaced by the protocol.

The launchd/manual tier entrypoint, `cre_tier_dispatch.py`, is also a normal
`SharedLock` owner. It forks `cre_run_tier.sh` only after taking both flocks and
passes the descriptor-bound authority to that child for the complete tier
lifetime. The child first creates a dedicated session/process group, then
execs the shell. SIGINT and SIGTERM are forwarded only while the unreaped
session leader proves that exact owned group. A bounded KILL escalation uses
the same proof before the leader is reaped. Afterwards the dispatcher keeps
both flocks until the group is absent but never signals a bare numeric PGID,
which could have been reused by an unrelated process group. The shell verifies
inherited proof before any collector work; it never creates, reclaims, or
removes the canonical lock namespace itself. That proof reasserts a
nonblocking exclusive flock on both inherited open file descriptions, so a
separately opened same-UID descriptor with copied metadata is rejected while
the real owner remains active.
Manual repair entrypoints are also ordinary `SharedLock` callers. They reject a
legacy file at the canonical lock path rather than unlinking or migrating it;
only the explicit governed quarantine recovery may handle that forensic
residue.

It then archives the directory and authority as an exact retained mode-0700
pair, fsyncing each namespace transition and advancing the guard through
`prepared`, `lock-renaming`, `lock-archived`, `authority-renaming`,
`pair-archived`, and `receipt-written`. Each phase accepts only its exact
top-level archive entries; the nested lock accepts only the bound active and
quarantine markers, and the final root adds only the bound receipt. Re-running
the same explicit command resumes only the recorded matching inode/hash pair;
a malformed, replaced, concurrent, or unexpected phase remains blocked. Only
after the immutable hashed receipt and the complete archive root are
revalidated does it fsync removal of the guard. It never unlinks or recursively
deletes lock artifacts. Any interrupted or uncertain recovery is a stop, not
permission for shell removal.

A guard phase records a completed durable archive prefix and the next intended
operation. It is not a perpetual assertion that a third party will keep a
canonical source pathname absent after the phase's check. Every phase that
would mutate another member, write the receipt, or clear the guard rechecks the
canonical sources it expects to be absent. A noncooperating same-UID writer can
therefore cause at most one intent-phase advance after a check; the next
destructive phase stops with the guard and all forensic evidence retained. The
protocol cannot atomically couple absence of an unrelated pathname with a
separate guard write on both supported platforms. That direct-filesystem writer
is outside the cooperative-process boundary above and requires manual recovery.
