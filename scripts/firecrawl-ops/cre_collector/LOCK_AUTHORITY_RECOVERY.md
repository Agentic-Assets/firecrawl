# CRE lock authority and recovery

`out/daily/.cre.lock` remains the compatibility directory for the legacy lease,
PID, and benchmark markers. A sibling `.cre.lock.authority` regular file is the
new ownership authority. Acquisition creates that file with `O_CREAT|O_EXCL|
O_NOFOLLOW`, retains its descriptor, and records its inode, random token, and
the exact lease generation it will install in the directory.
Every directory mutation, benchmark interlock, candidate rollback, and normal
release verifies that the descriptor, canonical authority pathname, and token
still agree. A replacement authority or lock directory therefore blocks work;
the replacement is not removed and candidate rollback does not run under it.

Legacy directories without a sidecar remain readable. A stale, non-recovery
legacy lock is reclaimed only after the existing dead-owner checks, then gains
an authority before the replacement directory is initialized. An existing
sidecar is reclaimed only when it is nofollow-opened, its descriptor and named
inode remain identical, its well-formed owner/token/generation record matches
the stale directory PID and lease, that owner is dead, and no recovery lease or
active/quarantine marker exists. Any malformed, empty, replaced, mismatched, or
live sidecar is left untouched and requires operator recovery.

If authority initialization fails after exclusive creation, the same process
retains the descriptor, inode, token, and generation. It may complete that
exact sidecar and then acquire the lock for the mandatory rollback path. Other
processes see the sidecar as a stop; they must not delete or reuse it.

The protocol protects against accidental replacement, stale lock state, and
cooperating processes under the collector account. POSIX directory creation
does not return an FD, so it cannot defend against a malicious same-UID actor
that can continually rename paths and copy private authority contents. Such an
actor is outside the local operational threat model; the fail-closed checks
still avoid mutation when the canonical entries do not match the held authority.
If this is suspected, stop the collector and perform manual recovery rather
than deleting lock paths.
