Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Local document text extraction

Local verification is separate from deployment and production acceptance; see
[PR #363](https://github.com/Agentic-Assets/firecrawl/pull/363).

This independent adapter accepts a file already acquired by an authorized process. It never fetches the source URL and does not write to Supabase or the listing collector. Install its pinned dependency with `npm ci --ignore-scripts` in this directory, then run:

Draft refresh and series hooks can opt into linked-document acquisition; they
are disabled by default and not deployed; separate production acceptance is required. The draft
`source_evidence.cli discover-documents` command
identifies candidate links in a saved, validated evidence plan; it performs no
download or extraction. Local checks are not deployment or source-access
evidence. Manual AnyDoc execution starts with an
already acquired local file.
The draft offline `plan-document-acquisition` preflight can select candidates
under an explicit bounded policy, but it does not acquire those files or run
AnyDoc. It performs no DNS or access verification and does not establish
publisher permission. An acquisition receipt preserves precise actual
download times separately from the source observation time; the local
provenance interface accepts UTC timestamps with optional one to six fractional
digits and preserves the supplied timestamp verbatim. The preflight is
not deployed; separate production acceptance is required.

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
native-page candidates. The draft refresh and series opt-ins do not add a new
writer pump, queue or scheduler. They require separate production acceptance
and do not prove publisher permission, coverage or text accuracy.

Draft recovery verifies an exact private seal and optional expected marker.
Conversion replay requires a completion marker published after the parent
validates converter completion, plus retained fetch and conversion input copies;
it does not spawn a converter. An artifact alone does not establish completion,
and a deleted original cannot be freshly rehashed from the seal. This recovery
path remains subject to separate production acceptance.

Draft `source_evidence.document_recovery.sealed` revalidates previously sealed
results under the existing foreground owner lock and trusted saved attempt,
without fetching or converting. It preserves the acquisition budget and may
reconcile original-copy cleanup; a missing persisted marker remains unresolved.
An absent copy does not identify who removed it. This API requires separate production acceptance.

Draft batch selection can associate one exact document URL with occurrences
from several source pages while preserving each page's plan, capture and clock.
One serial acquisition budget covers the selection; shared URLs do not prove
the occurrences identify the same property. Batch behavior requires separate
production acceptance.

Draft checkpoint-refresh and series flags opt into the bounded document path with an
explicit policy, its hash and a Node executable alongside source evidence.
Resume preserves those bindings and uncertain child document work requires
reconciliation before another attempt. Serial and pipeline paths defer document
work until collection returns and workers settle under the owner lock, before
canonical source-failure checks, so accessible partial evidence can be processed.
Resume binds source, code, target and the source-settlement marker; seal
revalidation and durable saves govern recovery. This is not deployed and requires separate production acceptance;
credential admission remains a separate gate.
An exact known no-data outcome remains incomplete but can permit an otherwise
authorized new source attempt without replaying the old document attempt.

Draft automatic evidence policy v3 separately admits document text by source to
the existing private event-store append pump. Earlier policies do not admit
document writes. Pending or uncertain appends block new series work; an
uncertain write requires explicit exact sealed-plan replay in refresh, without
redownload or re-extraction. This does not write canonical listings or OM facts;
the draft remains disabled by default and requires separate production acceptance.

Draft evidence SQL runs through an owned client with a 45-second deadline and
incrementally bounded output. SQL input is capped at 72 MiB, stdout at a
caller-selected limit up to 32 MiB, and stderr at 64 KiB. Unknown local cleanup keeps the foreground
recovery barrier; even settled local cleanup does not establish remote rollback.
Foreground evidence saves persist the checkpoint before checking sticky CPU and
memory guard trips; terminal and error publication uses the ordinary save path.
These changes are not deployed; separate production acceptance is required.

Owned-process settlement is checked before publishing conversion completion;
unknown cleanup keeps the parent recovery barrier. New-session escapees are
outside that guarantee. Reserved phase time includes fetch startup and reap and
converter timeout and reap; it is not a native-I/O or whole-host deadline.

```sh
node extract.mjs /private/input.pdf /private/provenance.json /private/new-artifact.json
```

The owner-only provenance file contains `{"url":"https://example.org/input.pdf","observed_at":"2026-10-28T12:00:00Z"}`. It must be a regular non-symlink file owned by the current user, with no group or other permissions, and at most 16 KiB. This keeps URLs out of command arguments. Known credential-bearing query keys and URL fragments are rejected rather than retained; this is not a general secret detector. Review the URL before use, and never strip a signed query and claim the resulting URL is the same source.

The worker detects format from content first, then the local filename extension.
The acquisition bridge may supply a versioned source-asserted hint for a
signatureless extensionless CSV when all bounded candidate assertions agree.
It considers explicit `text/csv` MIME labels and filename labels, not generic
prose or an unobserved HTTP header. Conflicting, unsupported, and overflowed
assertions remain unresolved. Detected content takes precedence; a conflicting
filename fallback remains unresolved, and conflicts are recorded. The hint is bound
to the selected candidate and selection, private conversion provenance,
artifact, completion marker, and sealed recovery; it is not verified file type,
source permission, or coverage. Legacy no-hint artifacts remain unchanged.
This draft path has no production acceptance or automatic activation.

CSV Markdown is a text extraction, not a lossless cell representation. The
pinned converter can normalize an embedded cell newline to a space. A matching
input hash and successful conversion do not prove original cell formatting or
complete table fidelity.

The output is created exclusively with owner-only file permissions; use a private directory as well. URLs and extracted content may be sensitive. The original SHA-256, acquisition observation time supplied by the caller, detected type, parser version/config, status, text and separate UTF-8 text SHA-256 are recorded. Local input must be a regular file, not a symlink, and is limited to 100 MiB. Conversion runs in a separate process with a 60-second timeout. Extracted text is capped at 4 MiB; oversized output is retained as a failure, never silently truncated. The JavaScript heap limit does not cap native converter memory. Exit codes are 0 for extracted, 2 for OCR-needed or failed read/conversion, and 1 for invalid invocation or artifact-write errors. The caller owns the accuracy of the URL and observation time, and must retain the original if required. A successful conversion has `coverage: unknown`: AnyDoc can skip content and its Markdown is not a fidelity or financial-truth guarantee.

Extracted Markdown is source text, not normalized listing facts. Keep useful
structured source facts alongside the text, including values that do not map to
a known listing field. The existing adapter records source URL and observation
time, detected format, parser provenance, and hashes; retained evidence stores
text in ordered chunks with a full-text digest. Even extraction of all available
text would not establish that financial tables, their layout, or their values
were faithfully represented. Coverage remains unknown.

The `source_evidence.cli export-document-text` reader reconstructs retained
text using `--capture-id`, `--out-dir`, `--env-file`, and
`--expected-db-target-sha256`. It validates chunks and hashes, and writes
`.txt` for plain text or `.md` for Markdown.

For `ocr_needed`, the artifact retains any page numbers reported by AnyDoc and has no extracted text. An explicit local retry is also available for a PDF with the specific failed code `unsupported`, after reviewing the document and local resource budget. Other failed codes remain refused:

```sh
python3 ocr.py --input /private/input.pdf --anydoc-attempt /private/anydoc.json --out /private/docling-attempt.json
```

This keeps the original attempt, verifies the PDF hash against it, and records a distinct attempt bound to the original artifact hash. It requires the pinned Docling image to already exist locally; it never pulls an image or uses network access. It creates and removes one labeled container, uses a read-only input and private temporary output directory, a read-only container filesystem, one CPU, 3 GiB memory, a 90-second document timeout and a 120-second client timeout. The fixed Markdown output is limited to 8 MiB per file by the container and 4 MiB when read; the host directory has no total quota. Container cleanup failures are recorded. Output consisting only of Docling image placeholders and whitespace is recorded as failed with `placeholder_only_output`, not extracted text. OCR warnings are coarse codes; status `extracted` with unknown coverage is not proof every page succeeded. The existing local `firecrawl_request.py parse` and Docling service are a separate opt-in path. This adapter does not create OM facts or authorize database writes.

For a born-digital PDF, `--engine poppler` uses the locally installed
`pdftotext` instead of Docling. It extracts existing text only and cannot OCR
images; the recorded parser version comes from the executable. It uses a private
temporary directory, a 120-second process timeout, and a 4 MiB output file limit.
The file-size limit also bounds each regular log file; native process memory is not capped. Diagnostics are retained as coarse warnings without document text. The artifact marks Poppler output as `plain`; it is not Markdown. Either engine
retains nonempty partial output from a failed converter with an explicit warning and converter exit code;
this is not proof of complete or accurate extraction. The retry remains opt-in,
creates no database writes, and leaves the original AnyDoc artifact untouched.

The optional acquisition-policy v2 mode `poppler_pdf_text_v1` runs
Poppler for an AnyDoc PDF that was extracted, needs OCR, or returned the
specific unsupported error, and retains each parser's output in a
separate sealed artifact and evidence plan. The relationship binds canonical
AnyDoc and Poppler artifact hashes. Each append has its own checkpoint and
exact replay. Poppler extracts embedded text only; it does not OCR, and does not
change AnyDoc's original outcome. Coverage and fidelity remain unknown. The mode is off
by default and requires separate production acceptance. Its phase reserves a
bounded process timeout and cleanup grace; native memory is not capped.

Settled means that the selected attempts have recorded outcomes, including
failed or OCR-needed outcomes. It does not mean that text was recovered from
every document. Inspect each parser outcome before relying on its text.

The opt-in sparse OCR complement uses the same pinned, locally cached Docling
image solely for its prebuilt PDFium and Tesseract components. It never pulls an
image or uploads a document. The adapter binds the original PDF and the ordered
AnyDoc and Poppler artifacts, records page-level results and unknown coverage,
and keeps usable partial text and bounded per-page Tesseract TSV word boxes,
line IDs, and raw confidence values. The TSV is limited to 1 MiB in aggregate;
text and TSV together are limited to 4 MiB. Omissions and partial page output
are explicit. Under `keep_unresolved_until_review`, they retain the original;
`temporary_only` preserves its existing explicit cleanup behavior.
Coordinates are pixels in the rendered page image; confidence values are engine
output, not calibrated probabilities. The adapter does not infer tables.
`no_text` requires both earlier attempts to have no text; `all_pages` is an
explicit policy for mixed or text-bearing PDFs. It
limits rendering to 200 DPI and 25 million pixels per page, uses Tesseract
English sparse mode without orientation detection, and applies 60-second
per-page and 210-second worker deadlines. Temporary images have a 128 MiB
per-file limit within a 512 MiB temporary filesystem. A private durable owner
intent precedes container creation; a separate settlement receipt records
verified removal. Failed pages, page limits and unconfirmed cleanup are
reported and subject to coordinator recovery barriers. These local results establish neither source permission nor OCR
accuracy or production acceptance.

### Physical-layout Poppler complement

The explicit `poppler_pdf_layout_v1` policy uses Poppler `-layout` and labels
that mode in parser configuration. The existing `poppler_pdf_text_v1` default
and its artifact shape remain unchanged. Layout text is a separate plain-text
complement, not table reconstruction, OCR, or a coverage guarantee. The same
input, text, process, privacy, custody, and replay bounds apply.

`keep_unresolved_until_review` retains documented extraction failures and
partial outputs, not every case of unknown content fidelity. When successful
converters leave fidelity unknown and no other configured condition is
unresolved, owned original copies can be removed. `temporary_only` retains
its explicit cleanup behavior.
