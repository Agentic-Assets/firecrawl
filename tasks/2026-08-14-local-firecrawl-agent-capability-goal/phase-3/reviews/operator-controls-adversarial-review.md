# AGENTIC-2280 operator-controls adversarial review

Date: 2026-08-14
Reviewer: independent operator-controls adversary
Worktree: `feat/local-firecrawl-agent-capabilities`
Scope: static review and focused tests only. No operator apply, API call, Docker/OrbStack action, `.env` read/write, CRE action, Linear update, commit, or push was performed.

## Verdict

**Do not claim AGENTIC-2280 is complete yet.** The CLI wrapper, direct helper,
MCP wrapper, and legacy swarm restart option now reject or lack the former
model-profile mutation path. However, four confirmed boundary and evidence
defects still permit a direct mutable route or make an applied change
unrecoverable/auditable only by manual inspection.

## Confirmed findings

### P1 — direct model writer bypasses the handoff entirely

`set_model_profile.sh` only checks whether
`FIRECRAWL_OPERATOR_HANDOFF=1` is present (lines 8-11), then writes the root
env file directly. Any same-user caller can set that public environment
variable and invoke the script, skipping the operator handoff's approval
reference, provider-cost acknowledgement, queue/crawl idleness checks,
dry-run receipt, and rollback record. The dedicated unit suite demonstrates
that the marker is sufficient to make every profile write.

**Required correction:** remove the direct public writer invocation as a
supported interface. Route model writes through a private handoff-only helper
and enforce the same explicit handoff operation at the script boundary. If the
same-UID limitation remains an accepted non-security limitation, document it
as such, but do not describe this simple environment-variable bypass as an
operator-only enforcement mechanism. Add a regression proving a direct
invocation with caller-supplied environment variables cannot mutate a fixture
env file.

### P1 — direct OCR lifecycle commands still mutate runtime and can enable raw capture

`local_firepdf_ocr.sh` remains an agent-documented command surface for
`start`, `start-adapter`, `restart`, and `restart-adapter` (lines 60-102 and
564-584). Those paths build/run/remove Docker containers. They accept
`--profile`, `--capture-json`, and `--output-dir` (lines 136-175), so
`restart-adapter --profile qa-debug --capture-json` can enable raw Docling
capture and arbitrary host artifact output without the queue gate,
attestation, receipt, or handoff readback. Only `enable-firecrawl` was
disabled. The script also creates `STATE_DIR` at line 21 before command
dispatch, so even a rejected `enable-firecrawl` request has a write side
effect.

**Required correction:** distinguish read-only diagnostics from mutable OCR
lifecycle commands, and make all start/restart/profile/capture operations go
through the operator handoff with its queue and receipt contract. Reject
capture/debug profiles at every direct public entrypoint, not only the new
handoff. Delay state-directory creation until an allowed mutable operator
operation actually begins. Add negative tests for direct `restart-adapter`,
`start`, and debug/capture flags that assert no Docker invocation, output
directory creation, or state-directory creation.

### P1 — restore trusts an arbitrary receipt key set and can disclose or overwrite secrets

For `restore`, `firecrawl_operator_handoff.py` accepts any
`changed_keys`/`old_values` pair from a schema-version-only JSON receipt (lines
544-562). It does not require an original `model`/`ocr-routing` operation or
that the exact key set equals one of the two non-secret allowlists. The caller
can also choose an arbitrary `--env-path` and `--receipt-dir` (lines 509-511).
A crafted local receipt can therefore make restore replace, for example,
`OPENAI_API_KEY` or an unrelated CRE variable. The current value is then put
in `old_values` in the new receipt. `write_receipt()` only searches for
`OPENAI_API_KEY=`-style text (lines 392-408), not JSON key/value fields, so a
provider secret can be persisted in the body-free receipt.

**Required correction:** bind receipts to the canonical repository and default
root `.env` path, or make test-only path injection non-public. Validate the
source receipt's operation, target, key-set identity, old/new key maps, and
digest semantics before restore; permit exactly `MODEL_KEYS` or
`OCR_ROUTING_KEYS`, never their subsets/supersets or secret keys. Construct
the receipt from an explicit non-secret projection instead of substring
filtering serialized JSON. Add a malicious-receipt test that attempts
`OPENAI_API_KEY`, a CRE resource key, an absolute alternate env path, and a
secret sentinel; assert no write and no sentinel in any receipt.

### P1 — apply failures leave changed state with no durable receipt

The handoff modifies the env before Docker recreation/health checks (model:
lines 633-653; OCR routing/restore: 654-670). Every later subprocess,
post-change idle check, and optional automatic restore runs before the only
`write_receipt()` call at lines 726-742. A compose failure, healthcheck
failure, failed final idle read, or failed restore healthcheck therefore raises
without a receipt while leaving a changed `.env` or a partially recreated
runtime. This contradicts the stated manual-handoff / rollback evidence
contract and prevents a reviewer from knowing the exact state to repair.

**Required correction:** once a mutation attempt begins, handle every failure
in a transaction-like boundary that re-reads the current env, projects only
the permitted non-secret state/digests, writes a mode-0600
`manual-handoff-required` receipt, and returns a redacted failure status. Do
not guess or automatically overwrite a divergent env. Add failure-injection
tests for each runner call and the final idle read that assert a receipt exists,
contains no secret, and reflects the actual final env digest.

### P2 — OCR adapter apply has no post-restart readback

For `ocr-adapter`, `adapter_snapshot()` runs before `restart-adapter` (lines
625-630), then the script starts/restarts the adapter (658-667). There is no
post-restart `/settings` or adapter health readback. The receipt consequently
reports pre-transition `active_ocr`, capacity, and settings fingerprint even
though it claims the adapter transition was retained. This misses a failed,
stale, or capture-enabled adapter configuration.

**Required correction:** after the restart succeeds, perform a bounded
loopback adapter health/settings readback, require zero active work and
no-raw-capture settings again, record the observed post-transition fingerprint,
and fail with a manual-handoff receipt if it cannot be verified. Add a test
where the second post-restart settings response is unsafe.

### P2 — automatic restore receipts record the transitional, not final, env digest

For a non-retained `model` or `ocr-routing` apply, the script restores
`raw_before` at lines 696-725 but still passes the temporary changed
`raw_after` into `make_receipt()` as `env_sha256_after` at lines 726-739. The
receipt says `final_state: restored` while its `after` digest describes a
configuration that is no longer on disk; it also omits a final post-restore
idle readback. That makes the receipt's rollback evidence internally
inaccurate and cannot support a later restore check.

**Required correction:** record distinct transition and final digests, or make
`env_sha256_after` consistently mean the actual final file and retain any
transitional digest under a separately named, documented field. Re-read and
verify the restored env plus queue/crawl idle state before issuing a restored
receipt. Add a default (non-`--retain`) apply test for the exact receipts and
readback ordering.

### P3 — CRE bootstrap guidance now points to a disabled direct command

`set_cre_resource_profile.sh` still tells a missing-env operator to “run
set_model_profile.sh first” (line 64). The new model script refuses that
ordinary direct invocation. This does not change a configured CRE pipeline,
but it breaks the documented recovery path for a missing root `.env` and
conflicts with the requirement to preserve CRE operations.

**Required correction:** change that message to the canonical env-template
bootstrap instruction and explicitly leave the CRE resource-profile script
outside the operator-handoff scope. Add a targeted missing-env assertion if
the message is retained as an operator aid.

## Confirmed positive controls

- `firecrawl_cli.sh` rejects former model/Docker/healthcheck flags before
  compatibility resolution or `npx`.
- `firecrawl_request.py` no longer includes profile/Docker mutation code, and
  its restricted agent-safe route keeps AI/OCR and raw-output controls out.
- The MCP wrapper has no profile-switch argument surface.
- The legacy swarm `--restart-between-stages` flag errors before input or
  network work and no longer calls `docker compose down`.
- The intended Gateway mappings are correct: Flash 0731 with Pro 0813 fallback
  for `gateway`, and Pro 0813 with no fallback for `gateway-pro`.

## Verification performed

```text
python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py \
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py \
  scripts/firecrawl-ops/tests/test_set_model_profile.py \
  scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py \
  scripts/firecrawl-ops/tests/test_firecrawl_cli_wrapper.py \
  scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py \
  scripts/firecrawl-ops/tests/test_firecrawl_request.py \
  scripts/firecrawl-ops/tests/test_firecrawl_request_coverage.py \
  scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py
# 95 passed, 1 skipped, 122 subtests passed

python3 -m py_compile <scoped operator/helper Python files and tests>
bash -n <scoped CLI/MCP/model/OCR shell scripts>
git diff --check
```

The existing suite proves the intended happy paths and selected negative
wrapper cases, but it does not cover the direct OCR lifecycle bypass,
environment-marker bypass, malicious restore receipt, mutation failure receipt,
post-restart OCR readback, or automatic-restore digest semantics.
