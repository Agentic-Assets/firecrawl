Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Local document text extraction

This independent adapter accepts a file already acquired by an authorized process. It never fetches the source URL and does not write to Supabase or the listing collector. Install its pinned dependency with `npm ci --ignore-scripts` in this directory, then run:

```sh
node extract.mjs /private/input.pdf /private/provenance.json /private/new-artifact.json
```

The owner-only provenance file contains `{"url":"https://example.org/input.pdf","observed_at":"2026-10-28T12:00:00Z"}`. It must be a regular non-symlink file owned by the current user, with no group or other permissions, and at most 16 KiB. This keeps URLs out of command arguments. Known credential-bearing query keys and URL fragments are rejected rather than retained; this is not a general secret detector. Review the URL before use, and never strip a signed query and claim the resulting URL is the same source.

The output is created exclusively with owner-only file permissions; use a private directory as well. URLs and extracted content may be sensitive. The original SHA-256, acquisition observation time supplied by the caller, detected type, parser version/config, status, text and separate UTF-8 text SHA-256 are recorded. Local input must be a regular file, not a symlink, and is limited to 100 MiB. Conversion runs in a separate process with a 60-second timeout. Extracted text is capped at 4 MiB; oversized output is retained as a failure, never silently truncated. The JavaScript heap limit does not cap native converter memory. Exit codes are 0 for extracted, 2 for OCR-needed or failed read/conversion, and 1 for invalid invocation or artifact-write errors. The caller owns the accuracy of the URL and observation time, and must retain the original if required. A successful conversion has `coverage: unknown`: AnyDoc can skip content and its Markdown is not a fidelity or financial-truth guarantee.

For `ocr_needed`, the artifact retains any page numbers reported by AnyDoc and has no extracted text. An explicit offline retry is available after reviewing the document and local resource budget:

```sh
python3 ocr.py --input /private/input.pdf --anydoc-attempt /private/anydoc.json --out /private/docling-attempt.json
```

This keeps the original attempt, verifies the PDF hash against it, and records a distinct attempt bound to the original artifact hash. It requires the pinned Docling image to already exist locally; it never pulls an image or uses network access. It creates and removes one labeled container, uses a read-only input and private temporary output directory, a read-only container filesystem, one CPU, 3 GiB memory, a 90-second document timeout and a 120-second client timeout. The fixed Markdown output is limited to 8 MiB per file by the container and 4 MiB when read; the host directory has no total quota. Container cleanup failures are recorded. OCR warnings are coarse codes; status `extracted` with unknown coverage is not proof every page succeeded. The existing local `firecrawl_request.py parse` and Docling service are a separate opt-in path. This adapter does not create OM facts or authorize database writes.
