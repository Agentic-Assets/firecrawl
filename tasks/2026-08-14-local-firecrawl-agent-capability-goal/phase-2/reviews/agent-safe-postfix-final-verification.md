# Final independent verification: AGENTIC-2279 postfix

**Reviewer:** independent safe-pilot verifier
**Date:** 2026-08-14
**Worktree:** `/private/tmp/firecrawl-agent-capability-preflight`
**Branch / reviewed HEAD:** `feat/local-firecrawl-agent-capabilities` / `16fd0c90c`
**Scope:** static source review and mocked tests only. No local API, Docker, `.env`, CRE path, or Linear action was run.

## Verdict

**Must fix before any live agent-safe pilot.** The second remediation correctly fixes the original staged-evidence and response/receipt defects, but it adds a new pre-gate mutation path: every permitted safe POST currently performs the compatibility doctor's CLI `map` probe *before* the helper's final direct idle gate and before terminal-receipt handling. A valid request can also lose its terminal manifest because receipt validation treats old evidence as invalid after a probe whose own deadline equals the evidence TTL.

## Confirmed must-fix findings

### P1 — The automatic compatibility doctor can issue a map request before the final idle gate or a receipt exists

`run_agent_safe_prerequisites()` invokes `doctor.doctor_result(..., mode="normal", run=True, ...)` at `firecrawl_request.py:347-351`. The checked-in compatibility manifest fixes the normal CLI probe to:

```json
["map", "https://example.com", "--limit", "1", "--json"]
```

`firecrawl_compatibility_doctor.py:322-359` sends that command through `firecrawl_cli.sh`; the project's own normal `map` path is a `POST /v2/map` (`firecrawl_request.py:1277-1288`). The doctor does this before `ensure_agent_safe_post_ready()` sets `agent_safe_request_started` and before its direct `GET /v2/team/queue-status` and `GET /v2/crawl/active` recheck (`firecrawl_request.py:704-748`).

Thus the primary safe request may be properly gated, while the automatic "prerequisite" map operation is not. If that map succeeds and the subsequent MCP probe fails, the helper exits `agent_safe_prerequisite_failed` with neither the final idle proof nor an agent-safe terminal receipt. The documentation currently confirms this ordering rather than constraining it (`agent-tooling-firecrawl.md:83-93`). It also contradicts the separately documented operator-only nature of `doctor --run`.

**Required remediation:** Do not invoke a post-capable CLI map probe from the automatic `--agent-safe` path. Keep the full `doctor --run` as an explicit operator check, or split out a dedicated agent-safe capability check that is strictly read-only (for example, bounded loopback GET plus MCP initialize/tools-list, with no tool invocation). Then run the final direct queue/active GET gate immediately before the single allowed recipe request. The receipt contract must cover every operation the helper causes to be sent.

Add a mocked integration test that uses the *real* `run_agent_safe_prerequisites()` wiring (not a patched replacement) and proves that an agent-safe recipe never runs `firecrawl_cli.sh map` or any POST before the two direct idle reads. Also prove a doctor/MCP failure cannot follow an unreceipted map operation. The current base fixture replaces `run_agent_safe_prerequisites` for nearly all dispatch tests (`test_firecrawl_agent_safe.py:72-76`), and the only same-process test mocks `doctor_result` while asserting `run=True`; it cannot observe this side effect.

### P1 — The receipt TTL can fail after a valid allowed request and suppress the required terminal manifest

The evidence TTL is 45 seconds (`firecrawl_request.py:51`), while the inline doctor has a 45-second total deadline (`firecrawl_compatibility_doctor.py:31, 539-569`). The preflight is generated first, then the doctor and direct idle reads run, then the permitted request runs. At receipt creation, `receipt_prerequisite()` re-applies freshness to that first preflight (`firecrawl_request.py:995-1003`), and `validate_agent_safe_receipt()` re-applies freshness to the receipt and both prerequisites (`1010-1040`).

Consequently, ordinary cold package/MCP latency can make a fresh preflight older than 45 seconds *after* the request was already allowed. Receipt writing then exits `agent_safe_receipt_write_failed`; the outer handler deliberately does not attempt a replacement because `agent_safe_receipt_attempted` is set (`1083-1132`, `1648-1673`). That violates the required one terminal receipt for an allowed action. It also makes a previously valid durable receipt fail the same validator merely because it is later than the execution TTL.

**Required remediation:** Enforce evidence freshness only at the execution gate, after any read-only prerequisite work and immediately before the allowed request. The durable receipt schema should validate timestamp format and reject future timestamps, but should not expire historical facts on read. Re-check or regenerate the read-only preflight after longer prerequisite work if it is part of the permission decision. Add a mocked monotonic/wall-clock test where the preflight remains valid at the direct gate but crosses the old TTL while writing, and assert one valid terminal manifest still results.

## Confirmed corrections from the earlier P1/P2 review

| Earlier concern | Final result | Evidence |
|---|---|---|
| 3xx response accepted as success | **Fixed** | Agent-safe JSON, multipart, and health paths treat every non-2xx response as `http_rejected`; `test_every_safe_3xx_is_rejected_without_a_followup_request` passes. |
| Caller-supplied prerequisite artifacts authorize a POST | **Fixed** | Prerequisites are produced inline and caller flags are ignored; forged-artifact regression test passes. |
| `success: false` or malformed queue/active observation can authorize work | **Fixed** | Both preflight and final direct gate require `success is True` and explicit zero values; tests cover false/malformed/nonzero values. |
| Optional false/zero/empty scrape controls bypass fixed contract | **Fixed** | Presence-based validation rejects these values before evidence reads or network activity. |
| Receipt path or parent can be a symlink | **Fixed** | Every existing component under `tasks/` is rejected when symlinked and final resolved containment is checked; regression test passes. |
| Partial receipt is treated as terminal | **Fixed** | Metrics are atomically written first, then the manifest commit marker. A failed manifest write leaves no terminal receipt or temporary file. |
| Safe output retains raw body, request values, IDs/status text, or absolute paths | **Fixed for the safe helper output** | The v1 metrics projection is finite and body-free; error/transport/crawl tests assert redacted terminal metrics. Normal helper behavior remains on its original `response_metrics` path. |

## Remaining P2 provenance gap

`validate_agent_safe_receipt()` validates only that the metrics-artifact digest looks like a SHA-256 and that its byte count is positive (`firecrawl_request.py:1058-1065`). It does not recompute the SHA-256 or byte count from the supplied `metrics` object. The writer creates correct values, but later validation will accept a manifest whose digest or size does not bind to the artifact it is purportedly validating.

**Remediation:** canonicalize the supplied metrics exactly as the writer does, then require both `artifact.sha256 == prefixed_sha256(metrics_bytes)` and `artifact.bytes == len(metrics_bytes)`. Add a tamper regression test that changes either field while leaving a syntactically valid SHA-256 string.

## Verification performed

The following mocked/static-only validation passed:

```text
python3 -m py_compile [six reviewed Python files]
python3 -m unittest -v [agent-safe, request, request-coverage, and preflight test modules]
git diff --check
```

Result: **76 tests passed**; compilation and whitespace validation passed. The compatibility-doctor live loopback suite was intentionally not run because this review is restricted to mocked/static validation.

## Safe next state

Do not run a live pilot or call `--agent-safe` until both P1 findings are addressed and their real-wiring mocked regressions pass. No CRE collector, runtime configuration, container, `.env`, Git, or Linear state was changed by this review.
