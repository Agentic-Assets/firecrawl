# Local Firecrawl parser and ops validation

Date: 2026-08-13

## Outcome

The rebuilt local stack passed its configuration guard, health check, bounded
PDF parser canary, direct HTTP helper parse, and CLI parse from a non-repository
working directory. No source code, Docker configuration, or `.env` values were
changed by this validation. Docling and OCR mode were neither started nor
invoked.

Raw, non-sensitive evidence is under
`tasks/tmp/20260813-parser-ops-validation/`.

## Commands and results

| Command | Result |
|---|---|
| `python3 scripts/firecrawl-ops/check_pnpm_docker_config.py` | Passed: `pnpm/Docker config guard passed`. |
| `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir tasks/tmp/20260813-parser-ops-validation/healthcheck` | Passed all four checks: compose services present, API root returned Firecrawl API metadata, and scrape smoke returned `success: true`, `markdown_len: 180`. |
| `python3 scripts/firecrawl-ops/local_capability_matrix.py --out tasks/tmp/20260813-parser-ops-validation/local-capability-matrix.md` | Completed, but selected an obsolete smoke artifact. See P2 below. The generated Markdown itself was confined to `tasks/tmp`. |
| `python3 scripts/firecrawl-ops/pdf_parse_canary.py --pdf apps/test-site/public/example.pdf --modes fast,auto --max-pages 2 --timeout 120 --out-dir tasks/tmp/20260813-parser-ops-validation/pdf-parse-canary` | Passed: `fast` HTTP 200, 415 Markdown characters, 54 ms; `auto` HTTP 200, 415 characters, 9 ms. |
| `scripts/firecrawl-ops/firecrawl_request.py parse apps/test-site/public/example.pdf --formats markdown,html --pdf-mode fast --max-pages 2 ...` | Passed against `POST /v2/parse`; wrote Markdown, HTML, metadata, and complete response only under the task temp directory. |
| `cd tasks/tmp/20260813-parser-ops-validation && ../../../scripts/firecrawl-ops/firecrawl_cli.sh parse ../../../apps/test-site/public/example.pdf --json --pretty` | Passed, proving the documented CLI wrapper preserves the caller working directory for a relative upload path. |

The direct helper implementation sends the expected multipart `options` plus
`file` request at [firecrawl_request.py](../../scripts/firecrawl-ops/firecrawl_request.py#L408-L432).
Its tested parser payload included `formats`, `type: pdf`, `mode: fast`, and
`maxPages: 2` as constructed at
[firecrawl_request.py](../../scripts/firecrawl-ops/firecrawl_request.py#L337-L358).
The canary defaults exclude OCR unless explicitly opted in at
[pdf_parse_canary.py](../../scripts/firecrawl-ops/pdf_parse_canary.py#L154-L162).

## Expected configuration gates

- The no-token parser calls succeeded, so local auth did not block the tested
  path.
- Plain PDF parsing is ready for the tested fixture. `PDF_RUST_EXTRACT_ENABLE`
  is set; no AI format was requested.
- `OPENAI_API_KEY` is empty. Summary, query, JSON extraction,
  params-preview, and extract are therefore not validated and remain gated on
  a usable provider credential, base URL, and model configuration.
- `BROWSER_SERVICE_URL` and `EXTRACT_V3_BETA_URL` are absent. Browser and
  agent routes remain intentionally unconfigured.
- The canary used only `fast,auto`. No Fire PDF/Docling OCR service was
  started or exercised, regardless of any pre-existing OCR routing setting.

The CLI responses contained the non-fatal API warning that the selected engine
does not support `skipTlsVerification`. The fixture still returned complete
Markdown and HTTP 200; this is only relevant to a future workflow that needs
that option.

## Confirmed defect

### P2: no-argument capability regeneration silently uses stale smoke evidence

`local_capability_matrix.py` defaults to the legacy directory
`tasks/tmp/local-api-smoke` at
[local_capability_matrix.py](../../scripts/firecrawl-ops/local_capability_matrix.py#L15-L18).
Its `latest_smoke_file` function only searches that one directory
([lines 62-66](../../scripts/firecrawl-ops/local_capability_matrix.py#L62-L66)), and `main` uses it when
`--smoke-file` is omitted ([lines 186-193](../../scripts/firecrawl-ops/local_capability_matrix.py#L186-L193)).

On this validation, the requested no-argument flow selected
`tasks/tmp/local-api-smoke/20260626-123942-local-api-smoke.json`. The checked-in
matrix instead records the newer August 11 artifact at
[local-capability-matrix.md](../../docs/firecrawl-ops/references/local-capability-matrix.md#L3-L6), and newer smoke
artifacts are present outside that legacy directory. This conflicts with the
operator claim that the script uses the “latest smoke matrix” at
[tools-capabilities.md](../../docs/firecrawl-ops/references/tools-capabilities.md#L101-L104).

Impact: an operator following the documented no-argument command can overwrite
the capability matrix with June route results, job IDs, and parser metrics,
making the resulting runtime-readiness evidence stale even though generation
exits successfully.

Root-cause hypothesis: smoke artifacts have moved to dated task directories,
but the generator still treats one historical fixed directory as the complete
evidence store. A later fix should either require an explicit `--smoke-file`
for durable regeneration or use a single canonical current-evidence location
that both the smoke producer and generator share.

## Upstream compatibility boundary

At local `HEAD` `a925132eb`, no parser/helper failure attributable to the
August 10 upstream sync was observed. The cached `upstream/main` is three
commits ahead of the current merge base (`e72fe3acac88`); no fetch or upstream
merge was performed, so this report does not certify compatibility with those
three unmerged commits.

At final readback, changes to `scripts/firecrawl-ops/firecrawl_healthcheck.sh`,
`scripts/firecrawl-ops/firecrawl_healthcheck_evidence.py`, and its test were
already present from concurrent work and were intentionally left untouched.
