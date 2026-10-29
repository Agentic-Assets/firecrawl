Parent: [collector agent guidance](../AGENTS.md).

# Source fields

`rcm.py` keeps the CBRE, Colliers and Newmark contact-source match and the new CBRE price
currency witness outside the ingest orchestrator. A marker binds an asserted
source shape, not publisher authentication. Contacts without a stable name or
email remain in source facts rather than repeatable additive child rows. A new
CBRE price context must match the exact source, field, displayed value and USD
basis before text can create a numeric price per square foot; legacy inputs keep
their prior behavior. None of these checks erases the original source values.

Buildout's 25 registered shared feeds, SVN, Lee and Franklin may promote the
lease availability count from a strictly fresh, complete, source-bound inventory
artifact. Franklin requires independently complete sale and lease feed proofs;
an exactly proved empty side is allowed. Multi-source and non-strict count
artifacts remain outside this phase. Missing,
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

Transwestern may promote an elevator count and the source's typical-floor-size
literal from its accepted property Details tab. The producer binds the scoped
observations to the accepted detail URL, body hash and original observation
clock. The ordinary source identity and strict-freshness checks still apply;
the placeholder slug also needs its existing published-identity proof. An
address is not a new requirement for ordinary slug identities. Static extraction
does not claim computed visibility. Intentional tab hiding is distinguished
from explicitly concealed property/row ancestors.

Zero elevators is a reported value. A floor-size literal retains its source
units or lack of units; no square-foot measurement is inferred. Unparseable and
competing literals remain in the raw observations. Qualification keeps explicit
field holds separate from absence, so another pass cannot erase an ambiguity
before merge. Holds and conflicts stay local to their field; agreeing property
values can use the newer observation. The fixed three-field SQL helper shares
the same ownership/clock checks and preserves sibling provenance atomically.

Newmark's published-detail enrichment may add `cre_public_listing_facts` to a
copied listing during ordinary ingest. The source-owned helper projects the
original collector artifact through the existing Newmark credential-withholding
codec and content policy, rechecks the source witness, property comparison,
claim ID and URLs, and requires unique listing and observation bindings. The
original input is unchanged and the projected evidence artifact remains
non-canonical. Numeric literals and omitted source paths remain explicit; the
collector clock and source coverage do not establish provider freshness or
complete inventory. Unmatched observations do not produce a listing envelope.
