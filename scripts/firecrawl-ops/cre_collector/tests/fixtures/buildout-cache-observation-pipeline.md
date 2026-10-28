# Offline cache-audit observation pipeline

This is a private, local research path. It reads a saved `buildout-page-audit-v1`
file and produces a screened observation artifact, integrity manifest, and
projection. It does not make network requests or write to a database, collector,
scheduler, or canonical listing. Source authorization, publisher identity,
coverage and freshness remain unverified. An audit's status and provenance
reasons are untrusted claims, not proof. Known audit reason codes are allowlisted;
unknown codes fail without echoing their content. `reportedProvenanceIncomplete` reports
the caller-reported audit status separately from `upstreamIncomplete`, which tracks extraction
omissions and does not establish source readiness. Audit claims are not recomputed;
even a reported complete status cannot establish provenance or authorization. Screening is heuristic and does not
prove that private outputs contain no confidential information.

The builder decodes `originalCacheBytesBase64`, checks the supplied cache digest,
and reads the embedded cache bytes directly rather than reserializing the audit's
JavaScript-parsed rows. JSON duplicate object keys are classified as projection unsupported and fail
without changing or deleting the private input. This is a representation limit,
not evidence that the source is unavailable; lossless duplicate-key projection
and retention of unaffected rows require follow-up. Numbers are
parsed without binary float conversion, subject to existing bounded numeric
support. The embedded cache was already serialized by the cache process: it is
not original provider HTTP bytes and cannot restore fidelity lost earlier. Keep
the original audit and cache as private recovery sources. The artifact's input
hash covers the complete audit, while `auditContext.cacheBytesSha256` covers its
embedded cache bytes. Record pointers address the decoded cache, not the audit
JSON. Non-inventory wrapper information stays in the original audit.

Example (paths and identifiers are operator-supplied assertions):

```sh
python3 buildout_cache_observation_pipeline.py \
  --audit /private/path/audit.json --out-dir /private/path/new-output \
  --source-key example-source --capture-id example-capture
```

The output parent must already be owned by the caller and private. The new
output directory is created exclusively with mode 0700 and files with mode 0600;
existing paths are not overwritten. This does not protect against hostile
same-user parent-directory races. Inputs must be regular files and symlink inputs
are rejected; on platforms without `O_NOFOLLOW`, use a supported POSIX/WSL
environment. Reads check a single descriptor before and after reading, not an
atomic snapshot across different files and not cryptographic authentication.
A failed output write can leave a private partial directory for inspection.

Omit `--generation-id` and `--page` when unknown. Their manifest values are null,
and unresolved scope is capture-specific; it cannot establish identity or
deduplication across captures. All cache-audit inputs use capture-specific unresolved scope, even when
embedded metadata and caller assertions match or the audit reports consistent.
The synthetic model rejects changed evidence or scope for the same source and
capture identifier; it makes no source authenticity claim. The transaction
default is `unknown`. The model is in-memory and synthetic only, not a durable
store or production admission policy. Private input and output sizes are bounded;
exceeding a bound fails rather than truncating evidence. Quarantined values and
other exclusions are reported, and input must be retained for recovery.

Builder code changes invalidate old fingerprints. Preserve old inputs, artifacts,
and manifests; verify old artifacts using the matching old code, or create a new
artifact and manifest under new paths. Do not assume an old artifact will reseal
byte-for-byte with updated builder code. Projection records hashes of the projector and available-space parser files read
at projection time. Those file fingerprints do not authenticate the source or
prove loaded code could not differ from files changed during execution. This path does not constitute complete
inventory, detail enrichment, production persistence, or source authorization.
