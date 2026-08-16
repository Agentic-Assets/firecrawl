# AGENTIC-2280 operator-controls integration report

Date: 2026-08-14

## Completed boundary integration

- `firecrawl_request.py` no longer accepts model-profile, repository-directory,
  Docker-recreate, or healthcheck controls and contains no profile-application
  or Docker mutation path.
- `firecrawl_cli.sh` rejects all prior mutation switches before package
  resolution or `npx`; its help directs operators to the guarded handoff.
- `firecrawl_mcp.sh` remains a package launcher only. A static test protects
  against model-profile, Docker, and healthcheck mutation strings.
- The sole documented agent-facing route for a model or OCR transition is the
  dry-run-first `firecrawl_operator_handoff.py` boundary. No apply was run.
- The one agent-tooling routing paragraph now reflects that separation.

## Focused proof

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
  scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py
# 95 passed, 1 skipped, 122 subtests passed

python3 -m py_compile <operator-boundary Python and focused test files>
bash -n scripts/firecrawl-ops/firecrawl_cli.sh \
  scripts/firecrawl-ops/firecrawl_mcp.sh \
  scripts/firecrawl-ops/set_model_profile.sh \
  scripts/firecrawl-ops/local_firepdf_ocr.sh
uvx ruff check <operator-boundary Python and focused tests>
uvx ruff format --check <same scope>
git diff --check
```

The scoped compilation, shell syntax, Ruff, and whitespace checks passed.
Static boundary tests verify parser rejection and no external launcher call
for rejected CLI options, and absence of a helper or MCP mutation surface.

## Deliberately not exercised

No operator apply, host API call, Docker/OrbStack command, `.env` read or
write, CRE process, Linear update, commit, or push occurred.
