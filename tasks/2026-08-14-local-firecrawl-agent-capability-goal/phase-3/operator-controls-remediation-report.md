# AGENTIC-2280 operator-controls remediation report

Date: 2026-08-14

## Remediated findings

- `set_model_profile.sh` is now an unconditional, non-mutating retirement
  message. It ignores arguments and environment variables, so an ambient
  handoff marker cannot authorize a write. The attested handoff writes only
  the three model-routing keys with its atomic Python writer.
- The handoff CLI no longer accepts alternate repository, env, or receipt
  paths. Production uses only the root `.env` and canonical receipt directory;
  unit tests use an explicit in-process path seam.
- Restore validates the full handoff receipt schema, a retained `model` or
  `ocr-routing` source operation, exact allowlisted key set and maps, target
  mapping, source digest, and no chained source receipt. Secret and CRE keys
  are rejected before queue checks or writes.
- Receipts are schema-validated before writing and contain only the explicit
  non-secret model/OCR maps plus digests. A normal automatic restore records a
  separate transition digest and the actual final env digest.
- After mutation begins, runner, health, final-idle, or adapter-readback
  failure attempts a byte-exact automatic restore only when the transition
  image still matches, then writes a mode-0600 redacted failure receipt with
  the actual final digest. Divergence remains `manual-handoff-required`.
- `ocr-adapter` restarts through the attested handoff with fixed no-capture
  arguments and requires a post-restart safe `/settings` readback before its
  retained receipt is issued. Direct `start`, `start-adapter`, and
  `restart-adapter` now invoke that handoff rather than Docker helpers; they
  neither create the OCR state/output directories nor invoke Docker directly.
- The CRE resource-profile missing-env message now directs an operator to the
  upstream env template and does not point to the retired profile writer.

## Deliberate remaining boundary

`local_firepdf_ocr.sh start-docling` and `restart` still expose the older
Docling lifecycle behavior. Replacing them safely requires a dedicated,
attested `ocr-docling` handoff operation that can preserve the documented
container-exists behavior without retaining raw settings or adding an ambient
authorization marker. This remediation did not execute or alter that runtime
path. No claim of full direct-lifecycle closure should be made until that
operation is implemented and these two dispatches are routed through it.

## Verification

```text
python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_firecrawl_cli_wrapper.py \
  scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py \
  scripts/firecrawl-ops/tests/test_firecrawl_request.py \
  scripts/firecrawl-ops/tests/test_firecrawl_request_coverage.py \
  scripts/firecrawl-ops/tests/test_firecrawl_agent_safe.py \
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py \
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py \
  scripts/firecrawl-ops/tests/test_set_model_profile.py \
  scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py \
  scripts/firecrawl-ops/tests/test_cre_resource_profile.py
# 103 passed, 1 skipped, 138 subtests passed

python3 -m py_compile <changed handoff and focused test modules>
bash -n scripts/firecrawl-ops/set_model_profile.sh \
  scripts/firecrawl-ops/local_firepdf_ocr.sh \
  scripts/firecrawl-ops/set_cre_resource_profile.sh
uvx ruff check <changed handoff and focused test modules>
uvx ruff format --check <same scope>
git diff --check
```

No handoff apply, local API, Docker/OrbStack, `.env` read/write, CRE run,
Linear change, commit, or push occurred.
