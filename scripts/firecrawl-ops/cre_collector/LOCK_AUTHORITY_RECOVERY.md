# CRE lock authority and recovery

`out/daily/.cre.lock` remains the compatibility directory for the legacy lease,
PID, and benchmark markers. A sibling `.cre.lock.authority` regular file is the
new ownership authority. Acquisition creates that file with `O_CREAT|O_EXCL|
O_NOFOLLOW`, retains its descriptor, and records its inode and random token.
Every directory mutation, benchmark interlock, candidate rollback, and normal
release verifies that the descriptor, canonical authority pathname, and token
still agree. A replacement authority or lock directory therefore blocks work;
the replacement is not removed and candidate rollback does not run under it.

Legacy directories without a sidecar remain readable. A stale, non-recovery
legacy lock is reclaimed only after the existing dead-owner checks, then gains
an authority before the replacement directory is initialized. A recovery lease
or active/quarantine marker always stops automatic reclaim. If an authority
already exists, a live or starting owner blocks acquisition; an operator must
resolve recovery-required state rather than deleting either entry.

The protocol protects against accidental replacement, stale lock state, and
cooperating processes under the collector account. POSIX directory creation
does not return an FD, so it cannot defend against a malicious same-UID actor
that can continually rename paths and copy private authority contents. Such an
actor is outside the local operational threat model; the fail-closed checks
still avoid mutation when the canonical entries do not match the held authority.
If this is suspected, stop the collector and perform manual recovery rather
than deleting lock paths.
