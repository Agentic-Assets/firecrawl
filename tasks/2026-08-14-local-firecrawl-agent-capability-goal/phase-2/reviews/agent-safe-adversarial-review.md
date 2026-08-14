# AGENTIC-2279 Agent-Safe Adversarial Review

## Scope and method

Reviewed the uncommitted `--agent-safe` changes to
`scripts/firecrawl-ops/firecrawl_request.py` and its focused tests on
2026-08-14. I also read the active goal, the prior 2279 pilot design, the
implementation report, and the scoped ops/CRE instructions. I made no code,
runtime, Docker, `.env`, Linear, CRE, or network changes.

Static verification completed:

- `python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py`
  passed (9 tests).
- `python3 -m py_compile scripts/firecrawl-ops/firecrawl_request.py
  scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py` passed.
- `git diff --check` passed.

The tests establish several important negative cases, but they do not cover
the confirmed bypasses below. **Do not run a POST-capable pilot until every P1
is fixed and independently re-verified.**

## Confirmed findings

### P1 — Safe API requests can follow redirects or use an environment proxy

`validate_agent_safe_args()` canonicalizes the initial API origin at
`firecrawl_request.py:275-318`, but all actual requests then flow through
`request_json()` / `request_multipart()` to `open_request()` and its default
`urllib.request.urlopen()` at `:369-434`. The default opener honors proxy
configuration and follows HTTP redirects. Thus a local endpoint redirect, or
a configured proxy, can move an ostensibly loopback-only safe request off the
approved path. The validation suite tests only the initial `--api-url`; it has
no proxy or redirect regression.

**Remediation:** add a safe-only request path using an opener built with
`ProxyHandler({})` and a no-redirect handler. Thread the safe-mode choice from
the already validated command to both JSON and multipart requests; preserve
the current default opener for non-agent operator modes. Treat any redirect as
a rejected, body-free result. Add tests that prove the safe request never
consults proxy settings and never follows a loopback `302` to a non-loopback
location.

### P1 — The implementation permits unsafe scrape targets and arbitrary local uploads

`require_agent_safe_target()` at `firecrawl_request.py:170-174` checks only
for HTTPS and embedded credentials. It accepts loopback, link-local, RFC1918,
and `.local` targets, as well as arbitrary ports. That contradicts the pilot
design's public synthetic fixture and can turn Firecrawl into a local-network
or container-network fetch path.

Similarly, `agent_safe_input_digest()` at `:241-253` permits every existing
repository file, including a local ignored configuration file or a governed
artifact, so long as its resolved path stays under the checkout. The design
requires exactly the tracked one-page `apps/test-site/public/example.pdf` and
its recorded SHA-256; the code only happens to use that fixture in one happy
path test.

**Remediation:** for this first pilot, make the target an explicit, canonical
fixture allowlist (the planned `https://example.com/`) rather than attempting
general public-host validation. Require default HTTPS port and reject query,
fragment, user info, IP literals, and all other hosts. Make safe parse accept
only the tracked synthetic PDF at its expected SHA-256, with exactly one page.
Do not widen either input class until there is an approved SSRF/threat-model
decision. Add negative tests for `https://127.0.0.1:3002`, `.local`, a
non-default port, and a non-fixture repository file (including an ignored
file, represented without reading its contents).

### P1 — POST-capable safe commands do not enforce preflight/doctor evidence or an idle queue

The goal and pilot design require fresh 2277 `base_http`/`async_jobs` evidence,
a successful 2278 compatibility doctor, and a known-idle queue before any
pilot POST. `validate_agent_safe_args()` only validates command-line shape;
its success flows directly through `main()` to the requested command
(`firecrawl_request.py:1113-1125`). `cmd_scrape`, `cmd_map`, `cmd_crawl`, and
`cmd_parse` then POST immediately (`:788-828`, `:976-993`). The new
`local_agent_preflight.py` and compatibility doctor are not consulted, and
`cmd_health()` reports queue counts without failing a non-idle/unknown queue
(`:839-867`).

This is a direct CRE-isolation regression: an agent can run a supposedly safe
POST while the shared queue is active or unverified.

**Remediation:** make POST-capable safe recipes fail closed on fresh, trusted
2277 preflight evidence and an independent passed 2278 doctor receipt tied to
the current tooling-manifest digest. Immediately before each safe POST, make a
bounded local GET queue check and reject active, nonzero, malformed, or
unknown state. These checks must themselves use the no-proxy/no-redirect safe
transport and must not mutate the host. Add a test that every safe POST is
blocked before `request_json`/`request_multipart` when either prerequisite is
absent, stale, mismatched, active, or unknown.

### P1 — “Body-free” artifacts can retain server-controlled response text

`response_metrics()` copies any string `id` and `status` from the response at
`firecrawl_request.py:457-497`, and `write_agent_safe_receipt()` persists the
metrics verbatim at `:635-667`. A service error or malformed response that
reflects a source URL, token, header, or content into either field therefore
survives in stdout, the metrics artifact, and the manifest digest. The test at
`test_firecrawl_agent_safe.py:252-323` checks markdown/HTML/links only; it
does not inject secrets into `id` or `status`.

For a submitted crawl, `get_crawl_id()` accepts any nonempty string
(`firecrawl_request.py:894-900`) before `poll_crawl()` interpolates it into a
local request path (`:946-953`). Manual `crawl-status` validates its id, but a
received id does not receive the same validation.

**Remediation:** use a per-recipe, closed metrics schema. Emit a crawl id only
after it matches the safe-id pattern, and emit status only from an explicit
finite status enum. Omit identifiers/statuses for recipes that do not need
them. Validate a received crawl id before a poll URL is constructed; an
invalid/missing id produces exactly one redacted `unknown` receipt and no
follow-up request. Add secret-injection tests for response `id` and `status`,
including a malformed returned crawl id, and assert zero raw-string retention
and zero follow-up poll.

### P2 — Crawl and parse limits remain caller-expandable, and crawl polling is optional

The safe validator accepts any positive `--max-pages` for parse (`:311-314`),
arbitrary finite `--timeout`/`--poll-timeout`, a zero poll interval, and any
single include-path value (`:215-237`, `:300-308`). It also accepts a crawl
without `--wait`; the existing approval test explicitly treats that shape as
safe at `test_firecrawl_agent_safe.py:177-188`. Such a run creates a job but
does not poll it to a known disposition. A wildcard/loosely scoped include
path and inflated timeouts undermine the intentionally small pilot, even when
the requested page limit is one.

**Remediation:** require the exact first-pilot bounds: `--wait`, limit one,
concurrency one, include path `/`, parse max-pages one, request timeout at
most the agreed bound, and a positive poll interval plus a short capped poll
deadline (the design specifies 30 seconds / one second). Reject all variants
rather than silently normalizing them. Keep the existing one-submit behavior;
an unknown id must be receipt-only and must never retry. Add negative tests
for no `--wait`, wildcard/alternate include paths, max-pages greater than one,
zero poll interval, and excessive request/poll timeouts.

### P2 — Caller-controlled receipt paths can become a secret or raw-path retention channel

`relative_task_receipt_dir()` accepts any relative path below `tasks/` and
returns it unchanged as `receipt_ref` (`firecrawl_request.py:177-191`). That
value is written to `artifact_ref` (`:641-667`). A caller can place a raw URL,
credential-like value, or host-specific path segment in `--receipt-dir` and
the supposedly redacted manifest will preserve it. The existing test only
checks a generated benign temporary directory.

**Remediation:** constrain each receipt directory component to a portable
safe-slug grammar (for example `[A-Za-z0-9_-]+`) and a known shape such as
`tasks/<task-id>/evidence`; reject whitespace, URL punctuation, percent
encoding, and path-like credentials. Keep only the relative `artifact_ref`.
Add tests that reject a raw URL, token-like segment, absolute path, `..`, and
symlink escape before output creation.

### P3 — The new safe interface is not documented beside the agent tooling ladder

The helper and test add `--agent-safe`, but no modified skill, ops playbook,
or agent-tooling document describes its restricted recipe, preflight/doctor
gates, prohibited CRE use, receipt location, or stop conditions. This makes
it likely that agents will choose the broad helper path or mistake safe mode
for a general client.

**Remediation:** after P1/P2 fixes, add one short entry to the existing
interface-ladder and local-agent skill documentation: this is the sole
temporary POST-capable pilot surface, the exact fixture/bounds, required
preflight/doctor/idle-queue evidence, body-free receipt contract, and the
hard CRE/operator boundary. Do not duplicate a new client guide.

## Refuted concerns

- Validation is correctly placed before `apply_model_profile()` in `main()`;
  rejected command-line shapes do not reach the model-profile/Docker code.
  The focused negative test patches those primary dispatch functions and
  proves that ordering for the tested invalid crawl.
- `--out`, `--out-dir`, `--save-fields`, `--unwrap`, model-profile options,
  and `--print-paths` are rejected before dispatch, so the ordinary raw-output
  paths are not directly reachable through a valid safe command.
- The receipt implementation uses a relative `artifact_ref`, hashes metrics,
  and does not serialize raw markdown, HTML, links, headers, or an absolute
  receipt filesystem path in the happy-path test.
- No CRE collector, EQUIRE, database, launchd, root `.env`, or Docker code is
  modified by this patch. The problem is that the current safe gate does not
  prevent a shared queue conflict, not a direct CRE code change.

## Required re-review proof

After remediation, assign an independent verifier to run the focused safety
tests plus added no-proxy/no-redirect, fixture, prerequisite, malformed-id,
secret-field, and exact-boundary tests. It should inspect the manifest and
metrics bytes for injected marker strings, confirm no HTTP/subprocess/output
call on rejected input, and perform no live POST. Only then should the parent
decide whether fresh preflight/doctor evidence authorizes the three separate
host pilots.
