READ ~/AGENTS.md FIRST (skip if missing).

# Local document conversion

Local verification is separate from deployment and production acceptance; see
[PR #363](https://github.com/Agentic-Assets/firecrawl/pull/363).

Read `README.md` and parent collector guidance. Input is a caller-supplied local
file. Source URL is provenance and is never fetched. AnyDoc is pinned in the
package lock; conversion runs locally with hosted OCR disabled. Preserve
`ocr_needed` and failed outcomes. A successful Markdown result has unknown
coverage and is not a claim of page completeness, table accuracy, visibility,
listing identity, or source permission. Original bytes are not retained here.

Format detection uses content first and local filename extension second. The
acquisition bridge names an extensionless URL `document.bin`; pinned AnyDoc
cannot identify signatureless CSV without an explicit format, and candidate
labels or MIME hints are not currently passed to the converter. A future hint
must be deterministic, source-labelled and bound across selection, conversion,
seal and recovery; content detection wins, conflicting hints remain unresolved,
and a source hint does not establish file type or permission. This is not yet
implemented or verified.

The draft offline document-candidate discovery in `../source_evidence/` does not
download documents or invoke AnyDoc. Draft refresh and series hooks explicitly
opt in to linked-document extraction and are disabled by default, unverified
and not deployed. Discovery is subject to separate production acceptance; neither
it nor a candidate URL proves public access, publication, or permission.
The draft acquisition-policy preflight is also offline and does not invoke the
adapter. Preserve precise actual download times in a separate receipt; do not
relabel original source clocks as download times. The provenance input accepts
UTC timestamps with optional one to six fractional digits and preserves them.
The draft `source_evidence.document_pipeline.run` library explicitly connects
the supervised fetch worker to this adapter under an existing foreground owner
lock, attempt manifest and save callback. It binds the source URL, exact
download clock, body hash and size, then seals private context, artifact,
binding and evidence plan before policy-aware cleanup of source copies.
Conservative failed and unknown reservations cover application-delivered body
bytes including a probe byte, not network bytes, buffered prefetch, headers or
transfer framing. Phase deadlines and grace do not bound all host activity or
native memory. Ambiguous
or interrupted work requires reconciliation without automatic resume. One
selection is supported per source attempt; draft version-2 selection combines
multi-page candidates. Draft refresh and series hooks add no new writer pump,
queue or scheduler. These changes remain subject to separate production acceptance and establish no publication permission, coverage or accuracy.

Draft recovery verifies the private seal and optional expected marker. No-spawn
conversion replay requires the parent-validated completion marker and retained
fetch and conversion input copies. An artifact alone is insufficient; after
original deletion, seal readback cannot freshly rehash it. Recovery requires separate production acceptance.
`document_recovery.sealed` uses the existing owner lock and trusted saved attempt
without fetching or converting. It preserves budget, does not restart acquisition,
and leaves a missing persisted marker unresolved. Reconciled absence does not
identify who removed a copy.

Draft batch selection uses one serial budget for ordered source-page plans and
retains each occurrence's source association. A shared document URL does not
prove property identity. This behavior remains subject to separate production acceptance.

Draft refresh and series opt-in binds a document policy, hash and Node executable to
automatic source evidence and its checkpoint. Preserve the bindings on resume;
do not start another attempt with uncertain document work. This is not deployed
and requires separate production acceptance without bypassing credential admission. Both serial and pipeline
paths defer document work until collection returns and workers settle under the
owner lock and before canonical source-failure checks. Resume binds source,
code, target and the source-settlement marker; seal revalidation and durable
saves are recovery barriers. Owned-process settlement precedes completion marker
publication; unknown cleanup keeps the parent barrier. Descendants escaping to
a new session are outside that guarantee. Reserved phase time includes fetch
startup/reap and converter/reap, not a native-I/O deadline.
Exact known no-data remains incomplete but need not block an otherwise authorized
new source attempt; the old document attempt is not restarted.

Draft automatic evidence policy v3 separately admits source-scoped document
text to the existing guarded private event-store pump; v1 and v2 do not. Pending,
refused or uncertain appends block a new series attempt. Reconcile an uncertain
write by explicit exact-plan refresh replay, without another fetch or conversion;
this grants no canonical listing or OM writes. It is disabled by default,
unverified and not deployed.

Draft evidence SQL uses an owned client, 45-second deadline and incrementally
bounded output: SQL input is capped at 72 MiB, stdout at the caller-selected
limit up to 32 MiB, and stderr at 64 KiB. Unknown local cleanup keeps the
foreground recovery barrier; local settlement does not establish remote rollback.
Foreground evidence saves persist before checking sticky CPU and memory trips;
terminal and error publication uses the ordinary save path. This is unverified.

No canonical database, OM writer, network downloader, or scheduler belongs in
this module. The private acquisition bridge is `../source_evidence/`. Private
deal documents retain EQUIRE's separate organization/deal scope. Tests run with
`npm ci --ignore-scripts` then `npm test` in a standalone no-hardlinks clone.
Never publish private source text or benchmark inputs. Parent path symlinks and
native converter memory are outside the module's current isolation guarantees.

The explicit `ocr.py` retry accepts only AnyDoc `ocr_needed` or failed
`unsupported` PDF artifacts bound to the original bytes. The default offline
Docling OCR and optional local Poppler text-only engine record separate parser
provenance and the original artifact hash. Poppler does not perform OCR; retained
partial output and converter diagnostics carry warnings and unknown coverage.
Never treat a retry as source, canonical or production acceptance.
