# AGENTIC-2280 final operator-controls verification

Date: 2026-08-14
Reviewer: independent adversarial verifier
Worktree: `feat/local-firecrawl-agent-capabilities`
Scope: static and mocked verification only. No `--apply`, local API, Docker/OrbStack, root `.env`, CRE, Linear, commit, or push action was performed.

## Verdict: PASS

The final remediation closes the previously confirmed P1/P2 public mutation
paths. Agent-facing model/OCR wrappers either fail closed before mutation or
delegate to the one guarded `firecrawl_operator_handoff.py` control plane.
There is no remaining must-fix bypass for AGENTIC-2280.

## Closure evidence

| Control | Verification result |
| --- | --- |
| Direct model setter | `set_model_profile.sh` is an unconditional exit-2 refusal. It contains no mutable env/path/editor/Docker path, including no prior environment-marker exception. Regression coverage proves all legacy profile names leave fixture bytes unchanged even when `FIRECRAWL_OPERATOR_HANDOFF=1` is supplied. |
| Legacy OCR lifecycle commands | `start-docling`, `start`, `restart`, `stop-adapter`, `stop-docling`, and `stop` now `exec` the handoff as `ocr-lifecycle ensure`, `restart`, or `stop`. `start-adapter` and `restart-adapter` similarly delegate only to the fixed default `ocr-adapter` action. The shell rejects sourcing before functions are defined and no longer creates its state directory before dispatch. Mocked aliases prove no Docker, state-directory, or raw-output write occurs on the legacy surface. |
| Profile/debug/capture bypasses | Legacy `--profile`, `--capture-json`, `--output-dir`, image, port, and replace-style inputs are not accepted by the lifecycle parser. The handoff refuses capture-enabled profiles such as `qa-debug`; fixed adapter startup supplies `LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=false`; post-restart adapter readback requires zero active work and `capture_docling_json is false`. |
| Guarded lifecycle semantics | `ocr-lifecycle` accepts only `ensure`, `restart`, and `stop`. It is dry-run by default, performs two idle loopback snapshots before an apply, then rechecks idleness immediately before mutation. Apply requires an allowlisted operator (`cayman` or `stace`), approval reference, provider-cost acknowledgement, exact `APPLY …` confirmation, and retained-state acknowledgement. Docling startup is pinned to the exact image/container/loopback binding; restart adds the fixed no-capture adapter and safe settings readback. |
| Receipts and failure behavior | Lifecycle receipts carry no environment maps and validate `body_retained_bytes == 0` before mode-0600 persistence. Credential-like operators are rejected. Env restore accepts only exact model/OCR transition keys and matching retained receipt/digest state. A failed env transition records the observed final digest after a safe restore attempt; a lifecycle failure writes a redacted, body-free failed/manual-handoff receipt rather than claiming rollback. Non-retained env applies record distinct transition and restored-final digests with a post-restore idle check. |
| Agent-safe and CRE non-regression | The agent-safe request pilot remains restricted to loopback, fixed public fixtures, bounded requests, and body-free evidence. No CRE collector/runtime paths changed; the only CRE-profile change replaces obsolete missing-env guidance with the upstream template instruction. |

## Verification performed

```text
python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py \
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py \
  scripts/firecrawl-ops/tests/test_set_model_profile.py \
  scripts/firecrawl-ops/tests/test_firecrawl_cli_wrapper.py \
  scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py \
  scripts/firecrawl-ops/tests/test_firecrawl_request.py \
  scripts/firecrawl-ops/tests/test_firecrawl_request_coverage.py \
  scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py \
  scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py \
  scripts/firecrawl-ops/tests/test_cre_resource_profile.py
# 112 passed, 1 skipped, 150 subtests passed

python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_local_agent_preflight.py \
  scripts/firecrawl-ops/tests/test_firecrawl_compatibility_doctor.py \
  -k 'not preflight_bypasses_ambient_proxy_and_connects_directly'
# 33 passed, 1 deselected, 27 subtests passed

python3 -m py_compile <scoped operator, pilot, compatibility, and test modules>
bash -n <scoped OCR/model/CRE/CLI/MCP shell scripts>
git diff --check
```

The deselected compatibility-doctor test creates local HTTP servers, which is
outside this static/mocked review scope.

## Non-blocking P3: retire stale examples

Several older agent-facing references still tell users to invoke the retired
`set_model_profile.sh` writer or to apply `qa-debug`/profile changes through
the legacy OCR aliases. Examples occur in `.agents/skills/firecrawl-ops`,
`.agents/skills/firecrawl-local-api`, `scripts/firecrawl-ops/README.md`,
`scripts/firecrawl-ops/CLAUDE.md`, and older local-PDF reference documents.
They now fail before mutation, so this is **not** a control bypass, but they
will cause avoidable failed agent runs. Replace those recipes with an explicit
handoff dry-run and, for a human-approved lifecycle action, the full attested
apply command. Do not restore direct profile/capture support merely to make
the stale examples work.

## Boundary note

This is an accountable same-user operator handoff, not OS authentication: a
same-UID operator can still invoke Docker or implementation code outside the
documented helpers. That limitation is explicit in the handoff module and
does not reintroduce a public agent-helper/CLI/MCP/swarm/OCR bypass.
