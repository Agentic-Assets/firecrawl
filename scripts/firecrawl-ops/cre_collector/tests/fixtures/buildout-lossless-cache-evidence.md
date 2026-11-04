Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

This standalone offline research tool reads a private cached JSON file and emits
a private typed evidence tree. It does not read the audit envelope, invoke the
collector, access the network or database, authenticate source attribution, or
promote listings. Use `python3 buildout_lossless_cache_evidence.py --input
/private/path/cache.json --out-dir /private/path/new-directory` with an existing
private parent. The input is never modified, and the newly created directory
and file are private and exclusive.

Objects are ordered entries, including repeated keys; arrays and primitives use
explicit type tags, and numbers retain their cached lexical tokens. Ordinal
paths index object entries and array items in the typed tree; unlike JSON
Pointers they distinguish repeated keys. The original cached bytes and SHA-256
are authoritative for exact representation and recovery. This cache may already
have been serialized by JavaScript and is not original provider HTTP evidence.
A unique `data.inventory` produces unresolved record paths. Duplicate or
missing envelope/inventory keys produce an ambiguous status, with no selected
rows; their screened tree evidence is still retained. No field or property is
promoted, including fields that have duplicate keys.

Screening quarantines certain credential-shaped keys, values, and recognized
attribute pairs by ordinal path. It is heuristic, incomplete and may omit safe
facts; quarantine is counted and original private input remains the recovery
source. Both input and output may contain confidential or credential-bearing
content. Do not feed this research output into the restricted store or live
collection: source admission, JLL withholding, access policy, long-term storage,
source completeness and allowed evidence remain unresolved. Limits fail rather
than silently truncate, but the standard-library parser runs before structural
limits can be fully checked; adversarial inputs can consume resources. Bounds and
memory isolation need further review before any production use. The output-size
cap is checked after serialization and does not bound peak memory use. Input no-follow
and change checks do not authenticate bytes or protect a hostile parent path.
