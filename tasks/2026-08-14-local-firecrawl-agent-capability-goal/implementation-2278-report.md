# AGENTIC-2278 Implementation Report

Date: 2026-08-14. Implemented in isolated worktree
`/private/tmp/firecrawl-tooling-compatibility` on
`feat/agentic-2278-tooling-compatibility`; no commit, push, Linear mutation,
package resolution, live doctor run, Docker action, root `.env` write, or CRE
change was performed.

## Delivered

- `scripts/firecrawl-ops/firecrawl_tooling_compatibility.json` is the one
  non-secret source for normal candidate pins: `firecrawl-cli@1.20.0` and
  `firecrawl-mcp@3.24.0`. It records the bounded map probe, JSONL MCP
  requirements, and a distinct HUMAN-ONLY `@latest` upgrade-probe contract.
- `scripts/firecrawl-ops/firecrawl_compatibility_doctor.py` has a static
  default that emits only body-free JSON. Its opt-in `--run` accepts only
  `http://localhost|127.0.0.1|[::1]:3002`, uses a 45-second total deadline,
  does a bounded map check, validates newline-delimited JSON-RPC initialize and
  tools-list, captures no response artifact, redacts child output/key values,
  and fails closed with enumerated codes.
- `firecrawl_cli.sh` and `firecrawl_mcp.sh` now read manifest defaults when no
  package override is set. Overrides must be exact compatible package semver;
  tags/ranges, including `@latest`, always fail in wrappers. Only the doctor's
  explicitly acknowledged `--run --upgrade-probe` path may invoke its separate
  HUMAN-ONLY probe. MCP now also forces npm logs to stderr at `error` level,
  preserving protocol stdout.
- Added doctor fixture coverage and extended both wrapper suites for exact
  defaults and rejected `@latest`, including when a stale human-marker
  environment variable is supplied.
  Updated the two agent skills and three ops references so normal agent usage
  no longer documents `@latest`.

## Static Verification

Passed without package resolution or host access:

```text
uvx ruff check scripts/firecrawl-ops/firecrawl_compatibility_doctor.py scripts/firecrawl-ops/tests/test_firecrawl_compatibility_doctor.py
uvx ruff format --check scripts/firecrawl-ops/firecrawl_compatibility_doctor.py scripts/firecrawl-ops/tests/test_firecrawl_compatibility_doctor.py
python3 -m py_compile scripts/firecrawl-ops/firecrawl_compatibility_doctor.py
bash -n scripts/firecrawl-ops/firecrawl_cli.sh scripts/firecrawl-ops/firecrawl_mcp.sh
python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_compatibility_doctor.py scripts/firecrawl-ops/tests/test_firecrawl_cli_wrapper.py scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py
git diff --check
```

The focused suite ran 22 tests successfully; its only skipped test was the
pre-existing `FIRECRAWL_RUN_MCP_SMOKE=1` live MCP smoke. Generated
`scripts/firecrawl-ops/__pycache__` and
`scripts/firecrawl-ops/tests/__pycache__` were removed afterward.

## Remaining Gate

Run the doctor only after the host health preflight is deliberately authorized:
`python3 scripts/firecrawl-ops/firecrawl_compatibility_doctor.py --run`.
Record its versioned, body-free result before accepting these candidate pins as
current-host proof. The HUMAN-ONLY upgrade probe is separate and cannot update
the manifest automatically. `sync_agent_skills.sh --dry-run` still reports
existing non-symlink Claude skill destinations; leave them untouched without a
separate operator decision.
