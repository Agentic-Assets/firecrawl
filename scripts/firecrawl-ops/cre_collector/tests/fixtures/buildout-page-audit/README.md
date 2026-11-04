# Buildout page-cache audit

`buildout-page-audit.ts` reads an existing registered Buildout page-cache wrapper
offline. It creates a private, exclusive audit file; it does not call the
provider, connect to a database, enqueue work, change lifecycle or certify an
inventory. Preserve the input file. This is not a `collect.ts` artifact and must
not be sent to the listing ingester or complete-source admission.

From the collector directory, set all seven environment variables and run
`node --import tsx buildout-page-audit.ts`:

| Variable | Purpose |
| --- | --- |
| `BUILDOUT_AUDIT_SOURCE` | Exact registered source key, such as `faris-lee` |
| `BUILDOUT_AUDIT_INPUT` | Existing page-cache wrapper path |
| `BUILDOUT_AUDIT_OUTPUT` | New output path in an owned private directory |
| `BUILDOUT_AUDIT_GENERATION` | Expected nonempty run generation; use an explicit placeholder when it was not recorded |
| `BUILDOUT_AUDIT_RUN_START` | Expected UTC run-window start |
| `BUILDOUT_AUDIT_RUN_END` | Expected UTC run-window end |
| `BUILDOUT_AUDIT_NOW` | UTC evaluation time |

Times must be valid UTC ISO timestamps ending in `Z` with optional one-to-three
fractional-second digits. The observed time must fall inside the caller's run
window, at most 24 hours before evaluation and no more than five minutes after
it. The caller supplies this context; it is not independent proof of a run.
The reader checks descriptor metadata before and after reading and refuses a
change. This detects ordinary concurrent changes; metadata checks do not prove
immutability against adversarial same-user changes.

The result always labels coverage unknown, authority none, and publisher
authentication unproven. Missing or mismatched generation, hash, timing, source
or transport context is `provenance_incomplete`; parsed rows remain available
with per-row reasons for unresolved identities or lifecycle/tenure flags.
`parsed_json_consistent_fidelity_unverified` means the checked parsed representation
was consistent; it does not establish complete coverage, authentication, or
lossless parsing. Unsafe integral or nonfinite values are flagged. JavaScript
JSON parsing does not verify duplicate keys or decimal precision.

**The output is raw sensitive data, not redacted.** `originalCacheBytesBase64`
contains the exact original cache-file bytes, and `cacheFileSha256` hashes those
bytes. The parsed-data hash is calculated over `JSON.stringify` of parsed data,
not original cache bytes or the provider's HTTP response bytes. Keep both input
and output private and follow applicable retention rules; do not paste their
contents into logs or public artifacts.

Input is limited to 16 MiB; output to 40 MiB; parsed structure to one million
nodes and depth 256. The output limit is checked after serialization and does
not cap peak memory use. Limit or JSON errors leave the original input untouched.
The output directory must already exist, belong to the operator, have no group
or other access, and resolve without symlinks; output creation is exclusive and
mode `0600`. Parent/ancestor checks are not a defense against a concurrent
same-user directory replacement: use a stable operator-owned private directory.
