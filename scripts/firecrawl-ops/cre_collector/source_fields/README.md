Parent: [collector agent guidance](../AGENTS.md).

# Source fields

Buildout's 25 registered shared feeds may promote the lease availability count
from a strictly fresh, complete, source-bound inventory artifact. SVN, Lee,
Franklin, multi-source and non-strict artifacts are outside this phase. Missing,
invalid or conflicting observations do not create zero or clear existing data.
A newer observation updates an existing count only when its provenance schema,
source, native ID, URL, tenure and previous value match the existing owner.

The source observation retains the label, ordinal, literal, original clock,
normalized comparison clock, source identity, generation and artifact hash.
The hash binds the producer artifact consumed by ingest; it does not independently
authenticate the original provider body. The current raw payload is not a history
store, and durable evidence retention remains a separate gate. This module adds
no DDL, scheduler or independent database writer; its optional update runs only
within the existing ingest transaction when compatible columns are present.
