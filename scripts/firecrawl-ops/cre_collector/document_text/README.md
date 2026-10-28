Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Local document text extraction

This independent adapter accepts a file already acquired by an authorized process. It never fetches the source URL and does not write to Supabase or the listing collector. Install its pinned dependency with `npm ci --ignore-scripts` in this directory, then run:

Full-source collection does not currently download or automatically extract
linked documents. The draft `source_evidence.cli discover-documents` command
identifies candidate links in a saved, validated evidence plan; it performs no
download or extraction. It is unverified under the current test hold and is not
deployment or source-access evidence. Manual AnyDoc execution starts with an
already acquired local file.
The draft offline `plan-document-acquisition` preflight can select candidates
under an explicit bounded policy, but it does not acquire those files or run
AnyDoc. It performs no DNS or access verification and does not establish
publisher permission. An acquisition receipt preserves precise actual
download times separately from the source observation time; the local
provenance interface accepts UTC timestamps with optional one to six fractional
digits and preserves the supplied timestamp verbatim. The preflight remains
unverified under the test hold and is not deployed.

The draft `source_evidence.document_pipeline.run` library connects supervised
private document fetches to this local adapter under an existing foreground
attempt, save callback and owner lock. It rechecks source bytes, hash, size,
final URL and precise download time, and seals private acquisition context,
artifact, binding and evidence plan before policy-aware original-copy cleanup.
Failed and unknown fetch reservations conservatively consume their full byte
cap plus one probe byte; phase deadlines and cleanup grace do not bound all
host activity or native converter memory. The byte cap covers body bytes
delivered to the application, not network bytes, buffered prefetch, HTTP headers
or transfer framing. Ambiguous or interrupted work keeps
its private files for reconciliation and never automatically resumes. One
selection is supported per source attempt; draft version-2 selection combines
native-page candidates. This library is not connected to regular collection, a
CLI, database append, writer pump or scheduler. It is unverified under the
test hold and does not prove publisher permission, coverage or text accuracy.

Draft recovery verifies an exact private seal and optional expected marker.
Conversion replay requires a completion marker published after the parent
validates converter completion, plus retained fetch and conversion input copies;
it does not spawn a converter. An artifact alone does not establish completion,
and a deleted original cannot be freshly rehashed from the seal. This recovery
path remains unverified under the hold.

Draft `source_evidence.document_recovery.sealed` revalidates previously sealed
results under the existing foreground owner lock and trusted saved attempt,
without fetching or converting. It preserves the acquisition budget and may
reconcile original-copy cleanup; a missing persisted marker remains unresolved.
An absent copy does not identify who removed it. This API is unverified.

Draft batch selection can associate one exact document URL with occurrences
from several source pages while preserving each page's plan, capture and clock.
One serial acquisition budget covers the selection; shared URLs do not prove
the occurrences identify the same property. Batch behavior is unverified under
the hold.

```sh
node extract.mjs /private/input.pdf /private/provenance.json /private/new-artifact.json
```

The owner-only provenance file contains `{"url":"https://example.org/input.pdf","observed_at":"2026-10-28T12:00:00Z"}`. It must be a regular non-symlink file owned by the current user, with no group or other permissions, and at most 16 KiB. This keeps URLs out of command arguments. Known credential-bearing query keys and URL fragments are rejected rather than retained; this is not a general secret detector. Review the URL before use, and never strip a signed query and claim the resulting URL is the same source.

The output is created exclusively with owner-only file permissions; use a private directory as well. URLs and extracted content may be sensitive. The original SHA-256, acquisition observation time supplied by the caller, detected type, parser version/config, status, text and separate UTF-8 text SHA-256 are recorded. Local input must be a regular file, not a symlink, and is limited to 100 MiB. Conversion runs in a separate process with a 60-second timeout. Extracted text is capped at 4 MiB; oversized output is retained as a failure, never silently truncated. The JavaScript heap limit does not cap native converter memory. Exit codes are 0 for extracted, 2 for OCR-needed or failed read/conversion, and 1 for invalid invocation or artifact-write errors. The caller owns the accuracy of the URL and observation time, and must retain the original if required. A successful conversion has `coverage: unknown`: AnyDoc can skip content and its Markdown is not a fidelity or financial-truth guarantee.

Extracted Markdown is source text, not normalized listing facts. Keep useful
structured source facts alongside the text, including values that do not map to
a known listing field. The existing adapter records source URL and observation
time, detected format, parser provenance, and hashes; retained evidence stores
text in ordered chunks with a full-text digest. Even extraction of all available
text would not establish that financial tables, their layout, or their values
were faithfully represented. Coverage remains unknown.

A planned reader command, `source_evidence.cli export-document-text`, will use
`--capture-id`, `--out-dir`, `--env-file`, and
`--expected-db-target-sha256` to recover retained text. This command is under
development and has not yet been verified; use the existing event readback and
chunk/hash checks until it is available and verified.

For `ocr_needed`, the artifact retains any page numbers reported by AnyDoc and has no extracted text. An explicit offline retry is available after reviewing the document and local resource budget:

```sh
python3 ocr.py --input /private/input.pdf --anydoc-attempt /private/anydoc.json --out /private/docling-attempt.json
```

This keeps the original attempt, verifies the PDF hash against it, and records a distinct attempt bound to the original artifact hash. It requires the pinned Docling image to already exist locally; it never pulls an image or uses network access. It creates and removes one labeled container, uses a read-only input and private temporary output directory, a read-only container filesystem, one CPU, 3 GiB memory, a 90-second document timeout and a 120-second client timeout. The fixed Markdown output is limited to 8 MiB per file by the container and 4 MiB when read; the host directory has no total quota. Container cleanup failures are recorded. OCR warnings are coarse codes; status `extracted` with unknown coverage is not proof every page succeeded. The existing local `firecrawl_request.py parse` and Docling service are a separate opt-in path. This adapter does not create OM facts or authorize database writes.
