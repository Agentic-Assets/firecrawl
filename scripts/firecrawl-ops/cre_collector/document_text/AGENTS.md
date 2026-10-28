READ ~/AGENTS.md FIRST (skip if missing).

# Local document conversion

Read `README.md` and parent collector guidance. Input is a caller-supplied local
file. Source URL is provenance and is never fetched. AnyDoc is pinned in the
package lock; conversion runs locally with hosted OCR disabled. Preserve
`ocr_needed` and failed outcomes. A successful Markdown result has unknown
coverage and is not a claim of page completeness, table accuracy, visibility,
listing identity, or source permission. Original bytes are not retained here.

The draft offline document-candidate discovery in `../source_evidence/` does not
download documents or invoke AnyDoc. Full-source runs do not automatically
extract linked documents. Discovery is unverified under the test hold; neither
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
multi-page candidates. There is no regular collector hook, CLI, database append,
writer pump or scheduler activation. These changes remain unverified under the
test hold and establish no publication permission, coverage or accuracy.

Draft recovery verifies the private seal and optional expected marker. No-spawn
conversion replay requires the parent-validated completion marker and retained
fetch and conversion input copies. An artifact alone is insufficient; after
original deletion, seal readback cannot freshly rehash it. Recovery is unverified.
`document_recovery.sealed` uses the existing owner lock and trusted saved attempt
without fetching or converting. It preserves budget, does not restart acquisition,
and leaves a missing persisted marker unresolved. Reconciled absence does not
identify who removed a copy.

Draft batch selection uses one serial budget for ordered source-page plans and
retains each occurrence's source association. A shared document URL does not
prove property identity. This behavior remains unverified under the hold.

No canonical database, OM writer, network downloader, or scheduler belongs in
this module. The private acquisition bridge is `../source_evidence/`. Private
deal documents retain EQUIRE's separate organization/deal scope. Tests run with
`npm ci --ignore-scripts` then `npm test` in a standalone no-hardlinks clone.
Never publish private source text or benchmark inputs. Parent path symlinks and
native converter memory are outside the module's current isolation guarantees.
