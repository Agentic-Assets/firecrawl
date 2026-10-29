READ ~/AGENTS.md FIRST (skip if missing).

# Private source evidence

Local verification is separate from deployment and production acceptance; see
[PR #363](https://github.com/Agentic-Assets/firecrawl/pull/363).

Read `README.md` and parent collector guidance. This package stores acquisition
observations, never canonical listings, source acceptance, normalized economics,
OM facts, lifecycle, status, or freshness. Identity and visibility assertions are
unverified. Admission receipts record an operator's review, not publisher
authentication. Private deal documents require EQUIRE's org/deal path and must
not use this public-listing acquisition store. JLL content needs its dedicated
withholding adapter; the generic admission rejects known JLL source keys/URLs.

Default planning and response conversion are local. Native JSON responses retain
their selected array, surrounding context and exceptional numeric literal map;
reconstruct that full representation before interpreting null placeholders.
Append requires explicit target binding and either per-plan admission or the
scoped, hash-bound recurring checkpoint policy documented in `README.md`;
an uncertain database outcome must be reconciled by exact replay/readback.
Never print text, raw facts, source credentials, or private results. Search is
chunk-local and does not establish document completeness. Tests belong under
`tests/source_evidence/` and run only in a standalone no-hardlinks clone. Use the
disposable migration/CLI runners for database behavior, never production fixtures.

Draft collector retention v2 splits nonempty `sources[].retainedObservations`
into indexed `source_observation` facts and records ordered `observationGroups`
in the header; listing `source_record` facts and v1 remain unchanged. Preserve
exact artifact reconstruction and exceptional numeric literals at their
original pointers. Listings and observations share the 10,000-record and
source-policy limits; the 16 MiB envelope, 3.5 MiB per fact/header, and 32 MiB
plan limits still apply. Oversized individual observations or unrelated metadata
remain refused. This draft is not deployed or written live and requires separate
production acceptance; it changes no canonical gates or source-coverage claims.

Version-2 recurring policies separately allow NAI and Avison native-page capture
and Faris map-credential projection. Read the corresponding README sections before
changing their format, pointer receipts, quotas, worker custody or replay order.
Projected observations cannot reconstruct removed source bytes; whole-response
JSON contexts do not assert a selected listing array. Effective-privilege checks
are bounded preflights, not server-side credential isolation. Production writer
activation remains subject to the shared-database review on AGENTIC-5040.

Draft Buildout parsed inventory capture is an explicit v2 format opt-in for 28
registered sources and 29 feeds, including SVN, Lee and separate Franklin sale
and lease feeds. The original 25 native receipt shapes and scope hashes stay
unchanged; Franklin requires its exact feed variant.
It retains parsed JavaScript JSON, not HTTP bytes or original numeric precision;
capture time is distinct from a nullable source clock, and memory-cache hits
create no fresh capture. Apply existing policy and credential checks: Faris
collector projection does not sanitize this format. The 128-page/128-MiB
attempt, 16-MiB parsed-body and observation, 3.5-MiB fact and 10,000-row page
limits apply. Capture failure stops the enabled path. This draft is not deployed
and requires separate production acceptance; no source admission or completeness follows.

Buildout document-reference withholding is a separate explicit projection,
`buildout-document-reference-withholding/v1`, for collector artifacts, and native
format `buildout_inventory_projected_v1`. The source-owned rule replaces only
reviewed document URLs with null plus a pointer receipt; it never fabricates a
token-free URL. Other fields, positions, unknown clean shapes, missing/duplicate
identities and original nulls remain intact. An authenticated receipt is required
to distinguish withholding from an original null; standalone validation cannot
prove the removed value existed. Unknown sensitive content still refuses
admission. Projected hashes and reconstruction describe only the derived JSON;
original URI/body/file hashes stay in private custody, not projected events.
Legacy map and Buildout parsed formats remain unchanged. See README.md for
collector and native opt-ins, loss boundaries and replay. No download or writer
activation follows from this projection.

Avison's raw producer files stay private and local. `avison_pages.py` applies
the source-owned `avison_native.py` projection before `avison_response.py`
creates database-bound observations. Original response/file hashes, integration
keys and withheld values must not enter those observations or capture IDs.
Unknown JSON shapes retain projected context without claiming listing rows.
HTTP status and final URL are unobserved by this transport; never invent them.

Fact-text search is a literal case-insensitive search over PostgreSQL's JSONB
text representation, not semantic matching, original bytes or document text.
Its private output contains match metadata only; use `get` for full facts.
Migration and index availability require separate operator authorization.

Draft `discover-documents` reads an existing validated plan and identifies
candidate URLs and their source occurrences offline. A relative link's nearest
explicit page URL is an unverified resolution base, not access authority or
proof of a listing relationship. Discovery makes no network or database calls
and does not download documents or run AnyDoc. It is subject to separate production acceptance and does not change source admission or canonical data.

Draft discovery can recognize document suffix hints in link strings and
captured URL-bearing scrape attribute values, and explicit document labels, MIME or download
attributes. Preserve native references, labels and unverified claim witnesses;
relative page bases are not access authority and previews are not originals.
Suffixes and MIME do not guarantee conversion support. The additions are
subject to separate production acceptance.

Draft bounded JSON/JSON-LD discovery never executes script text. Preserve the
original script text pointer separately from the decoded JSON pointer. Skip
incomplete or unverified scripts and references with explicit diagnostics;
legacy page contexts need an explicit zero omitted-character count before
unmarked fields are treated as complete. Apply existing source and credential
policies to decoded values. This requires separate production acceptance.
Preserve exceptional numeric literals with the explicit pointer-to-literal
null-placeholder map on derived occurrences and witnesses.

Draft `plan-document-acquisition` is a pure policy preflight over a validated
plan and rederived candidates. It produces a bounded source-scoped selection,
not a download, extraction, permission decision, or completed evidence write.
Operator-asserted exact host/source allowlists and limits do not authenticate
publishers. No DNS is checked by preflight; acquisition transport must
validate public IPs at each request and redirect hop and preserve actual
acquisition clocks separately from source-observation clocks. This draft is
not deployed; separate production acceptance is required.

Draft document policy v4 explicitly opts into `property_occurrences_first_v1`.
It keeps candidate manifest bytes and indices, groups only exact URLs, checks
existing host/reference admission and records every cap exclusion. Stable
priority is limited to consistent, live, same-generation Transwestern collector
property-location witnesses; missing, stale, malformed or conflicting bindings
cannot qualify their occurrence. A separate valid property occurrence may
coexist with footer or unknown occurrences of the same exact URL. A collector
artifact hash and source-body hash are bound
assertions, not an independent rehash of the original page. Manual plans without
recurring collector custody cannot acquire this priority. Policy versions 1–3
keep their original ordering and selection representation. This is unverified,
disabled unless explicitly configured and requires separate production acceptance.

Draft `document_pipeline.run` connects the supervised document transport to
local AnyDoc under the existing attempt manifest, save callback and foreground
owner lock. It rederives policy and the sealed source plan, checks public
destinations at each hop, and binds the actual download clock, URL and body
hash before extraction. Cumulative reservations cover application-delivered body
bytes including one probe byte and remain charged after failed or unknown
attempts; buffered prefetch, headers, framing and network bytes are outside that
bound. Fetch and converter
process timeouts and grace periods are not a whole-host deadline or native
memory limit. The pipeline seals private context, artifact, binding and plan
before policy-aware cleanup of rechecked original copies. Ambiguous or
interrupted work requires reconciliation and never resumes automatically.
One selection is allowed per source attempt; draft version-2 selection combines
multi-page native candidates. Draft refresh and series hooks are explicitly
opted in and disabled by default; no new writer pump, queue or scheduler is
activated. They are not deployed and require separate production acceptance;
do not claim publisher identity, visibility, complete coverage
or extraction accuracy.

Draft seal readback checks an exact private file set and optional expected marker;
authenticate the context independently. Conversion replay requires a completion
marker published after parent validation and rechecks retained fetch and converter
inputs without spawning. An artifact alone is insufficient, and deleted originals
cannot be freshly rehashed by seal readback. Recovery is subject to separate production acceptance.

Draft `document_recovery.sealed` runs under the existing owner lock with a
trusted saved attempt. It performs no fetch or conversion, preserves budget,
and may reconcile cleanup without inferring who removed an absent copy. Missing
persisted seal markers remain unresolved; recovery never rearms acquisition.

Draft version-2 document selections bind ordered source-page plans to one
attempt and one serial budget. Group exact URLs but retain every occurrence's
plan, capture, clock, labels and unverified identity witnesses; do not infer a
property merge. All page plans remain subject to seal validation. This is
subject to separate production acceptance.

Draft refresh and series document acquisition opts in with policy path, digest and
Node executable alongside automatic source evidence. Preserve these bindings on
child invocation and resume; reconcile uncertain or paused prior document work
before another attempt. Serial and pipeline collection invoke document work only
after collection returns and workers settle, under the foreground lock and before
canonical source-failure checks. Bind source, code, target and source-settlement
marker on resume. Revalidated seals and durable saves are recovery barriers.
Owned-process settlement precedes conversion completion; unknown cleanup keeps
the parent barrier. New-session escapees are outside that guarantee. Reserved
phase time includes fetch startup/reap and converter/reap; it is not a native-I/O
deadline. The integration is disabled by default, not deployed, and
unverified and does not bypass credential admission.
Exact known no-data remains incomplete but does not alone block an otherwise
authorized new source attempt; never restart its old document attempt.

Draft automatic evidence policy v3 separately admits selected source document
text to the existing guarded private event-store append lane. Version 1 and 2 do
not admit document writes. Pending, refused or uncertain document appends block
a new series attempt; uncertain writes require explicit exact-plan refresh
replay, never a new download or extraction. No canonical listing or OM writes
follow. This is disabled by default and requires separate production acceptance.

Draft evidence SQL uses an owned client with a 45-second deadline and
incrementally bounded output: SQL input is capped at 72 MiB, stdout at the
caller-selected limit up to 32 MiB, and stderr at 64 KiB. Preserve the foreground recovery barrier when
local cleanup is unknown; local settlement does not prove remote rollback.
Uncertain appends require exact-plan replay. Foreground evidence saves persist
before checking sticky CPU and memory trips; terminal and error publication uses
the ordinary save path. This requires separate production acceptance.

Document export reconstructs the complete bounded capture, verifies ordered
chunks and text hashes, and writes exact Markdown plus provenance into a new
private directory. Keep unknown extraction coverage visible; a valid text
digest proves reconstruction integrity, not faithful extraction of every page.

Draft `enrichment_custody.py` binds one guarded worker's fixed private claims
and output paths into the existing guarded-tier receipt before dispatch. Read
and sync those files only after positive owned-worker settlement, including on
nonzero exit; unknown settlement keeps the awaiting marker and recovery fence.
Hashes and byte counts describe local custody, not valid JSON, successful
collection, complete source coverage, canonical ingest or database retention.
The opt-in `guarded_evidence.py` lane separately revalidates and appends a sealed
receipt through the existing writer; `guarded_fence.py` blocks automated evidence
writes while its exact receipt requires reconciliation. Preserve the distinction
between canonical worker outcome, local settlement and remote database outcome.
Unknown local cleanup still needs existing owner recovery. Read README.md
"Targeted enrichment evidence and recovery" before changing these boundaries;
AGENTIC-5040 still gates production writer activation.

Draft collector-capture export is limited to retained collector v1/v2. It reads
one ordered capture with a 10,002-row query limit, accepts at most 10,001 rows
and a 32 MiB response, then verifies rows, replanning, numeric maps and source
policy before using the shared private-export publisher. That publisher creates
an exclusive mode-0700 directory and mode-0600 files. Projected captures export
only projected data. Draft witnesses bind original pointers for finite integral
floats represented in scientific notation; they permit exact restoration after
JSONB normalization without inferring meaning or units. Legacy captures lack
that witness and may still refuse reconstruction. Any exact-digest mismatch must
refuse, not assert reconstruction. This draft is not deployed; separate production acceptance is required; it proves no original wire bytes, completeness, freshness
or canonical promotion.


Source-owned observation projection profiles live in `projections/`; read its
scoped AGENTS and README before changing any explicit observation policy.
