Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Private source evidence

This is a separate unverified evidence store. It does not promote listing fields, establish publisher authenticity, determine listing status, store original binaries, or write OM facts. Artifacts and results may contain confidential material; keep their parent directories private. The CLI creates new output files exclusively with mode 0600 and prints only counts and the plan digest.

From the collector directory, build a plan without contacting a database:

```sh
python3 -m source_evidence.cli plan --artifact /private/artifact.json --source-key cbre --out /private/plan.json
```

For a Buildout observation artifact also provide `--observed-at 2026-10-28T12:00:00Z`; `--external-id` is an optional caller assertion for document evidence. Review the entire plan and source-retention policy. For approved private retention, prepare a private admission JSON with `schema_version` `cre-source-evidence-admission/v1`, `plan_sha256` matching the plan, `purpose` `private_unverified_evidence`, `data_scope` `public_listing_source`, `content_policy` `reviewed_for_private_retention`, and `source_keys` containing exactly the plan source key. JLL remains refused without its source-specific withheld-content adapter.

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
unverified under the current test hold, not deployed, and has not been written
live. It does not change canonical gates or establish complete source coverage.

An in-progress Foundry change intends to carry already parsed, unqualified held
observations in `sources[].retainedObservations`, so the existing private
evidence bridge can settle the artifact before canonical gates. An all-held
full run is intended to write a normal-shaped artifact with
`runMeta.evidenceOnly=true` and exit nonzero; a mixed run can retain held
context without changing accepted counts. Canonical ingest and monitor
entrypoints are intended to refuse evidence-only artifacts. This workflow is
unverified under the current test hold and is not deployed. Typed access to
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
gates. These changes remain unverified and not deployed.

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

The planned `export-document-text` reader will accept `--capture-id`,
`--out-dir`, `--env-file`, and `--expected-db-target-sha256` for whole-document
recovery. It is under development and has not yet been verified. Until then,
use the existing event readback and verify chunk ordinals and the full-text
digest before claiming reconstruction.

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

This command and the draft numeric witness are unverified under the current test
hold and are not deployed. New collector captures can record bounded original
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
policy opt-in for one of the 25 registered source/provider tuples. SVN, Lee and
Franklin coordinate families are outside this draft's scope. It uses the same
settled native foreground lane and existing policy review, credential admission,
JLL and Avison controls. Faris collector map projection does not apply to this
format; credential-bearing native bodies refuse automatic admission.

Capture retains the collector's parsed JavaScript JSON representation, not raw
HTTP bytes or original numeric precision. Capture time is separate from the
nullable source observation clock, which can be older for a generation-cache
page. HTTP status and final URL are not asserted. A memory-cache hit creates no
fresh capture. The draft adds no source fetch and does not change listing parser,
reconciliation, status or source-acceptance gates.

The attempt limits are 128 pages and 128 MiB; a parsed body is limited to
16 MiB, each fact to 3.5 MiB, an observation to 16 MiB and a page to 10,000
records. Capture failures stop the capture-enabled path rather than authorize
fallback or recovery. These draft changes are unverified under the current hold
and not deployed; they establish no complete inventory, freshness, identity or
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
