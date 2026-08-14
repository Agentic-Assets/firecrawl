# AGENTIC-2279/2281 agent-safe postfix independent verification

Date: 2026-08-14
Scope: independent static and mocked review of the consolidated
`feat/local-firecrawl-agent-capabilities` worktree. No live Firecrawl API,
Docker, `.env`, CRE, Linear, or external package operation was performed.

## Verdict

The corrected implementation materially improves the first pilot: it has a
dedicated proxy-free/no-redirect transport, exact public/PDF fixtures, fixed
crawl bounds, in-memory-only crawl IDs, projected metrics, and retained normal
helper behavior. It is **not ready for a live POST pilot** yet. Three
fail-closed P1 issues and three P2 contract/durability issues remain below.

## What was verified as fixed

- Safe JSON and multipart requests use `ProxyHandler({})` and a
  redirect-rejecting handler. Ordinary requests still use the prior `urlopen`
  path. The existing non-safe helper suites pass, so this did not narrow normal
  helper behavior.
- Scrape, map, and crawl accept only `https://example.com/`; safe parse accepts
  only the tracked `apps/test-site/public/example.pdf` with the reviewed
  digest. Query, fragment, IP/private-network, alternate-host, alternate-port,
  arbitrary-file, and symlink-file inputs are rejected before a request.
- Safe crawl requires exactly one page, concurrency one, include path `/`,
  markdown, `--wait`, a 30-second poll budget, and a one-second interval.
  Standalone `crawl-status` and generic `post` are unavailable. A returned
  malformed crawl ID is not polled or retained.
- Projected metrics omit response IDs, statuses, messages, bodies, URLs,
  paths, headers, and uploaded-file names. The initial review's injected
  identifier/status cases are covered and pass.
- Freshness is strict UTC and capped at 45 seconds; the doctor requires normal
  mode, all three checks, `body_bytes_persisted == 0`, and the current manifest
  hash. The direct recheck occurs before every safe POST.

## Confirmed findings

### P1 — A redirect becomes a successful command rather than a rejected terminal result

`open_request()` correctly stops a redirect from being followed, but catches
the resulting `HTTPError` and returns its 3xx status (`firecrawl_request.py`
592-605). `run_and_write()` and `cmd_parse()` only raise for `>= 400`
(1027-1044, 1076-1094). Consequently a safe scrape/map/parse whose loopback
endpoint returns 302 writes an `http_rejected` receipt but exits `0`.

For crawl, a 3xx body that happens to contain a syntactically valid `id` can
also enter the poll path. This contradicts the documented rule that redirects
are rejected, and permits an agent to mistake a rejected operation for a
successful terminal result.

**Mocked reproduction:** a safe map with idle gate responses followed by a 302
returned exit code `0` without following the redirect.

**Remediation:** define safe success as exactly `200 <= status < 300` at every
safe response boundary. Map all non-2xx values, including 3xx, to
`http_rejected`, write the one redacted receipt, and exit nonzero before crawl
ID extraction/polling. Add scrape, map, parse, crawl-submit, poll, and health
3xx regressions; assert no follow-up request after the redirect.

### P1 — The preflight and doctor files are shape checks, not trusted evidence

`validate_preflight_evidence()` and `validate_doctor_evidence()` accept an
arbitrary fixed-path JSON file if its fields have the right shape, current
timestamp, and (for the doctor) the readable manifest hash
(`firecrawl_request.py` 303-361). Neither confirms that the preflight's
`evidence_digest` was computed by `local_agent_preflight.py`, nor that the
doctor record came from a `--run` execution. The receipt does compute a hash
of the supplied bytes, but that only identifies an untrusted input.

This does not create an OS security boundary against a same-user process that
can edit the checkout. It does, however, defeat the advertised cooperative
agent gate: an agent can write two current, body-free JSON files under the
fixed path and then cause a safe POST without doing either prerequisite.

**Mocked reproduction:** hand-authored, current preflight/doctor documents
with a dummy preflight digest satisfied validation and allowed the safe map
POST (the third mocked request was consumed).

**Remediation:** choose and document the intended authority boundary. For a
meaningful helper-enforced guard, generate/verify the required observations in
the same trusted operation, or consume an operator-held attestation whose
producer and integrity are actually verifiable. If same-user artifacts remain
the deliberate model, state that they are cooperative operational evidence,
not authorization, and do not describe the helper as enforcing the preflight
or doctor run. At minimum, share a canonical preflight verifier that
recomputes the producer's evidence digest and rejects unknown document keys.
Add a test using a hand-authored shape-valid document and assert the chosen
behavior explicitly.

### P1 — A negative queue-status response can authorize a POST

The direct gate considers `{"jobsInQueue": 0}` idle without requiring
`"success": true` (`firecrawl_request.py` 706-739). The preflight producer
similarly labels any decodable sub-400 queue body `result: "success"` and
extracts counts without checking the API's `success` flag
(`local_agent_preflight.py` 260-311). The upstream controller explicitly
returns `success: true` on a valid queue status
(`apps/api/src/controllers/v2/queue-status.ts` 74-79), so a false/missing flag
must be treated as unknown rather than idle.

**Mocked reproduction:** `{"success": false, "jobsInQueue": 0}` plus an
empty active-crawl response permitted a safe map POST and exited `0`.

**Remediation:** require a JSON object with `success is True` in both the
preflight observation and immediate direct recheck; preserve the existing
integer/zero checks. Add direct and saved-preflight cases for false/missing
success, and assert no POST.

### P2 — False/zero scrape controls bypass the exact fixed recipe

The safe scrape rejection list uses truthiness at
`firecrawl_request.py` 403-420. Thus `--only-main-content false`,
`--wait-for 0`, and `--max-age 0` are accepted and are passed into the request
body even though the pilot promises no caller-controlled rendering/cache
options.

**Mocked reproduction:** `--only-main-content false` validated successfully.

**Remediation:** reject these controls by presence (`is not None`) rather than
truthiness, and add the false/zero negative table. Retain the default values
only when no option was supplied.

### P2 — The fixed evidence directory can still escape through a parent symlink

`read_agent_safe_evidence()` rejects a symlink only on the final JSON file
(`firecrawl_request.py` 283-296). A symlinked `tasks/agentic-2279` or
`evidence` parent passes that check, and `write_agent_safe_receipt()` will
follow it for both artifacts. This misses the prior review's required symlink
escape protection and allows output outside the task tree.

**Remediation:** before reading or writing, reject symlinks on every existing
component and require `AGENT_SAFE_EVIDENCE_DIR.resolve(strict=False)` to stay
under the real repository `tasks/` directory. Test a symlinked intermediate
directory, not only a symlinked `preflight.json`.

### P2 — Receipt v1 does not yet provide the promised terminal/durable contract

The implementation retains no raw data, but `write_agent_safe_receipt()`
(`firecrawl_request.py` 878-918) writes directly to final filenames without
atomic replacement or a schema validation step. Its manifest also lacks the
receipt-design's `kind`, `observed_at`, explicit redaction declaration, stable
receipt ID, prefixed digest grammar, and externally validated closed schema.
A failed or interrupted artifact write can therefore leave a metrics-only or
partial manifest that a later reader cannot distinguish from a valid terminal
receipt.

**Remediation:** implement the phase-3 receipt v1 schema as the single source
of truth, validate the projected metrics/manifest before writing, write both
through same-directory temporary files, and `os.replace` the manifest last as
the commit marker. Add write-failure/torn-file tests and a strict negative
schema table. Keep only opaque logical `artifact_ref` values and retain the
current redaction projector.

## Verification performed

- `python3 -m py_compile` for the helper, preflight, doctor, and focused tests
  — passed.
- `python3 -m unittest -v` for `test_firecrawl_agent_safe.py`,
  `test_firecrawl_request.py`, `test_firecrawl_request_coverage.py`, and
  `test_local_agent_preflight.py` — **68 passed**. These are mocked/static;
  they include the prior normal-helper regression suite.
- `git diff --check` — passed.
- A broader doctor-suite run executed **84 of 85** tests successfully. The
  remaining `test_preflight_bypasses_ambient_proxy_and_connects_directly`
  could not bind its ephemeral `127.0.0.1` listener in this sandbox
  (`PermissionError`), so this report does not claim a clean full doctor run.
  The failure was environment permission before the test's doctor call, not a
  live API or Docker operation.

## Refuted concerns

- I found no regression that exposes raw server IDs, status strings, messages,
  source bodies, target values, uploaded paths, or query fragments in the
  projected safe artifacts on the tested paths.
- The normal helper still accepts its broader operational features; the new
  transport is selected only when `agent_safe=True`.
- No CRE collector, EQUIRE process, database, launchd job, root `.env`, model
  profile, Docker/OrbStack configuration, Linear record, or live endpoint was
  changed or exercised by this verification.

## Release gate

Do not run a live safe POST pilot until the P1 findings are fixed, the P2
receipt/path controls are resolved or explicitly deferred with rationale, and
an independent mocked re-review confirms every failure path writes at most one
redacted terminal receipt and never performs a post-gate follow-up request.
