# AGENTIC-2278 Post-Remediation Independent Verification

Date: 2026-08-14

## Scope and method

This was a static, independent review of the sole consolidated worktree on
`feat/local-firecrawl-agent-capabilities`. It read the earlier finder/skeptic
reports, the manifest, doctor, wrappers, wrapper/doctor tests, and the affected
operator guidance. It did not resolve packages, call the local API, run Docker,
read or write `.env`, run a live MCP smoke, touch CRE code, mutate Linear, or
commit/push.

## Result

At review time, one local-only safety finding remained. The five findings from
the prior skeptic report were otherwise remediated and their focused fixture
suite passed. The remediation addendum below records the subsequent proxy fix;
the host doctor itself remains unproven until an explicit operator run is
performed.

### P1 — API preflight can use an ambient outbound proxy

**Status: CONFIRMED.** `preflight_api` creates its opener with only
`_RejectRedirects` (`scripts/firecrawl-ops/firecrawl_compatibility_doctor.py:191-205`).
`urllib.request.build_opener` adds its normal `ProxyHandler` when an ambient
`http_proxy`/`HTTP_PROXY` value is present. Thus a doctor invoked with a proxy
environment can send its nominal `GET http://localhost:3002/` preflight to an
outbound proxy before redirect policy applies. That violates the documented
loopback-only boundary and leaves a source-request disclosure path.

**Independent evidence:** a no-request standard-library construction with
`http_proxy=http://proxy.invalid:8080` produced an opener containing
`ProxyHandler {'http': 'http://proxy.invalid:8080'}`. The doctor imports and
uses that same `build_opener` without a `ProxyHandler({})` override. The
existing redirect test mocks `build_opener`, so it does not exercise the proxy
case.

**Narrow correction:** construct the preflight opener with an explicit empty
`ProxyHandler({})` plus `_RejectRedirects`, then add a fixture that sets both
case variants of proxy variables and proves the constructed opener has no
proxies. Reassess child-process proxy handling separately: package resolution
may require a proxy, while traffic to the loopback Firecrawl URL must not be
routed through one.

## Prior findings rechecked

- **Normal pin/version mismatch: REFUTED after remediation.**
  `_require_normal_pin_version` compares the observed CLI and MCP versions to
  the manifest pins in normal mode (`:244-248`, `:298-299`, `:387-392`), with
  separate CLI and MCP mismatch tests.
- **Direct wrapper `@latest` bypass: REFUTED after remediation.** Both wrappers
  call `--validate-package-spec` for any override (`firecrawl_cli.sh:14-23`,
  `firecrawl_mcp.sh:14-23`); the validator accepts only exact `name@x.y.z`
  semver (`firecrawl_compatibility_doctor.py:155-162`). Tests prove `@latest`
  fails even with `FIRECRAWL_HUMAN_UPGRADE_PROBE=1`. Deliberate exact-semver
  overrides remain allowed, as required by the approved compatibility design.
- **CLI map envelope: REFUTED after remediation.** The probe now requires
  `success: true` and a list at `data.links` (`:323-330`), matching the pinned
  CLI response shape; the positive and negative fixture tests cover it.
- **MCP JSONL/version inventory: REFUTED after remediation.** The doctor sends
  `initialize`, `notifications/initialized`, and `tools/list` (`:470-498`),
  checks JSON-RPC IDs/inventory, and requires the normal manifest version in
  `serverInfo.version` (`:355-392`).
- **Deadline including cleanup: REFUTED after remediation.** Cleanup receives
  the original absolute deadline (`:422-445`, `:504-505`); the fixture proves
  termination and forced kill cannot add a second wait beyond it.
- **Body-free/redacted output: REFUTED for the documented result surface.**
  Results retain only enumerated failure codes, package/version metadata, map
  status, MCP tool count, and `body_bytes_persisted: 0`; child stdout/stderr is
  never echoed or written. A future hardening pass could add byte caps to
  in-memory child stdout reads, but that is not a persisted-body leak.
- **Documentation accuracy: PARTIALLY REFUTED.** Pin, explicit upgrade, map,
  MCP, and no-body claims match the code in the reviewed skills and reference
  docs. The statements that `--run` is loopback-only need the proxy correction
  above before they are fully true.

## Verification performed

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  scripts.firecrawl-ops.tests.test_firecrawl_compatibility_doctor \
  scripts.firecrawl-ops.tests.test_firecrawl_cli_wrapper \
  scripts.firecrawl-ops.tests.test_firecrawl_mcp_wrapper

26 passed, 1 opt-in live MCP smoke skipped
```

The above is fixture-only coverage. It does not establish that the current
host resolves the pinned npm packages, that the local API is healthy, or that
the live MCP server answers correctly.

## Remediation addendum

The confirmed P1 is remediated in the same consolidated worktree.

- `preflight_api` now uses `ProxyHandler({})` with `_RejectRedirects`, so
  ambient proxy settings cannot route its direct loopback health request.
- A local-only fixture starts a target server and a poisoned proxy server under
  `http_proxy` and `HTTP_PROXY`; the preflight reaches the target exactly once
  and the proxy zero times.
- The pinned CLI and MCP clients use Axios, whose Node adapter consults proxy
  environment variables. Therefore the doctor must also protect the child map
  and MCP API calls, not only its Python preflight. `_command_environment` now
  preserves proxy settings for `npx` package resolution but writes the merged
  loopback exclusions to both `NO_PROXY` and `no_proxy`; the focused fixture
  verifies the preserved proxy settings and exact loopback exclusions.

Post-fix verification passed: `py_compile`, the 28-test compatibility doctor
and wrapper fixture suite (one opt-in live MCP smoke skipped), `bash -n`, Ruff
lint and format checks, and `git diff --check`. The local target/proxy servers
are test fixtures only; no Firecrawl API, npm package, Docker, `.env`, CRE, or
Linear action was performed.
