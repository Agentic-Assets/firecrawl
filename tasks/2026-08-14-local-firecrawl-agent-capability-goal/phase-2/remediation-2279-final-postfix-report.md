# AGENTIC-2279 final postfix remediation report

Date: 2026-08-14

## Scope

This narrow third pass remediates the final independent verifier's two P1
findings and one P2 provenance gap. No live Firecrawl service, Docker, `.env`,
CRE process, Linear record, branch, commit, or push action was used.

## Remediation

- Added a dedicated internal `agent_safe_result()` compatibility path. It uses
  only a loopback root GET, exact normal CLI `--version` verification, and MCP
  JSONL initialize/`tools/list`. It never invokes the CLI map command. The
  established `doctor --run` path still runs its manifest-defined map probe as
  an explicit operator diagnostic.
- `--agent-safe` now calls the read-only compatibility path, then checks both
  prerequisite timestamps for freshness at the execution gate. Its direct
  queue and active-crawl GETs remain the last checks before the one permitted
  recipe POST.
- Durable receipt validation now checks strict nonfuture UTC timestamp shape
  without applying the 45-second execution TTL. Historical receipts therefore
  remain verifiable, while fresh prerequisites still fail closed before a POST.
- Receipt validation canonicalizes the supplied metrics bytes and recomputes
  their SHA-256 and length. The opaque artifact reference must match the
  receipt ID and those recomputed values.
- The agent tooling note now distinguishes the automatic read-only safe
  compatibility check from the explicit operator `--run` map diagnostic.

## New regressions

- Real helper prerequisite wiring is exercised with the actual read-only
  compatibility function: no CLI map probe occurs, and the observed helper
  calls are queue GET, active-crawl GET, then the one recipe POST.
- An MCP compatibility failure performs no helper request, no map probe, and
  writes no receipt.
- The doctor test proves its agent-safe compatibility path calls CLI version
  and MCP probes but not `run_cli_probe`.
- A controlled-clock test proves a receipt is written and validates after
  execution prerequisites cross the former 45-second receipt-write boundary.
- Receipt tests accept an old valid receipt, reject a future timestamp, and
  reject tampered projected metrics or a mismatched metrics digest.

## Verification

- `python3 -m py_compile` on the helper, preflight, doctor, and five focused
  test modules — passed.
- `python3 -m unittest -v` on the helper, coverage, agent-safe, preflight, and
  compatibility-doctor suites — **99 passed**. The doctor listener is an
  ephemeral loopback-only fixture.
- `uvx ruff check` and `ruff format --check` on the modified helper, doctor,
  and agent-safe/doctor test files — passed.
- Combined `pytest-cov` on the five focused suites — **99 passed**; measured
  helper coverage 92% and preflight coverage 85% (91% aggregate for the
  dynamically measured source files). Annotated coverage output is temporary.
- `git diff --check` — passed.

## Remaining gate

This is static/mocked proof only. A live first-pilot request remains separately
operator-gated and was not attempted.
