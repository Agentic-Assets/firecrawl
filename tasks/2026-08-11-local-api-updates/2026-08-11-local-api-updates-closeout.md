# Local API updates closeout (2026-08-11)

**Branch:** `fix/local-api-safe-search-evidence`
**Base:** `main` / `origin/main` at `9efc59fe2`
**Implementation commit:** `7520ed084` (`fix: refresh local API capability tooling`)
**State:** committed locally; not pushed, reviewed, or merged. GitHub CI has not run.

## Goal

Adopt the useful parts of the completed upstream sync for the self-hosted local API and CRE tooling, while preserving the CRE data-ownership boundary and local Docling limitations.

## What shipped

- Added opt-in `--safe true|false` to `scripts/firecrawl-ops/firecrawl_request.py` for upstream v2 exploratory search. Omission preserves the server default. The helper test asserts both explicit forwarding and omission.
- Refreshed the generated local capability matrix from a current runtime smoke matrix. Nine core local API probes passed; optional browser, agent, and support probes remained deliberately skipped.
- Hardened `apps/api/Dockerfile` against stale ignored NAPI artifacts. The image now rebuilds the native package and asserts `convertDocumentToMarkdown` before TypeScript compilation; `apps/api/.dockerignore` excludes its generated declaration from Docker context.
- Documented the opt-in search policy and the intentionally deferred page-level PDF markdown contract.

## Verification

Passed on `7520ed084`:

- `docker compose up -d --build --force-recreate api`: image build passed, including native export assertion and `pnpm run build`.
- `scripts/firecrawl-ops/firecrawl_healthcheck.sh`: local stack, root endpoint, and scrape smoke passed after the rebuild.
- Local `safe:true` `/v2/search` call succeeded with a `web` result set.
- `python3 scripts/firecrawl-ops/local_api_smoke_matrix.py`: 9 core probes passed; 4 optional mutating/service-dependent probes skipped by design.
- `python3 scripts/firecrawl-ops/tests/test_firecrawl_request.py`: 20 passed.
- `pnpm --dir apps/api exec tsc --noEmit`; native export check; document-converter snip: 6 passed.
- `npm test` in `scripts/firecrawl-ops/cre_collector`: typecheck plus 768 unit tests passed without live collection or ingestion.
- `python3 scripts/firecrawl-ops/check_pnpm_docker_config.py`, `docker compose config -q`, and `git diff --check` passed.

## Decisions made

- The CRE listings UI remains a governed CRE database-view/RPC consumer. It is not a Firecrawl client, so no UI wiring change is appropriate.
- The CRE collector retains source-specific acquisition and explicit `maxAge: 0` policies. Safe-search is available only as an explicit helper option because forcing it can reduce research recall.
- Page-level PDF markdown remains unavailable through the local helper. The current Docling adapter does not return the required physical `pages:[{page,markdown}]` contract, and the API correctly fails rather than fabricating it.
- The NAPI build failure was a stale generated local artifact, not an upstream source mismatch. Rebuilding and asserting the native export in Docker fixes the actual deployment boundary without committing generated artifacts.

## Left to the operator

- Review, push, and open a PR for `fix/local-api-safe-search-evidence` if desired. A merge to `main` still requires Cayman approval.
- Do not enable optional upstream browser, agent, exchange, research, Slack, SIEM, or monitor routes until their required services, credentials, and operating policy are explicitly configured.
