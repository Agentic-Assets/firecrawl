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

No canonical database, OM writer, network downloader, or scheduler belongs in
this module. The private acquisition bridge is `../source_evidence/`. Private
deal documents retain EQUIRE's separate organization/deal scope. Tests run with
`npm ci --ignore-scripts` then `npm test` in a standalone no-hardlinks clone.
Never publish private source text or benchmark inputs. Parent path symlinks and
native converter memory are outside the module's current isolation guarantees.
