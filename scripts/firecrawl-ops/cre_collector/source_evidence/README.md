Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Private source evidence

Local verification is separate from deployment and production acceptance; see
[PR #363](https://github.com/Agentic-Assets/firecrawl/pull/363).

## Draft guarded enrichment custody

The applied guarded enrichment tier supplies its worker an exact private
`enriched.json` path and records `claims.json`/`enriched.json` custody intent in
its existing receipt before dispatch. The worker refuses occupied or unsafe
destinations before database claims and writes claims exclusively with mode
0600. Ordinary direct enrichment runs use unique private directories instead
of second-resolution shared names. All targeted collector output is private
and exclusive, including empty and bespoke-only artifacts.

After positive owned-worker settlement, the parent records bounded file hashes
and byte counts, including when the worker returned nonzero. File and directory
fsync precede the custody receipt. Missing output is recorded as absence;
unknown settlement does not permit a file read. Existing recovery fencing and
canonical queue behavior remain in force. A post-claim filesystem failure can
still leave claims for the existing reclaim mechanism.

The custody marker alone describes local files, not JSON validation, append
admission or a database write. The opt-in guarded evidence lane below reuses
the existing foreground writer and exact-plan replay on the same receipt;
targeted output does not pass through full-source refresh gates or introduce
another queue, manifest or scheduler. AGENTIC-5040 separately gates production
credentials and activation; local verification does not establish deployment.

This is a separate unverified evidence store. It does not promote listing fields, establish publisher authenticity, determine listing status, store original binaries in the evidence database, or write OM facts. Draft document acquisition may retain local private binary copies as described below. Artifacts and results may contain confidential material; keep their parent directories private. The CLI creates new output files exclusively with mode 0600 and prints only counts and the plan digest.

From the collector directory, build a plan without contacting a database:

```sh
python3 -m source_evidence.cli plan --artifact /private/artifact.json --source-key cbre --out /private/plan.json
```

For a Buildout observation artifact also provide `--observed-at 2026-10-28T12:00:00Z`; `--external-id` is an optional caller assertion for document evidence. Review the entire plan and source-retention policy. For approved private retention, prepare a private admission JSON with `schema_version` `cre-source-evidence-admission/v1`, `plan_sha256` matching the plan, `purpose` `private_unverified_evidence`, `data_scope` `public_listing_source`, `content_policy` `reviewed_for_private_retention`, and `source_keys` containing exactly the plan source key. JLL remains refused without its source-specific withheld-content adapter.

Draft offline candidate discovery accepts an existing evidence plan:

```sh
python3 -m source_evidence.cli discover-documents --plan /private/plan.json --out /private/candidates.json
```

It is intended to validate the plan and its existing source policy, group exact
document URLs while retaining their source occurrences, and identify known
document channels and source-labeled links. Relative links use only a nearest
explicit page-context URL as an unverified base. Candidate discovery does not
prove accessibility, publication, permission, document completeness, or a
physical-property association. It makes no network or database call and does
not download or extract documents. The command is a draft, not deployed; separate production acceptance is required. The draft refresh and series hooks described
below are explicit opt-ins, disabled by default; manual extraction starts with
an already acquired local file as described in `../document_text/README.md`.

Draft discovery also recognizes document suffix hints in scraped links and
captured URL-bearing attribute values, and explicit document labels, MIME types or download
attributes in retained links. It retains original references, source labels and
unverified claim witnesses. Scraped links keep sibling page-context clocks and
the collector's qualification result as unverified observations; neither is a
fresh document download clock. Relative-only references remain excluded by the
acquisition policy; previews are not assumed to be original documents. These
discovery additions are subject to separate production acceptance; suffixes and MIME
types do not guarantee that AnyDoc can convert a file.

Draft discovery parses complete captured JSON and JSON-LD script text as bounded
JSON, never as executable code. An occurrence retains both the original script
text pointer and a separate pointer within its decoded JSON. Incomplete or
unverified script text and truncated or unverified page references produce
explicit exclusion diagnostics; they are not acquisition targets. For older
page contexts without per-field integrity, completeness is inferred only when
the page explicitly reports zero omitted characters. Source and credential
policies still apply to decoded data. Exceptional JSON numeric literals are
retained in a bounded pointer map with explicit null-placeholder encoding rather
than silently rounded or used to discard unrelated document URLs. This behavior
remains unverified.

Draft acquisition-policy preflight adds a separately bounded, operator-asserted
selection from an existing plan and candidate manifest:

```sh
python3 -m source_evidence.cli plan-document-acquisition --plan /private/plan.json --candidates /private/candidates.json --policy /private/policy.json --policy-sha256 POLICY_HASH --expected-db-target-sha256 TARGET_HASH --collector-git-sha COMMIT --out /private/selection.json
```

The intended preflight rederives the candidates from the source plan and binds
the selection to the plan, manifest, policy, source, target and collector
commit. The explicit policy specifies registered sources, exact allowed hosts,
document and byte limits, deadlines, redirect limits and original-file
retention choice. Source-scoped absolute HTTP(S) candidates on standard ports
are selectable without rewriting the observed URL.
This is an operator assertion, not publisher permission or proof of public
access. No DNS, network, download, extraction or database request occurs. A
transport must validate public IP addresses on every request and
redirect hop and enforce source-specific access and visibility rules. Source
observation clocks must not be relabeled as download times; preserve precise
actual acquisition timestamps in a separate receipt. The local AnyDoc
provenance interface accepts UTC timestamps with optional one to six fractional
digits and preserves them verbatim. A selection is not a
completed download or extraction. This draft is not deployed and requires
separate production acceptance.

Draft `document_fetch` and `document_acquisition` provide a bounded direct
transport and a separate receipt validator, used by the draft opt-in refresh and
series hooks. The transport checks the allowed host and public destination for
each request and redirect, and requires an external worker to enforce the
absolute deadline. The draft one-shot supervisor binds a private job by hash,
limits the child to the document deadline plus five seconds of startup grace,
and validates the private receipt and body digest on readback. A timeout or
worker failure is ambiguous: the remote request may already have occurred.
There are no automatic retries. The transport accepts standard-port HTTP(S)
and identity content encoding, and refuses unsupported or ambiguous transfer
framing. The validator rederives selection and binds complete response
bytes to their digest, size, URL and actual acquisition clock. It preserves
source occurrences separately; publisher identity and visibility remain
unverified. Receipt metadata alone does not prove redirect locations, aggregate
budget enforcement, original-file retention, or that extraction actually ran.
Extraction binding is explicitly caller-asserted and does not rerun a parser.
No cumulative run budget is enforced by the one-shot supervisor itself. The
draft foreground library below connects it to local AnyDoc. The hooks are
disabled by default, not deployed; separate production acceptance is required.

Draft `document_pipeline.run` explicitly connects a rederived selection and
sealed source plan to the supervised fetch and local AnyDoc adapter. Its caller
must hold the existing foreground owner lock and provide the existing attempt
manifest and durable save callback. It allows one selection per source attempt;
draft version-2 selection combines multiple native pages. The existing
attempt records intent, reservations and outcomes. Reservations conservatively
charge the maximum body bytes delivered to the application plus one oversize
probe byte, including failed and unknown attempts. They do not bound network
bytes, buffered prefetch, HTTP headers or transfer framing.
The reserved phase includes fetch startup and reap grace and converter timeout
and reap grace. These are not a whole-host or native-I/O deadline or a
native-memory limit. Owned-process settlement is checked before publishing a
conversion completion marker; an unknown cleanup outcome retains the parent
recovery barrier. Descendants that escape into a new session are outside that
guarantee.

The bridge rechecks the private job, receipt, actual acquisition clock, body
hash and size before conversion. It privately seals acquisition context, AnyDoc
artifact, binding and evidence plan before policy-aware cleanup of the two
rechecked local original-body copies. The enriched artifact and plan header carry
all original candidate occurrences and listing relationships; the unchanged
converter output is saved separately as `converter-artifact.json`. Each body
cleanup has its own saved intent and removal state. A sealed 401, 403, 429 or 451
response stops the selection without another fetch. `keep_unresolved_until_review` retains
copies for validated failed or OCR-needed extractions; ambiguous or interrupted
work preserves files and requires reconciliation, with no automatic resume.
The draft opt-in hooks add no new writer pump, queue, scheduler or canonical
change. They require separate production acceptance. Their metadata and text do not establish publication permission,
listing identity, complete coverage or extraction accuracy.

Draft checkpoint-refresh and series integration accepts explicit
`--source-document-policy`, `--source-document-policy-sha256`, and
`--source-document-node` alongside automatic source evidence. It binds policy,
source scope, target, revision and Node path into the series and child; paused
or uncertain document work requires reconciliation before another attempt.
Serial and pipeline collection both defer document work until collection
returns and its workers settle, before canonical source-failure checks so
accessible partial evidence can be processed; the foreground owner lock remains
authoritative. Resume binds source scope, code, target and the persisted source
settlement marker. Revalidated seals and durable checkpoint saves govern recovery;
unknown process settlement or save outcomes stop progress.
An exact known no-data outcome remains incomplete, but does not by itself
require document reconciliation; an otherwise authorized source retry may make a
new source attempt. The old document attempt is not restarted.
This integration is not deployed, requires separate production acceptance, and does not resolve the separate
credential admission gate.

Draft automatic source-evidence policy v3 separately admits document text by
source and the `local_anydoc_public_listing_v1` format. Eligible sealed outcomes
use the existing private event store and guarded foreground append pump. A
pending, refused or uncertain append blocks a new series attempt; an uncertain
write requires explicit exact sealed-plan replay in refresh, without downloading
or extracting again. Version 1 and 2 policies do not admit document-text append jobs.
This adds no canonical listing or OM fact writes and remains disabled by default,
unverified and not deployed.

Draft evidence SQL execution uses an owned client process with a 45-second
deadline, a 72 MiB SQL input cap, caller-selected stdout up to 32 MiB, and a
64 KiB stderr cap. Output is bounded incrementally. The input cap accommodates
hex encoding of a 32 MiB plan plus bounded statement and guard overhead. This
subprocess path requires POSIX/WSL and refuses unsupported platforms before
dispatch. Local process settlement and remote
transaction outcome are separate: a timeout, interruption or client failure
does not establish rollback. Uncertain local cleanup preserves the foreground
recovery barrier; an uncertain append keeps its exact-plan replay requirement.
Foreground evidence saves persist the checkpoint and then check sticky CPU and
memory guard trips; terminal and error publication uses the ordinary save path.
These changes are not deployed; separate production acceptance is required.

Draft `document_seal.read` verifies an exact private file set, canonical bytes
and an optional expected marker without rerunning extraction. The caller must
rederive the acquisition context or authenticate it against a trusted prior
seal. The conversion completion marker is published after the parent observes
and validates converter completion; an artifact alone is not that marker.
No-spawn conversion replay requires the original fetch and conversion input
copies still present and rechecked. Once originals are deleted, seal readback
cannot freshly hash them. These recovery primitives remain subject to separate production acceptance.

Draft version-2 document selection combines ordered source-page plans from one
attempt into one bounded selection. Exact matching URLs share a fetch while
each occurrence retains its originating plan, capture, source clock, labels
and identity witnesses. These associations do not merge properties or establish
identity. The selection uses one serial budget across the batch; its source
plans must all remain sealed and bound to the attempt. This draft requires
separate production acceptance and does not activate regular collection.

Draft `document_recovery.sealed` uses the existing foreground owner lock and a
trusted saved attempt to revalidate sealed results without network requests or
conversion. It preserves reservations, refuses to restart acquisition, and can
reconcile selected original-copy cleanup. A missing persisted seal marker stays
unresolved even when files exist; an absent pending copy does not establish who
removed it. This API is subject to separate production acceptance.

Existing single-source collector artifacts can be wrapped offline before planning:

```sh
python3 -m source_evidence.cli convert-collector --artifact /private/rejected.json --source-key svn --out /private/retained.json
python3 -m source_evidence.cli plan --artifact /private/retained.json --source-key svn --out /private/plan.json
```

The wrapper retains every emitted listing, including duplicate or incomplete records and supplied `raw_data`, plus all artifact metadata and errors. It refuses mixed or mismatched sources, and uses the artifact's run-finished clock, not a per-record observation time. Its digest binds canonical JSON, not the original file bytes. Collector records have already passed through source mapping and pruning; this does not recover discarded HTTP responses, prove completeness or establish source acceptance. Empty artifacts with explicit source entries are retained. Conversion and planning are local; private retention admission and database authorization remain separate.

The draft version-2 collector representation separates each nonempty
`sources[].retainedObservations` entry into a `source_observation` fact with its
original `sourceIndex`, `observationIndex`, and value. Listing `source_record`
facts remain unchanged. Ordered `observationGroups` in the header preserve the
grouping needed to reconstruct the complete collector artifact, including
exceptional numeric literals at their original JSON pointers. Exact
reconstruction is required. Version 1 remains unchanged.

Listings and retained observations share the 10,000-record ceiling and any
tighter source-policy `max_records`. The envelope remains limited to 16 MiB,
each fact and header to 3.5 MiB, and the plan to 32 MiB. An oversized individual
observation or unrelated oversized metadata is still refused. This draft is
not deployed; separate production acceptance is required. It does not change canonical gates or establish complete source coverage.

An in-progress Foundry change intends to carry already parsed, unqualified held
observations in `sources[].retainedObservations`, so the existing private
evidence bridge can settle the artifact before canonical gates. An all-held
full run is intended to write a normal-shaped artifact with
`runMeta.evidenceOnly=true` and exit nonzero; a mixed run can retain held
context without changing accepted counts. Canonical ingest and monitor
entrypoints are intended to refuse evidence-only artifacts. This workflow is
not deployed; separate production acceptance is required. Typed access to
partial observations does not resolve uncertain identity or status, establish
source acceptance, or claim complete raw-response coverage. On a strict
identity failure, the intended `RetainedSourceError` preserves that failure and
carries held context and otherwise accepted conflicting parsed rows as
unqualified `sources[].retainedObservations`; it does not rekey, merge or
activate them. Evidence-only output is intended to be written exclusively as a
private mode-0600 file, without replacing an existing destination, and to exit
nonzero. The existing generic private evidence context bridge may retain it
under its admission rules.

The shared Python `cre_artifact_scope.require_canonical_artifact` guard is
intended for ingest main and its freshness guard, the monitor loader and no-op
reader, and gate counting before canonical database operations. The marker is
not authentication; unmarked artifacts still require the usual canonical
gates. These changes require separate production acceptance.

Admission scans HTTP(S) URL tokens in text and object keys for known JLL hosts and credential-bearing query keys, including the reconstructed bounded document text across chunk boundaries; it is not a general secret detector. Conflicting nested declarations of protected source keys are refused. Avison collector evidence is checked against its source-owned asking-price visibility control and projection, including control witnesses in capture context. Withheld records or capture context that the projection would change are refused, including malformed or contradictory controls; use a source-owned projected artifact before requesting admission. Independent rent, cap rate and NOI remain eligible. Other Avison evidence shapes need an adapter before admission. Local planning alone does not establish that any historical Avison plan is safe to retain.

The foreground checkpoint and series runners optionally retain settled collector artifacts using a separate operator-asserted recurring policy. This is disabled unless all four options are present: `--source-evidence-policy`, `--source-evidence-policy-sha256`, `--source-evidence-env-file`, and `--source-evidence-target-sha256`. The policy is a private JSON object with exact fields `schema_version` (`cre-automatic-source-evidence-policy/v1`), `purpose` (`private_unverified_evidence`), `data_scope` (`public_listing_source`), `format` (`collector_artifact_v1`), `collector_git_sha` (exact clean 40-character commit), `target_sha256` (the selected database URL fingerprint), `source_keys` (explicit allowed registered source keys), `max_artifact_bytes` (at most 16 MiB), and `max_records` (at most 10,000). Pin the policy file's byte SHA-256 in the command. The policy expresses operator authorization for the specified recurring source class; it is not a per-plan human review or publisher authentication. JLL remains excluded, and existing content and Avison controls apply. Credential-labelled fields refuse the entire capture; unknown fields are retained unchanged. The scanners do not prove content has no secrets or withholding restrictions.

The coordinator seals exact private artifact bytes and the reconstructed plan under its existing run directory before source validation and gates. Exceptional numeric literals are retained in a JSON Pointer map; the numeric-aware canonical digest retains those literals while normalizing ordinary JSON representation. It records capture, policy, target, transport, generation, attempt and hash bindings in the existing attempt. A pending plan is appended only from the foreground serial lane, never a pipeline polling callback. Remote database URLs require `sslmode=verify-full`, an absolute explicit `sslrootcert`, and `gssencmode=disable`; the CA bytes and transport settings are hashed and rechecked. Local Unix-socket fixtures are exempt from TLS. This binds the client connection configuration; it does not prove encryption beyond a pooler.

The dedicated database login and writer role must pass the enforced role-attribute, membership, and direct ACL/ownership dependency checks before each append. This code creates no role, grant or schema. Configure and verify the target and writer separately. Evidence failures and uncertain commits pause subsequent evidence writes and remain distinct from canonical source outcomes. Resume with identical bindings; `--source-evidence-replay-unknown` on an opted-in serial checkpoint resume explicitly replays only the sealed exact uncertain plan. Existing pipeline resume restrictions remain in force, so reconcile uncertain pipeline outcomes separately. A source process that produces no artifact is recorded as such; this path cannot recover data discarded inside an adapter or prove capture completeness.

Once the separate database migration, roles, permissions and a specific write are authorized, bind an explicit environment file and target fingerprint:

```sh
python3 -m source_evidence.cli append --plan /private/plan.json --admission /private/admission.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT --apply
```

The append is one transaction using `cre_source_evidence_writer` and the append function. The target fingerprint identifies the URL target, not the database's identity beyond that URL. Credentials stay out of command arguments. The function accepts exact retries and rejects divergent reuse. A timeout, connection failure or interrupted client can leave the commit outcome unknown. Reconcile against an authorized reader's database readback of the exact capture ID and item numbers; this CLI's text search is not a complete capture readback. When appropriate, replay the same unchanged plan and admission against the same target; do not alter rows or assume the failed client means rollback. The reviewed admission is an operator attestation, not cryptographic identity or publisher authentication.

Search requires the separate reader role and uses PostgreSQL `simple` full-text search on extracted text chunks only:

```sh
python3 -m source_evidence.cli search --query 'industrial warehouse' --mode phrase --limit 10 --out /private/results.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
```

Results are bounded to 25 chunks, not whole documents; a phrase crossing chunks will not match. Search does not prove completeness and content should be reviewed before disclosure. Queries may appear in the local process arguments. No command here applies DDL, downloads sources, modifies listing lifecycle, or changes scheduling.

Structured facts use PostgreSQL JSONB containment and a separate GIN index. For
example, an owner-only query file containing
`[{"kind":"document_outcome","value":{"extraction":{"status":"ocr_needed"}}}]`
finds retained OCR-needed outcomes without treating them as complete documents:

```sh
python3 -m source_evidence.cli search-facts --contains-file /private/query.json --out /private/matches.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
python3 -m source_evidence.cli get --capture-id CAPTURE_ID --item-no 0 --out /private/event.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
```

Fact search returns bounded match metadata. `get` returns one complete event,
including its ordered facts and optional text, into an exclusive private file.
Each text chunk records its ordinal, total count and original full-text digest;
verify all ordinals and the reconstructed UTF-8 hash before claiming complete
reconstruction. Chunk windows are not page citations. Original document bytes
and publisher identity are not independently verified by stored text hashes.

Document Markdown is retained source text, separate from normalized listing
facts. Preserve useful structured source facts alongside it, including values
without a recognized listing-field mapping. The document artifact records the
caller-asserted source URL and observation time, detected format, parser
provenance, and source and text SHA-256 values. Evidence chunks retain their
order and full-text digest; extraction coverage remains unknown. Extracting all
available text alone would not prove financial-table layout or value fidelity.

The `export-document-text` reader accepts `--capture-id`, `--out-dir`,
`--env-file`, and `--expected-db-target-sha256`. It validates chunk order and
full-text digest and uses `.txt` for plain text and `.md` for Markdown.

Acquisition policy v2 may opt into `pdf_complement` with mode
`poppler_pdf_text_v1` and an absolute, regular executable path in
`pdftotext_path`. An extracted, OCR-needed, or unsupported AnyDoc PDF gets a separately sealed Poppler
attempt and relationship, and independent evidence append checkpoints. Exact
replay handles an uncertain append without re-extraction. Poppler is text-only,
does not perform OCR, and does not change AnyDoc's outcome; both parser outputs have unknown
coverage. The feature is disabled by default and requires production acceptance.
Time and output are bounded; native memory and parent-path symlinks are not.
Before starting Poppler, the pipeline seals the AnyDoc attempt and persists its
marker. If Poppler settlement is uncertain, recovery can authenticate that
primary seal but leaves the complement unresolved and does not remove originals
or restart conversion. The `keep_unresolved_until_review` policy retains input
copies for failed or partial complementary output as well as failed AnyDoc.

## Native JSON response retention

The draft `export-collector-capture` reader is intended to recover one complete
retained collector v1 or v2 capture. It reads in order with a limit of 10,002
rows, accepts at most 10,001, and bounds the response to 32 MiB. Before writing,
it verifies row ordering, replans the capture, and checks context, numeric maps
and source policy. Its shared private-export publisher creates an exclusive new
mode-0700 directory containing mode-0600 `collector.json` and `metadata.json`.
The reconstructed collector JSON is a canonical semantic representation with
exceptional numeric literals restored. A projected capture exports the
projected representation, not removed source bytes. This readback does not
prove original wire bytes, source completeness, freshness or canonical
promotion. Document-text export uses the same private publisher.

This command and the draft numeric witness are not deployed; separate production acceptance is required. New collector captures can record bounded original
JSON-pointer witnesses for finite integral floats whose Python representation
uses scientific notation, such as `1e+20`. The reader uses those witnesses to
restore the representation if PostgreSQL JSONB renders the value as an integer,
then checks the complete reconstructed digest. The witness makes no inference
about a number's meaning or units. Legacy captures lack this witness and can
still fail exact reconstruction after JSONB numeric normalization; a digest
mismatch must refuse export. Support for every capture is not established.

Opted-in checkpoint attempts record an `awaiting_artifact` marker before worker
dispatch and use an attempt-specific temporary path. A settled serial resume
reconciles that path or its rejected-artifact destination before another worker
can replace it. Missing, unsafe or conflicting artifacts remain explicit
incomplete outcomes. This uses the existing manifest and ownership barrier;
it does not add a background worker or make pipeline resumes supported.

A source feed may contain useful fields that the listing mapper never emits. The
local response converter retains a selected JSON array and its surrounding
response context without requiring addresses, listing objects or unique IDs:

```sh
python3 -m source_evidence.cli convert-response --body /private/response.json --receipt /private/response-receipt.json --array-pointer /data/publicPosts --out /private/observation.json
python3 -m source_evidence.cli plan --artifact /private/observation.json --source-key nai-global --out /private/response-plan.json
```

The receipt has exactly `sourceKey`, `requestUrl`, `finalUrl`, `method` (`GET` or
`POST`), `status` (`200`), `startedAt`, `finishedAt` (UTC RFC3339), `bodySha256` and
`bodyBytes`. Conversion checks the actual supplied bytes against that receipt;
HTTP identity, response clock and public visibility remain caller assertions.
It makes no network or database call. Append uses the same separately bound
private-retention admission, target and restricted store as other evidence.
JLL and raw Avison responses remain subject to their source-owned controls.
An optional private `--capture-context` JSON file (at most 64 KiB) retains the
request query, variables, scope and other useful caller-supplied capture context;
it is included in the stored header and content-policy scan.

The observation records every selected array value in order, including duplicates,
primitives and nulls. An explicit placeholder and RFC6901 pointer preserve its
location in the complete response context. Numbers that would lose precision,
overflow or underflow in Python, signed zero, and exceptionally large integers
use an explicit pointer-to-numeric-literal map with null placeholders. The map
is retained with the capture header. Reconstruct the full observation before
interpreting a placeholder; it is not a missing source value. `reconstruct_json`
restores those numeric JSON values and their types, but does not restore original
whitespace or byte formatting. The original body hash cannot be reverified from
only the reconstructed representation. The format records this limitation.

This is an acquisition adapter, not an automatic source-feed tap. Checkpoint
automation currently retains emitted collector artifacts; adapters that discard
a response before emission still need source acquisition integration. Receipt
metadata cannot prove complete inventory or authorize canonical record matching.

## Version 2: native acquisition and explicit map projection

The optional `cre-automatic-source-evidence-policy/v2` keeps the version-1 fields
and additionally requires `collector_projections`, `native_source_formats`, and
`max_native_pages` (1–128). The dictionaries must name sources already listed
in `source_keys`; the supported entries are:

```json
{
  "collector_projections": {"faris-lee": "faris-google-map-tile-query/v1"},
  "native_source_formats": {
    "nai-global": "nai_public_posts_v1",
    "avison-young": "avison_sharplaunch_website_v1"
  },
  "max_native_pages": 128
}
```

Empty dictionaries disable those features. Version-1 policy behavior remains
unchanged. The existing policy SHA, exact clean collector commit, database target
and transport bindings still apply. This configuration authorizes a source
format; it does not certify publisher identity or complete inventory.

### Draft Buildout parsed inventory capture

The draft `buildout_inventory_parsed_v1` format requires explicit version-2
policy opt-in for one of 28 registered sources and 29 feeds. SVN and Lee each
have one registered feed; Franklin Street has separately identified sale and
lease feeds. The original 25 native receipt shapes and scope hashes stay unchanged.
It uses the same
settled native foreground lane and existing policy review, credential admission,
JLL and Avison controls. Faris collector map projection does not apply to this
format; credential-bearing native bodies refuse automatic admission.

Capture retains the collector's parsed JavaScript JSON representation, not raw
HTTP bytes or original numeric precision. Capture time is separate from the
nullable source observation clock, which can be older for a generation-cache
page. HTTP status and final URL are not asserted. A memory-cache hit creates no
fresh capture. The draft adds no source fetch and does not change listing parser,
reconciliation, status or source-acceptance gates.

Before parsed-data proof hashing, native publication, or generation-cache
writing, the shared exact JSON-value guard refuses values serialization would
change, including non-finite numbers and negative zero, and structures over its
100,000-node or 128-depth limit. Refusal is explicit and is not a transport
retry; already in-flight requests may still settle. This guard cannot restore
original numeric lexemes or repair values already lost in older cache files.
Collector freshness metadata is held separately from source page and row data.

The attempt limits are 128 pages and 128 MiB; a parsed body is limited to
16 MiB, each fact to 3.5 MiB, an observation to 16 MiB and a page to 10,000
records. Capture failures stop the capture-enabled path rather than authorize
fallback or recovery. These draft changes are not deployed; separate production acceptance is required; they establish no complete inventory, freshness, identity or
publisher-authentication claim.

### Avison native website feed

The existing website GET can capture its decoded text before JSON parsing,
listing mapping, deduplication or detail-page checks. The shared publisher
keeps raw pages in the private owned attempt directory with the same finite
quotas and single-worker custody as NAI. Team-member requests are excluded.
The integration key, request headers and key-derived commitments are excluded;
publication fails with a redacted error if the body contains that key.

The coordinator projects source-withheld asking fields and monetary aliases
through `avison_native.py`, preserving unaffected original values and context,
including unknown fields, empty/null values, duplicate IDs and addressless
records. Independently labeled financial values are retained. Monetary spans
that remain ambiguous carry an explicit unresolved marker. A bounded receipt
contains changed JSON pointers, actions and reasons, never removed values.
These source-specific rules do not relax the existing collector-format guard.

`cre-avison-native-observation/v1` retains projected records, the projected
envelope, exceptional numeric literal map and projection receipt. Hashes and
capture identity bind only this projected representation. Original bytes cannot
be reconstructed or their hashes reverified from the database observation.
The raw page hash remains solely in local custody and checkpoint integrity
state. The transport exposes decoded text, not original HTTP bytes; exact
HTTP status and final URL are unavailable. Its receipt therefore states
`success_status_unobserved` and preserves the attempted registered URL and
collector observation clocks, with no fabricated status or final URL.

Accessible JSON without the expected `items` array is projected under
`unmappedResponse`, with `responseEncoding: wrapped_unmapped_json` and zero
asserted listing records. The observation still preserves that useful context.
Invalid JSON stays in private local custody with an explicit diagnostic.
Native settlement and replay use the existing foreground evidence lane even
when canonical collection fails or emits no artifact; they never certify full
inventory, matching, normalization, source acceptance, lifecycle or scheduling.

### NAI public feed

For NAI, the coordinator creates one private directory per owned attempt and
supplies its path and limits to the source worker. Each actual publicPosts
response is captured before filtering, deduplication or mapping, with its exact
request, Fetch-delivered bytes, worker's response observation clock and hash. An immutable
entry's identity includes its entire serialized payload, so later conflicting
responses survive and exact replay is idempotent. Cache hits reuse the original
observation. The source uses the existing query, pagination, concurrency and
retry policy. Fields not requested by that GraphQL query are outside this
capture's coverage.

The producer enforces a maximum of 128 entries and 128 MiB of serialized entry
bytes, with the policy's potentially tighter page limit. Publication is serialized
within the single Node worker owned by that attempt. The internal single-writer
assertion is supplied automatically by the coordinator, not a human approval or
an OS lock; sharing a directory across independent processes is unsupported.
The POSIX ownership and filesystem requirements apply on macOS and Linux/WSL;
this does not establish Windows-native parity.

After process settlement, the coordinator validates each entry and seals its
plan before source gates. Native jobs use the same foreground writer lane as
collector jobs. Native and collector uncertain transactions are reconciled before
ordinary pending writes, with native exact replays first on ties. Explicit
replay-only mode can reconcile an uncertain native write while preserving an
unrelated collector refusal; it does not unlock new writes under that refusal.
Serial recovery operates only after the owned-worker settlement barrier.
Pipeline resumes remain unsupported. No background worker or second work queue
is installed.

Valid JSON without a publicPosts array, including GraphQL errors or a failed
HTTP response, is retained as `cre-json-response-context-observation/v1`: its
complete JSON context and exceptional numeric literals are preserved, with an
explicit assertion that no listing array was selected. Such a capture has zero
record events and one context event; it is not evidence of zero listings or a
successful source fetch. Invalid JSON stays in the private page file with an
explicit diagnostic. Other bounds and content-policy failures remain visible.
The source IDs in a request are assertions, including dynamically discovered
IDs; the captured query alone does not independently verify office membership.

For manual whole-response conversion, use `--whole-response` instead of
`--array-pointer`. Selected-array version-1 conversion still requires HTTP 200;
the context-only format preserves the observed HTTP status without claiming
listing access or enumeration.

Faris projection removes only the observed `key` and `token` query parameters
from the exact supported HTTPS Google map-tile URL form. It preserves surrounding
text, labels, remaining parameters and unrelated facts. The receipt records
bounded JSON pointers, occurrence ordinals, removed parameter names and projected
URL hashes. It creates no original-content hashes that could act as guessing
oracles for removed low-entropy values. This is an irreversible representation;
a projected URL may not be fetchable and is not the original requested resource.
Preexisting independent source metadata and hashes are not universally scrubbed,
and this is not a general secrecy or publisher-authentication guarantee.

The map projection must be explicitly enabled. For a manual conversion:

```sh
python3 -m source_evidence.cli convert-collector --artifact /private/faris.json --source-key faris-lee --project-map-credentials --out /private/projected.json
```

Automatic projected sealing stores only the projected observation as its new
sealed input. Its representation, hashes and reconstruction contract are marked
as projected; it cannot reproduce removed source bytes. Ordinary collector
artifact custody remains separate. Existing credential and source checks run
after projection, and JLL/Avison restrictions are not relaxed.

## Effective database privileges

Automatic append now also checks specified effective capabilities, including
PUBLIC grants, on the session login and writer role before assuming the writer.
It rejects accessible unrelated security-definer routines, persistent table or
column writes, sequence changes and schema creation. Standard temporary-object
rights are outside this bounded check. System objects, invoker routine side
effects, concurrent grant changes and independent use of a credential are not
made safe by a client-side preflight. Passing it does not prove a universally
append-only credential.

Production automatic intake remains disabled pending the separate shared-database
privilege review tracked by [AGENTIC-5040](https://linear.app/agenticassets/issue/AGENTIC-5040).
This phase changes no shared production ACL, creates no login, provisions no
credential and enables no scheduler. Private source acquisition and canonical
source acceptance retain their separate boundaries.

## Literal text search in facts

After the separately governed fact-search index migration is applied, the reader
can search the PostgreSQL JSONB text representation of retained facts:

```sh
python3 -m source_evidence.cli search-fact-text --query 'industrial warehouse' --limit 10 --source-key nai-global --out /private/matches.json --env-file /private/db.env --expected-db-target-sha256 FINGERPRINT
```

The query is a literal, case-insensitive substring, with a maximum of 512 UTF-8
bytes. Quotes, backslashes, control characters, `%`, `_`, and `!` are matched as
literal input in JSON serialization. This searches labels and values together;
it is not semantic search, relevance ranking, JSON path matching, or a promise
about original source formatting. PostgreSQL JSONB normalizes its representation.
Use `search-facts` for JSON containment and `get` for a complete event. Optional
`--source-key` and `--capture-id` restrict results. The limit is 1–25, ordered
by observation time descending, capture ID and item number. Results contain
metadata only and are written to an exclusive private file; facts are never
printed. A short or punctuation-only query may require a full index scan, and
queries have a 30-second database statement timeout. The existing document text
search and its phrase and chunk boundaries are unchanged.

## Draft Buildout document-reference withholding

Some Buildout inventory and collector records contain credential-bearing
document references. The ordinary evidence scanner refuses those captures.
The separate source-owned `buildout-document-reference-withholding/v1` rule
replaces only exact reviewed HTTPS Buildout document URLs at
`inventory[*].pdf_url` or `listings[*].brochures[*].url`. It preserves all other
fields and array positions, including missing/duplicate IDs and addressless or
closed rows. It does not establish canonical listing identity or change the
collector's canonical closed-row behavior. Missing, original null, malformed
and withheld references stay distinct through unchanged values and the receipt.
Unknown clean response shapes remain intact; sensitive content outside the
reviewed rule still causes ordinary admission refusal.

The entire URL value becomes null. A bounded receipt records its source and
record pointers, withholding reason, rule version and projected canonical
digest. Whole-URL withholding also removes any non-secret path information; it
is deliberately lossy. No original URI, token or digest of the removed source
bytes enters that receipt. A null alone cannot prove a reference existed, and
an attacker who can replace both a receipt and its self-reported digest can
claim an original null was withheld. Trust requires the independently saved
seal/checkpoint and policy bindings. A withheld pointer cannot overlap an
exceptional-number literal pointer.

For an offline collector conversion, explicitly select the rule:

```sh
python3 -m source_evidence.cli convert-collector --artifact /private/collector.json --source-key cawley-chicago --collector-projection-version buildout-document-reference-withholding/v1 --out /private/projected.json
```

The existing `--project-map-credentials` option retains its map-specific
behavior and is mutually exclusive with this selector. Conversion output is
private and only narrowly projected; it is not generally sanitized. Always
run ordinary planning and content-policy validation before admission.
Version-2 or version-3 automatic source policies may explicitly configure the
same rule in `collector_projections` for supported Buildout source keys. The
default and legacy map versions are unchanged; projections are not silently
combined. Other credential-bearing locations can still refuse a capture.

Native capture separately opts into `buildout_inventory_projected_v1` in
`native_source_formats`. The original private entry is checked against its
source, feed variant, page, sort, body digest and filename before projection.
The new `cre-buildout-projected-observation/v1` observation retains only the
projected canonical digest/length and loss receipt, alongside the original
source/capture clocks and scope. Replay re-reads and reprojects the private
entry against the trusted saved plan. Original file hashes remain local custody
markers and never enter the derived events. Existing
`buildout_inventory_parsed_v1` behavior is unchanged.

Reconstruction and hashes establish the projected representation only. Removed
URIs and original source bytes cannot be recovered from it; native input was
already parsed JavaScript JSON, not original HTTP wire bytes. No transformed
URL is a download target, and no document type, access, sharing permission or
complete inventory follows. There is no new acquisition or canonical-write
authority. Production activation and private evidence-target admission remain
separate requirements.

## Draft property-location selection order

Policy v4 requires `selection_order: property_occurrences_first_v1` and retains
the v3 PDF complement and sparse OCR settings. It stable-partitions existing
document candidates using eligible Transwestern property-location observations;
other candidates retain discovery order. Existing URL, host and absolute-source
reference checks still apply before the document cap. The manifest remains
unchanged and each selection or exclusion retains its original candidate index
and a priority class with its witness or null. Exact URL grouping retains every
source occurrence; an unselected candidate remains in the bound manifest.

Priority requires the same collector source record, a matching document or link
occurrence, consistent source URL, detail/body-hash assertions and timestamps,
and a live Transwestern detail within the recurring bound generation. Missing,
stale, malformed, conflicting or incomplete bindings supply no priority for
that occurrence. Independently validated property and footer occurrences can
coexist for one exact URL; the property witness does not erase the others.
Witnesses bind the collector artifact file hash, plan, capture, record pointer,
generation, attempt and role hash, but these are source custody assertions; they
do not independently authenticate the publisher or rehash unavailable original
HTML. Manual plans lacking recurring bindings receive no priority. This is local
draft behavior, not production acceptance; it does not admit a new URL, waive
access checks, create a writer or change legacy v1–v3 selection behavior.

Only v4 acquisition contexts add an optional `selection_binding` to the existing
private context representation. It binds the complete selection digest, order,
zero-based selected position and priority witness into the sealed context.
Archived-context validation rederives these fields from the source plans and
policy; a sidecar or a digest supplied alongside it is not independent authority.
Sealed readback still requires the caller's authenticated context and marker.
Legacy contexts remain unchanged. The foreground pipeline executes the selected
order, its checkpoint and budget digests bind it, and recovery performs no new
fetch or conversion. A selected position describes that array, not independent
evidence of network request order. The standalone budget API reserves selected
indices; it does not schedule work.

## Draft offline HTML landing observations

`document_landing.parse_document_landing` accepts bounded acquired HTML bytes and
an asserted final URL and returns a private observation containing a body digest,
visible text, metadata and raw anchor, iframe, turbo-frame, embed and object references with
element, attribute and source position. It does not validate the HTTP receipt,
classify access, resolve references, run JavaScript or discover candidates through
`document_candidates`. Complete JSON and JSON-LD script text is recorded
without execution;
other script, style, template and noscript contents are excluded. Incomplete JSON
scripts retain a completeness marker but no partial text, and are not discoverable
candidates. Complete scripts must parse as JSON and pass credential and source
policy checks before sealing. Scripts failing these checks are withheld and
marked, while other policy-safe page content can still be retained.
UTF-8 decoding uses replacement; this is not an exact text reconstruction.

The draft foreground pipeline recognizes a narrow HTML byte signature after an
unsupported AnyDoc outcome and attaches a separately sealed source-page context.
The existing receipt and candidate occurrences bind the original body and source
relationships. Readback and recovery check its seal marker, and source and
credential policies scan the retained observation before sealing. Oversized input
is refused; count and text limits and reference completeness are explicit. The
helper itself has no network or database behavior. This integration remains
disabled unless the existing document acquisition path is explicitly enabled.
The output is not proof of access, publication, listing identity or extraction
completeness. Any nested reference requires its own policy and acquisition checks.
The converter outcome remains unchanged; HTML page text is not represented as a
successful document extraction.

## Targeted enrichment evidence and recovery

Applied guarded enrichment can opt into the same automatic source-evidence
policy with `--source-evidence-policy`, `--source-evidence-policy-sha256`,
`--source-evidence-env-file`, and `--source-evidence-target-sha256`. All four
are required together; the evidence environment is separate from the canonical
queue environment. The worker must be locally settled before its private
artifact can be sealed. This retains the emitted artifact and makes no claim
about source completeness, canonical promotion, or lifecycle status.

The canonical shared owner lock serializes an owner-bound recovery marker for
one outstanding targeted evidence receipt. The marker is a recovery fence, not
a queue or scheduler. It is published before an automatic database append and
survives an uncertain database outcome. Checkpoint automatic evidence writes
pause while it exists; canonical ingest and monitoring are independent. The
separately authorized manual evidence CLI is outside this automatic fence.

After local owner settlement and any required existing manual owner recovery,
reconcile the exact receipt on the same clean code and policy binding:

```sh
python3 cre_guarded_tier.py enrich --source SOURCE --expected-sha SHA \
  --source-evidence-reconcile-receipt /absolute/private/run/receipt.json
```

Reconciliation revalidates the private receipt, claims, artifact, plan, source,
code, policy, target and transport, and replays the identical transaction. A
validated pending receipt can publish a missing pre-dispatch recovery marker
under the owner lock. A validated receipt durably recording an appended and
settled job can be reconciled again without a marker or another database call;
missing markers for other dispatched or uncertain transactions require manual
diagnosis. Reconciliation does not rerun enrichment. Unknown
local process cleanup still requires the existing
manual owner-recovery procedure; this command does not adopt an armed lease.
A database outcome can remain unknown even after the local process has settled.
The restricted writer and AGENTIC-5040 production credential-isolation gate
remain separate; this feature does not provision or activate production access.

### Shared map-widget projection

Explicit recurring policies can admit the observed `maps.googleapis.com/maps/vt`
projection for Cushman & Wakefield, NAI Global and SVN as
`google-map-tile-query/v1`.
Faris keeps its existing `faris-google-map-tile-query/v1` contract. Only literal
`key` and numeric `token` parameters on that exact HTTPS endpoint are removed;
other URLs and source content still pass the existing admission checks. Pointer
receipts identify the projection, not original source bytes, and a projected map
reference is not an approved download target. No source or policy is enabled by
this support.

### Read-only target catalog inspection

Inspect a candidate with a private database environment and its independently
verified target fingerprint. The optional `--login` selects an existing role to
assess; omit it to assess the session login. Names and catalog details are saved
to a new mode-0600 file, and only counts are printed.

```sh
python3 -m source_evidence.cli inspect-target --env-file /private/evidence-db.env \
  --expected-db-target-sha256 TARGET_HASH --out /private/target-inspection.json
```

The command does not assume a role, invoke application functions, read evidence
rows, or modify the database. It shares the automatic writer guard's effective
capability predicates and reports bounded hazard, extension and connectable
database lists with counts and truncation indicators. Other databases are not
inspected. Role attributes and migration-related object and extension metadata
are informational; they do not validate SQL 019/020 or role memberships. This
includes `fact_index_name_exists`, which checks name resolution rather than
the object's type, index definition or validity. The result
is a bounded catalog assessment, not proof of credential isolation, complete
permissions, migration readiness, or production activation. Server-side
restrictions, independent connections, system objects, invoker routine effects
and concurrent ACL changes require separate review.

Acquisition policy v3 explicitly adds `sparse_ocr` with mode `sparse_ocr_v1`,
strategy `no_text` (neither prior parser returned text) or `all_pages` (also
OCR native or mixed PDFs), and `max_pages` from 1 through 1000. It requires
the v2 Poppler complement and reserves the versioned OCR deadline before
fetch. Prior AnyDoc and Poppler results are sealed and saved before dispatch;
the final seal binds all three outcomes and independent append plans. Unknown
process or container cleanup blocks further work. Exact append replay uses the
sealed plan and does not repeat fetch or parsers. Under
`keep_unresolved_until_review`, failed or partial OCR, omitted pages or other
warnings retain originals. Coverage remains unknown; this is not deployment,
source permission, complete extraction, or a canonical database write.

After OCR process settlement and verified container removal, the coordinator
writes an exclusive completion marker binding the artifact, prior attempts,
receipt, policy, worker and container settlement. If interrupted before the
final seal, recovery may revalidate that marker, both retained input copies and
the prior seals, then seal the exact existing OCR artifact without starting a
process. Missing or invalid completion or settlement remains unresolved; the
existing budget reservation stays charged and no new acquisition is admitted.
Whitespace-only OCR text is discarded as non-content with an explicit warning;
its recorded page byte counts remain diagnostic while text offsets are zero.


Source-specific private page-observation projections are documented in
[`projections/README.md`](projections/README.md).

### Transwestern parsed inventory buckets

The explicit `transwestern_inventory_parsed_v1` native format captures each
successfully parsed bucket before identity checks, deduplication, or row caps.
It binds the transaction, bucket, and position in that transaction's request
order as producer assertions, and retains duplicate and partial rows. A non-array parsed value is kept
as whole-response context without an asserted listing array. Capture is disabled
unless the existing source-scoped native policy enables it; ordinary credential
and content scanners still apply. A capture failure fails the enabled path.

This is the JavaScript parsed and serialized value returned by `scrapeJson`,
not HTTP or SDK response bytes. Its parser may unwrap HTML, extract a JSON
fragment, or repair quotation marks; JavaScript numeric precision and
serialization apply. Capture time is a collector clock, HTTP status and final
URL are unobserved, and a request for `maxAge: 0` is not proof of source
freshness. Invalid or failed JSON parsing cannot be captured by this hook.
The existing native page, body, record, fact, and plan limits apply; a refusal
is not a complete capture. Retention provides no source admission, canonical
identity, lifecycle authority, production activation, or document access.
The shared exact JSON-value guard refuses non-finite numbers, negative zero,
non-JSON values, or structures beyond its 100,000-node and 128-depth bounds
before serialization; it does not reconstruct original numeric lexemes. The
saved 415-row sale fixture measured 5,811 nodes at depth 2 during the offline
proof, which is not a guarantee about other responses.

### Pyramid decoded inventory pages (prototype)

The explicit `pyramid_inventory_decoded_v1` source format privately captures each
successful Pyramid inventory response's collector-decoded UTF-8 text before
header, JSON, row or transaction validation. It retains asserted page, pass,
request URL, final response status, selected numeric headers and a collector
clock; redirect final URL, wire bytes and provider freshness are not observed.
The private storage codec is gzip plus base64, not HTTP content encoding.
The decoded capture ceiling is 16 MiB, stricter than the existing 32 MiB source
transport ceiling; enabling capture can therefore refuse a response the normal
collector otherwise accepts. Existing 128-file/128-MiB stored-attempt bounds
remain, and a typed publication failure ends that attempt rather than retrying.
Completed earlier files remain private for settlement; an unsettled worker must
still pass the existing recovery fence.

Valid array JSON preserves ordered rows and source fields with numeric witnesses;
valid non-array JSON is retained as unqualified whole context. Malformed decoded text is retained only in the private page: decoding and
admission refuse it because structured credential scanning cannot safely inspect
its keys. The private page remains available for diagnosis. Pyramid admission
also inspects bounded recursively JSON-encoded strings using the existing
credential-field policy, refusing when inspection bounds are exceeded. This
does not establish detection of every secret.
No result proves inventory convergence, access, source authority or canonical
listing admission. This format is opt-in and needs policy/custody review.

The explicit Poppler complement mode `poppler_pdf_layout_v1` uses `-layout`
and is bound to parser configuration during acquisition, replay and append.
The existing text mode and its serialized configuration remain unchanged.
Neither text mode establishes table fidelity or complete page coverage.
`keep_unresolved_until_review` follows converter outcome and configured OCR
omission indicators; unknown fidelity alone does not keep original copies.

### Docling JSON document supplement

Explicit acquisition policy v5 adds `docling_json` with mode
`docling_json_v1` and a bounded `max_pages`. It carries v4 selection and
existing Poppler and sparse OCR settings, and reserves additional phase time.
The foreground coordinator seals ordered priors, verifies owned settlement and
a completion marker, and creates a separate Docling append job. Recovery does
not spawn a converter. Exact UTF-8 JSON is chunked as document text and exported
as `document.docling.json`; unknown fields remain in the original bytes.

Admission strictly decodes JSON and applies existing content restrictions to
keys, strings and bounded nested JSON. This is not a universal secret detector.
Failure, partial or omitted page output retains originals when
`keep_unresolved_until_review` is selected; success does not prove fidelity.
`temporary_only` keeps its explicit behavior. This draft is not deployed and
requires separate production acceptance.

For v5, an interruption while OCR is in progress leaves both original files
in reconciliation. A verified settled OCR completion can be replayed without
spawning to create or verify the existing Docling-prior seal. Recovery remains
incomplete and does not create a final seal, append job, or start Docling.
