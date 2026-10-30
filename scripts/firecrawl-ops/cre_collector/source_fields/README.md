Parent: [collector agent guidance](../AGENTS.md).

# Source fields

`public_facts.py` owns bounded JSON inspection and display-fact construction
shared by the source-specific Newmark, SRS, Buildout and Kidder helpers. Source identity, URL and
observation-clock qualification remain in each source helper. A known technical
credential omission does not prove that every possible secret was detected.
Marked SRS, Buildout and Kidder staging also inspects credential assignments and Basic
authorization values, including recognizable copies and encoded JSON. Decoding
is bounded to five percent/HTML transformations; unresolved input is refused.
Ordinary prose such as "Basic rent" remains source data.

SRS preserves a parsed source snapshot through both collector prune boundaries.
`srsRawRetention` describes acquisition of that snapshot, including missing
identity components; it does not establish canonical admission or exact original
numeric literals. Marked rows receive an inspected staging copy and
`srsRawStaging` receipt before normalization. Unknown business facts, explicit
nulls, false values, empty values and array positions remain intact within the
declared bounds. The exact source paths
`apto_data.marketing_approver.Professional_Credentials_Certification__c` and
`apto_data.related_brokers[].Professional_Credentials_Certification__c` are
treated as business fields, with their values still inspected. The original private
artifact is unchanged. Unmarked legacy inputs retain their existing behavior
and do not replace a newer public envelope with an empty one.
Identified technical credentials and recognizable copies are omitted from SRS
staging and public facts with an explicit partial notice.

The current canonical schema still requires a usable source URL. Private
observations missing that URL are retained by collection without inventing a
link or bypassing admission. SRS projection requires the mapper ID or URL to
bind to its receipt with every present identifier matching. Its admitted source
clock is `inventoryObservedAt`; an unknown clock remains unknown. An omitted or
unbound carrier produces a partial envelope; unsafe source URLs are not staged
as canonical URLs. Incomplete inspection refuses staging, while display-size
limits produce explicit partial envelopes. Neither a source merge
nor saved-input replay activates production ingestion or proves full coverage.

Buildout retains the eligible inventory row once at the shared mapper and
restores it after collector pruning. Its source-qualified
`buildout-public-inventory/v1` envelope requires the retained mapper ID and URL
to match the receipt and binds the exact registered source and feed variant;
Franklin's sale and lease feeds also bind to the requested transaction pass.
The admitted source clock is `inventoryObservedAt`; an unknown clock remains
unknown. The inspected staging copy omits
technical document references and recognizable copies while the private
acquisition remains unchanged. Source-specific document-reference withholding
applies to native `pdf_url` and mapped brochure URLs; see the
[withholding rule](../source_evidence/README.md#draft-buildout-document-reference-withholding).
Referenced broker records receive the same inspection through a local overlay,
without changing the shared broker pool.

Kidder captures each eligible parsed inventory row privately as `rawKidder`,
with `kidderRawRetention` binding the mapper ID, URL, observation clock and
collector-owned cache disposition. The `kidder-public-inventory/v1` projection
recomputes the mapper identity from `listing_key`, then the existing
`property_key` fallback, and requires the receipt and emitted URL to match.
The shared `/properties/` fallback is not a unique-listing claim and cannot
qualify public facts. Numeric IDs qualify only when integral and within the
JavaScript safe-integer range; unsupported numeric IDs remain unqualified,
not reinterpreted. Missing or invalid observation clocks produce partial
context, not a freshness assertion. Marked rows receive an inspected staging copy
and `kidderRawStaging` receipt; identified technical credentials and
recognizable copies are withheld with explicit omissions, while the private source is unchanged.
Unmarked legacy inputs retain their prior behavior. This projection establishes
neither source authorization, canonical admission, current freshness, nor
complete inventory coverage.

When separate passes merge into one canonical property, ordered inventory
observations retain each envelope's facts and source context. Conflicts remain
visible inside those observations; normalized fields keep their existing merge
rules. Bounds and omitted references remain explicit. These envelopes describe
captured parsed values, not original number lexemes, source authentication or
complete inventory. Unmarked legacy behavior is unchanged.

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
Qualification follows the original artifact object across staging copies. A
count remains eligible only while its observation, identity and provenance
witnesses survive inspection unchanged; altered evidence becomes a field hold.

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
