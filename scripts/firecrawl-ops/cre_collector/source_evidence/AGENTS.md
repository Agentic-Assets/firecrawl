READ ~/AGENTS.md FIRST (skip if missing).

# Private source evidence

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
remain refused. This draft is unverified under the test hold, not deployed, and
not written live; it changes no canonical gates or source-coverage claims.

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
limits apply. Capture failure stops the enabled path. This draft is unverified
under the hold and not deployed; no source admission or completeness follows.

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
and does not download documents or run AnyDoc. It is unverified under the
current test hold and does not change source admission or canonical data.

Draft `plan-document-acquisition` is a pure policy preflight over a validated
plan and rederived candidates. It produces a bounded source-scoped selection,
not a download, extraction, permission decision, or completed evidence write.
Operator-asserted exact host/source allowlists and limits do not authenticate
publishers. No DNS is checked by preflight; acquisition transport must
validate public IPs at each request and redirect hop and preserve actual
acquisition clocks separately from source-observation clocks. This draft is
unverified under the test hold and is not deployed.

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
activated. They are unverified and not deployed
under the hold; do not claim publisher identity, visibility, complete coverage
or extraction accuracy.

Draft seal readback checks an exact private file set and optional expected marker;
authenticate the context independently. Conversion replay requires a completion
marker published after parent validation and rechecks retained fetch and converter
inputs without spawning. An artifact alone is insufficient, and deleted originals
cannot be freshly rehashed by seal readback. Recovery is unverified under the hold.

Draft `document_recovery.sealed` runs under the existing owner lock with a
trusted saved attempt. It performs no fetch or conversion, preserves budget,
and may reconcile cleanup without inferring who removed an absent copy. Missing
persisted seal markers remain unresolved; recovery never rearms acquisition.

Draft version-2 document selections bind ordered source-page plans to one
attempt and one serial budget. Group exact URLs but retain every occurrence's
plan, capture, clock, labels and unverified identity witnesses; do not infer a
property merge. All page plans remain subject to seal validation. This is
unverified under the hold.

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
follow. This is disabled by default, unverified and not deployed.

Document export reconstructs the complete bounded capture, verifies ordered
chunks and text hashes, and writes exact Markdown plus provenance into a new
private directory. Keep unknown extraction coverage visible; a valid text
digest proves reconstruction integrity, not faithful extraction of every page.

Draft collector-capture export is limited to retained collector v1/v2. It reads
one ordered capture with a 10,002-row query limit, accepts at most 10,001 rows
and a 32 MiB response, then verifies rows, replanning, numeric maps and source
policy before using the shared private-export publisher. That publisher creates
an exclusive mode-0700 directory and mode-0600 files. Projected captures export
only projected data. Draft witnesses bind original pointers for finite integral
floats represented in scientific notation; they permit exact restoration after
JSONB normalization without inferring meaning or units. Legacy captures lack
that witness and may still refuse reconstruction. Any exact-digest mismatch must
refuse, not assert reconstruction. This draft is unverified under the test hold
and is not deployed; it proves no original wire bytes, completeness, freshness
or canonical promotion.
