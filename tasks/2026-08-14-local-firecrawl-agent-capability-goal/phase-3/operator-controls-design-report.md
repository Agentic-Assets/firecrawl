# AGENTIC-2280 operator controls design

**Scope:** design only, 2026-08-14. No runtime, `.env`, Docker, Linear, API, or
CRE change was made for this report.

## Decision

Add one fork-owned, explicit operator transition entrypoint for shared model
and OCR configuration. Reuse the existing profile setter, OCR helper,
healthcheck, API queue endpoints, and current model routing. Do not add a
client, service, scheduler, provider, background retry loop, or agent-facing
profile switch.

Suggested public command:

```text
scripts/firecrawl-ops/firecrawl_operator_handoff.sh
```

It should have mutually exclusive operations:

1. `model --profile <gateway|gateway-pro|...>` changes only the three
   non-secret model-routing keys.
2. `ocr-routing` changes only non-secret local FirePDF routing keys and never
   overwrites `FIRE_PDF_API_KEY`.
3. `ocr-adapter --profile <name>` starts or restarts the existing local Docling
   adapter with a named profile; it does not write `.env`.
4. `restore --receipt <id>` restores only the recorded non-secret keys after
   rechecking that the full `.env` digest still equals the post-transition
   digest.

`set_model_profile.sh` remains the internal profile renderer/writer called by
the operator entrypoint only. `local_firepdf_ocr.sh` remains the lifecycle
implementation, but `enable-firecrawl` should become a refusal with migration
guidance (or be private to the entrypoint). This is materially simpler than
duplicating its Docker/Docling logic.

An operator flag is an audit boundary, not authentication against a malicious
same-UID process. The host must remain trusted; the implementation must not
claim that a shell flag is RBAC. The actual enforceable product boundary is:
agent-safe CLI/helper/MCP interfaces never expose a profile mutation flag or
invoke the setter/Docker, while the documented mutable script is explicitly
operator-only and records an accountable approval reference.

The AGENTIC-2279 `--agent-safe` validation is useful for bounded pilots but is
not sufficient evidence for AGENTIC-2280 by itself: the ordinary CLI and helper
parsers still accept profile-changing flags. To satisfy “the only mutable
profile path,” remove or reject those flags in the ordinary agent wrappers as
well, rather than relying on callers to remember safe mode.

## Why current paths need replacement

| Current path | Confirmed concern | Required resolution |
|---|---|---|
| `firecrawl_cli.sh --firecrawl-model-profile` | Invokes `set_model_profile.sh` and may recreate API directly from an agent-facing wrapper. | Reject the flag before resolving repo, writing `.env`, invoking Docker, healthcheck, or `npx`. |
| `firecrawl_request.py --model-profile` | Invokes the setter and Compose directly from the local-agent helper. | Remove/reject the profile, no-recreate, and profile-healthcheck options before `apply_model_profile` can run. |
| `firecrawl_swarm_pipeline.py` | `run_profile_switch` resolves the user-installed setter and executes `docker compose down` then full `up`; it bypasses both wrapper controls and would disrupt all local work. | Remove profile switching from this legacy/example flow; require an operator handoff outside the run, and never run `compose down` as part of a model change. |
| `set_model_profile.sh` | Writes before it observes queue/crawl state. Its model switch also rewrites unrelated local-default keys. | Invoke only through operator handoff; narrow its transition mode to the three model keys. Retain a separately tested bootstrap mode if needed. |
| `local_firepdf_ocr.sh enable-firecrawl` | Mutates `.env` directly and, when `.env` is absent, calls `set_model_profile.sh budget`, silently selecting OpenRouter rather than the Gateway default. | Replace with operator entrypoint routing mode; fail closed on absent config or explicitly bootstrap `gateway`, never `budget`. Preserve the API-key line. |
| `firecrawl_healthcheck.sh` | Its normal smoke submits `POST /v2/scrape`; it cannot be the pre-write safety check. | Use only after an approved transition/recreate. Pre-write checks are bounded GETs only. |

## Exact transition state machine

All state-changing operations use `--dry-run` by default. `--apply` requires
all of the following values before any mutation: a nonempty `--operator`, an
external `--approval-ref`, `--approve-provider-cost`, and exact confirmation
text such as `APPLY model gateway`. Retaining rather than restoring requires a
separate `--retain --handoff-ref <ref>` and `RETAIN model gateway` confirmation.
The receipt records attestation fields, never a key or full `.env` contents.

| Phase | Exact checks and action | Fail-closed result |
|---|---|---|
| Validate | Accept one operation and a declared target. Permit loopback `http://localhost:3002` only; reject proxy routing and redirects. Confirm the static profile mapping before network work. | Exit 2, no files or subprocesses. |
| Snapshot A | GET `/`, `/v2/team/queue-status`, and `/v2/crawl/active`, each with bounded connect/read deadlines. Parse JSON strictly. Queue must have `success:true`, finite nonnegative integer `activeJobsInQueue`, `waitingJobsInQueue`, and `jobsInQueue`, with `jobsInQueue == active + waiting` and all three zero. Active response must have `success:true` and an empty `crawls` array. | State `unknown` or `busy`; no `.env`, Docker, OCR, or canary action. Do not print active crawl URLs. |
| Snapshot B | Repeat both queue/active GETs after a short fixed delay and require the same idle result. For `ocr-adapter`, also GET adapter `/settings` and require finite `adapter.active_ocr == 0`, `max_concurrent_ocr > 0`, and no raw Docling JSON capture. | Race/busy/unknown; no mutation. |
| Plan | Calculate changed keys, current and proposed non-secret config fingerprints, and canary cost envelope. `--dry-run` emits only a body-free planned receipt. | No Compose action on dry run. |
| Apply | Recheck Snapshot B immediately before mutation. Change only the operation's allowlisted keys; preserve `OPENAI_API_KEY`, `FIRE_PDF_API_KEY`, all unrelated `.env` bytes, and CRE settings. Run `docker compose ... up -d --force-recreate api` only for model/OCR-routing operations. For adapter profile only, call the existing `local_firepdf_ocr.sh restart-adapter --profile ...` path. | Abort on any subprocess failure; report retained pre-change state and no automatic compensation guess. |
| Verify | Run existing bounded healthcheck after API recreation. Recheck queue/active before submitting one approved canary. Validate only success/schema/metadata/elapsed time, discard response body, and write body digest plus metrics. | Failed check or canary ends transition; do not retry. |
| Restore or hand off | Default is explicit restore using recorded non-secret key values only if the post-change full `.env` SHA-256 still matches; recreate and healthcheck again. `--retain` is allowed only with the separate handoff confirmation/reference. | Digest mismatch means manual handoff, never overwrite another operator's change. |

The queue API implementation returns `jobsInQueue`, `activeJobsInQueue`, and
`waitingJobsInQueue`; it may aggregate Redis and FDB counts. The active-crawl
endpoint returns crawl objects containing URLs, so receipts record only
`active_crawl_count`, never its response body. Both checks are necessary:
queue visibility covers more than crawls, while active crawl visibility detects
in-flight crawl state.

## Configuration and rollback contract

For `model`, the sole keys are `OPENAI_BASE_URL`, `MODEL_NAME`, and
`MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK`. Current static mapping must remain:

| Profile | Base URL | Primary | Fallback |
|---|---|---|---|
| `gateway` | `https://ai-gateway.vercel.sh/v1` | `deepseek/deepseek-v4-flash-0731` | `deepseek/deepseek-v4-pro-0813`, one structured-output compatibility fallback only |
| `gateway-pro` | `https://ai-gateway.vercel.sh/v1` | `deepseek/deepseek-v4-pro-0813` | empty |

The transition record stores only the old/new values of those non-secret keys,
the full `.env` SHA-256 before and after, and the changed-key list. It must not
create a full `.env` backup because that can duplicate provider credentials.
Require the three profile keys to already exist for a reversible transition.
If bootstrap support remains necessary, make it a separately named operator
`init` action that creates a no-secret template and cannot be mistaken for a
profile switch.

`set_cre_resource_profile.sh` is a useful *pattern* for allowlisted key state,
0600 ignored state, and restore refusal when there is no state. Do not invoke,
modify, share a state directory with, or extend that CRE script: model/OCR
handoff needs its own state path and an additional full-env digest collision
check before restore.

`ocr-routing` similarly changes an allowlist such as `FIRE_PDF_ENABLE`,
`FIRE_PDF_PERCENT`, `FIRE_PDF_BASE_URL`, `PDF_RUST_EXTRACT_ENABLE`, and
`MINERU_PERCENT`; it must leave `FIRE_PDF_API_KEY`, RunPod keys, and all model
keys unchanged. Refuse if local OCR routing would need to replace a nonempty
external FirePDF key. `ocr-adapter` uses the current named
`pdf_ocr_profiles.json` plus `local_firepdf_ocr.sh`; no new profile format is
needed.

## Canary and OCR semantics

The post-transition canary is opt-in in `--apply`, bounded, and never retried
by the handoff script.

- Model canary: one public fixed fixture with one narrow JSON schema, a hard
  request deadline, and an explicit maximum of two provider calls only when
  the existing Gateway structured-output fallback is actually deployed. The
  receipt says `structured_fallback_status: unmerged` until PR #32's fallback
  implementation is merged, so a local branch result is not presented as
  durable default behavior.
- OCR canary: the existing test-site PDF, `mode:ocr`, `maxPages:1`, fixed
  deadline, no `--out`/raw capture. Validate response metadata in memory and
  retain only a digest and compact quality/capacity facts.
- `429` means OCR backpressure: stop and let the operator choose a later run or
  a lower workload. `504` means timeout: manual review or an explicitly
  authorized rerun. `422` means low-quality OCR: reject for automated use and
  require manual review/profile decision. No status gets an automatic retry,
  profile escalation, adapter restart, or capacity increase.

## Receipt schema

Write an ignored, mode-0600, body-free JSON receipt under
`tasks/tmp/firecrawl-operator-handoff/`. Include `schema_version`, receipt id,
timestamps, operation, target, static mapping fingerprint, operator,
approval/handoff references, `provider_cost_approved`, queue and active *count*
snapshots, OCR settings fingerprint/counts if applicable, `.env` digests,
Compose/health status, canary id/status/elapsed/body digest, and
`final_state: restored|retained|manual-handoff-required`.

Never include API/provider/OCR keys, URLs from active crawls, request/response
bodies, PDF paths, raw adapter settings, environment dump, or Docker logs.
AGENTIC-2281 should later reuse this compact contract rather than inventing a
second receipt system.

## Required tests and proof path

1. Extend `test_set_model_profile.py` with a table-driven static mapping test
   for every profile, specifically Gateway Flash 0731 plus Pro 0813 fallback
   and Gateway Pro 0813 without fallback.
2. Add wrapper/helper negative tests using a byte fixture `.env` and stubbed
   `docker`, setter, healthcheck, and `npx`: all profile-changing flags return
   a usage/policy error; `.env` is byte-identical and no stub is invoked.
3. Unit-test the new handoff with fake bounded GET responses: idle double
   sample succeeds in dry run; malformed values, mismatched queue arithmetic,
   one active job/crawl, redirects/proxy, timeout, and idle-to-busy race all
   fail before the write/Docker spies.
4. Test `--apply` is impossible without all approval and exact-confirmation
   fields; a valid sequence has strictly ordered calls: double snapshot,
   allowlisted write, API recreate, healthcheck, final idle check, one canary,
   restore or explicit retained receipt.
5. Test no full `.env`/secret material enters the receipt and restore refuses
   a post-transition digest mismatch. Test OCR `429`, `504`, and `422` each
   yield a terminal non-retry state.
6. Run `ruff`, `py_compile`, focused pytest, shell syntax checks, `git diff
   --check`, then a host validation only after a current preflight is idle. The
   host test starts with bounded GETs and stops before the canary if they fail.

## Narrow file set and gates

Expected fork-only touch points are the operator script and tests, existing
CLI/helper flag parsing and tests, `set_model_profile.sh`, the mutable
`enable-firecrawl` branch, the legacy swarm profile-switch function, ops/agent
docs, and the two local skills. Do not touch `apps/api`, CRE
collector/adapters/SQL, EQUIRE, Supabase/Postgres, launchd, OM facts, or root
`.env` for implementation.

Implementation should wait for AGENTIC-2277/2278 integration and independently
re-read the live issue. A fresh host health/idle result, a provider-cost
approval, and explicit operator confirmation are still runtime gates. PR #32
is currently a draft unmerged prerequisite for claiming the configured
structured-output fallback is live everywhere; its mapping may be tested now,
but its end-to-end behavior cannot be used as merged-main proof.
