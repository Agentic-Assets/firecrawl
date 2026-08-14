# AGENTIC-2278 Compatibility Skeptic Remediation

## Result

All five confirmed compatibility findings are remediated in the consolidated
worktree without changing Docker, root `.env`, CRE collector paths, or runtime
state.

- Normal doctor probes now require the observed CLI and MCP server versions to
  equal the exact manifest pins: `1.20.0` and `3.24.0`.
- Both wrappers reject tags and ranges, including `@latest`, even if the stale
  `FIRECRAWL_HUMAN_UPGRADE_PROBE` environment marker is set. Only the doctor's
  explicit `--run --upgrade-probe --acknowledge-human-upgrade-probe` path uses
  direct, labeled `@latest` commands.
- The preflight opener rejects every redirect before it can be followed; the
  subprocess environment is forced to the previously validated loopback URL.
- The CLI map fixture now validates the observed contract
  `{"success":true,"data":{"links":[]}}` without persisting response bodies.
- MCP process cleanup shares the absolute doctor deadline; a forced kill/reap
  cannot add another wait beyond that deadline.

## Focused proof

- `uvx ruff check ...` and `uvx ruff format --check ...` passed for the doctor
  and compatibility wrapper tests.
- `python3 -m py_compile ...` and `bash -n` for both wrappers passed.
- Focused `unittest`: 39 passed, 1 opt-in live MCP smoke skipped.
- Focused `pytest`: 39 passed, 1 opt-in live MCP smoke skipped, 24 subtests
  passed.
- `git diff --check` passed.

No package resolution, live doctor/API call, Docker action, Linear mutation,
commit, or push was performed.
