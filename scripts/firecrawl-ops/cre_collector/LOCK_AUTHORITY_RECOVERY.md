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
directory with no entries except safe matching `pid` and `lease` files; its
empty guard is retained under a distinct forensic name rather than deleted.

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
