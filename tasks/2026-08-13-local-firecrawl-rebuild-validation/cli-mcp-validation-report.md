# Local Firecrawl CLI and MCP validation

Date: 2026-08-13

## Outcome

The rebuilt local API passed non-AI CLI and direct-helper checks for scrape,
map, search, and crawl submit/status. The MCP wrapper initialized and listed
its tools through the current package. No source code, Docker configuration,
`.env`, or service lifecycle was changed.

Raw, non-sensitive artifacts are confined to
`tasks/tmp/20260813-cli-mcp-validation/`.

## Baseline

`bash scripts/firecrawl-ops/firecrawl_healthcheck.sh --evidence-dir tasks/tmp/20260813-cli-mcp-validation/healthcheck`
passed all four checks at 06:34:41 EDT. Its evidence is:

- `tasks/tmp/20260813-cli-mcp-validation/healthcheck/20260813-063441-firecrawl-healthcheck.json`
- `tasks/tmp/20260813-cli-mcp-validation/healthcheck/20260813-063441-firecrawl-healthcheck.md`

The root endpoint answered as Firecrawl API, and the non-AI scrape smoke
returned 180 Markdown characters. The API, Redis, RabbitMQ, NuQ Postgres, and
Playwright services were up. Compose emitted unset-variable warnings, but none
prevented the tested non-AI paths.

## Static wrapper checks

| Command | Outcome |
|---|---|
| `for TEST_CASE in test_package_and_api_url_overrides test_model_profile_no_recreate_runs_profile_script_without_docker test_model_profile_recreate_runs_docker_and_healthcheck_before_npx; do python3 -m unittest -v "scripts.firecrawl-ops.tests.test_firecrawl_cli_wrapper.FirecrawlCliWrapperTests.${TEST_CASE}"; done` | Passed all three cases. Together with separately run `test_default_npx_invocation_uses_local_api_and_preserves_cwd` and `test_help_and_missing_profile_value_do_not_call_npx`, all five CLI-wrapper unit cases passed. |
| `python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_request.py` | Passed 20 tests. |
| `python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py` | Passed 3 wrapper unit tests and skipped the explicit opt-in stdio smoke. |

## Live CLI checks

The wrapper resolved `firecrawl-cli@latest` to version `1.20.0`. No AI format
was requested.

| Command | Outcome | Saved evidence |
|---|---|---|
| `scripts/firecrawl-ops/firecrawl_cli.sh scrape https://example.com --format markdown,links --json --pretty -o tasks/tmp/20260813-cli-mcp-validation/cli-scrape.json` | Passed. Saved 180 Markdown characters, one link, and HTTP 200 metadata. The CLI scrape JSON is payload-only rather than a `success`/`data` envelope. | `cli-scrape.json` |
| `scripts/firecrawl-ops/firecrawl_cli.sh map https://example.com --limit 5 --json --pretty -o tasks/tmp/20260813-cli-mcp-validation/cli-map.json` | Passed. Returned `success: true` and an empty link list. `example.com` has no discoverable same-site child URLs, so zero links is expected for this target. | `cli-map.json` |
| `scripts/firecrawl-ops/firecrawl_cli.sh search "example domain" --limit 1 --json -o tasks/tmp/20260813-cli-mcp-validation/cli-search.json` | Passed. Returned one web result. | `cli-search.json` |
| `scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1` | Passed. Submitted job `019ffab1-6aae-7657-b9c8-dc3c8c97f0a7`. |
| `scripts/firecrawl-ops/firecrawl_cli.sh crawl 019ffab1-6aae-7657-b9c8-dc3c8c97f0a7 --status` | Passed. The job reached `completed`, with `total: 1` and `completed: 1`. |

The crawl used submit then status polling, not `--wait`, consistent with the
known local wait-hang limit.

## Direct helper checks

| Command | Outcome | Saved evidence |
|---|---|---|
| `scripts/firecrawl-ops/firecrawl_request.py scrape https://example.com --formats markdown,links --pretty --out tasks/tmp/20260813-cli-mcp-validation/helper-scrape.json --save-fields tasks/tmp/20260813-cli-mcp-validation/helper-scrape-fields --quiet --print-paths` | Passed. The full response and split `markdown.md`, `links.json`, and `metadata.json` were written. It has `success: true`, 180 Markdown characters, one link, and a 200 status. | `helper-scrape.json`, `helper-scrape-fields/` |
| `scripts/firecrawl-ops/firecrawl_request.py map https://example.com --limit 5 --pretty --out-dir tasks/tmp/20260813-cli-mcp-validation/helper-map-responses --basename example-map --quiet --print-paths` | Passed. `--out-dir` created the timestamped full response `20260813-063632-example-map.json`. The direct API response exposes top-level `success`, `id`, and `links`, not the CLI's `data` envelope. | `helper-map-responses/20260813-063632-example-map.json` |

## MCP checks

`FIRECRAWL_RUN_MCP_SMOKE=1 python3 -m unittest -v scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py`
failed only in its opt-in integration test after the bounded 15-second read:
`TimeoutError: Timed out waiting for MCP response headers`. The three wrapper
unit tests in the same invocation passed.

This is a test-protocol defect, not a failed MCP wrapper. The wrapper's current
package is `firecrawl-mcp@3.24.0`. A separately bounded live probe that sent
newline-delimited JSON-RPC initialized successfully with server information
`firecrawl-fastmcp 3.24.0`, then returned 26 tools, including
`firecrawl_scrape`, `firecrawl_map`, `firecrawl_search`, `firecrawl_crawl`,
and `firecrawl_check_crawl_status`. The probe terminated its child process
after listing and made no Firecrawl service change.

## Ranked defects

### P1: documented CLI output flags are incompatible with the package selected by the wrapper

The wrapper deliberately resolves `firecrawl-cli@latest`, which is currently
version `1.20.0`. Its command-specific option surface no longer matches the
examples in the checked-in skills:

- `scripts/firecrawl-ops/firecrawl_cli.sh search "example domain" --limit 1 --json --pretty -o tasks/tmp/20260813-cli-mcp-validation/cli-search.json` fails with `error: unknown option '--pretty'`.
- `scripts/firecrawl-ops/firecrawl_cli.sh crawl https://example.com --limit 1 --json -o tasks/tmp/20260813-cli-mcp-validation/cli-crawl-submit.json` fails with `error: unknown option '--json'`.

Search works when `--pretty` is omitted. Crawl works without `--json` or
`-o`, emitting compact JSON to stdout. This is a user-facing compatibility
regression in the documented wrapper workflow, not an API capability failure.
Update the examples and tests for the current CLI, or pin a CLI release whose
per-command output flags match the promised interface.

### P2: MCP integration smoke uses obsolete Content-Length framing

`test_firecrawl_mcp_wrapper.py` writes Content-Length framed messages and
waits for Content-Length response headers. `firecrawl-mcp@3.24.0` responds to
newline-delimited JSON-RPC instead, so the opt-in test times out despite a
working wrapper and server. The failure also leaves Python `ResourceWarning`
messages for unclosed subprocess streams. Update the test's framing and close
all streams in teardown. No change to `firecrawl_mcp.sh` is indicated by the
live probe.

## Expected limits, not defects

- AI-backed summary, query, JSON, extract, and crawl-parameter calls were not
  attempted. This validation intentionally used only non-AI formats; those
  paths remain configuration-dependent on a usable provider credential,
  base URL, and model.
- Crawl `--wait` remains deliberately untested because the local CLI may hang
  after completion. Submit plus status polling completed successfully.
- The empty map result for `example.com` is target-appropriate and still
  proved the map wrapper/API path.
