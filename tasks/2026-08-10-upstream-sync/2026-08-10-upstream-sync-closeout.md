# Upstream sync closeout (2026-08-10)

**Branch:** `chore/sync-upstream-2026-08-10`  
**Base:** local fork `main` at `e19464409`; merged `upstream/main` `e72fe3aca` (v2.11.196)  
**Commit:** `5febed6c8`  
**State:** ready for remote branch publication. `main` was not changed or merged. CI has not run.

## Goal

Bring the self-hosted Firecrawl fork current with upstream without losing the local operations, Docling OCR, or governed CRE collector systems.

## What shipped

- Merged 236 upstream-only commits while retaining 298 fork-only commits and all fork-only operations surfaces: `.agents/`, `.claude/`, `.cursor/`, `.githooks/`, `docs/firecrawl-ops/`, `scripts/firecrawl-ops/`, `LOCAL_DEVELOPMENT_GUIDE.md`, and `AGENTS.md`.
- Reconciled the FirePDF collision in `apps/api/src/scraper/scrapeURL/engines/pdf/`: upstream page markdown, async routing, cancellation, ZDR, deadline, and concurrency semantics now coexist with local Docling typed 429/422/504 failures, OCR metadata, and forced-OCR cache bypass.
- Kept the fork's intentional deletion of upstream CI workflows. Kept upstream `SELF_HOST.md` as the short canonical guide and retained local detail in the fork-specific local-development and operations documents.
- Updated dependency policy to upstream pnpm 11.4 and regenerated the API and Playwright lockfiles. Updated the local pnpm/Docker guard accordingly.
- Restored cross-SDK test consistency with the merged cloud keyless-client contract: JS and Rust expectations now permit it; Python async construction now matches the synchronous client; legacy Python tests explicitly use the v1 client API and Python 3.14-safe coroutine execution.

## Verification

Passed on this branch:

- Merge integrity: no unresolved paths; `git diff --check`; upstream is an ancestor of `HEAD`.
- API/operations: `docker compose config -q`; `docker compose build`; `docker compose up -d --force-recreate`; `scripts/firecrawl-ops/firecrawl_healthcheck.sh`; local API smoke matrix; fast and auto PDF parse canary.
- FirePDF: focused FirePDF, async, and cache suite, 49 tests across three files.
- Local Docling: built adapter, started Docling Serve, and sent the repository PDF directly through `/ocr` using `research-page-aware`. It returned one page of markdown with no errors and metadata asserting `safe_for_reuse: false` for OCR cache safety. Temporary Docling and adapter containers were stopped afterward.
- CRE collector: TypeScript typecheck plus 768 unit tests passed. Python collector suite had 2,077 passes and one fixture failure because a local reviewed Cushman artifact is absent. The OM-facts Postgres contract script did not run to completion because its noninteractive Docker pipeline hung; no live ingest or collection was run.
- SDKs/services: Go SDK tests; Rust SDK tests and doc tests; JavaScript SDK 118 unit tests and build; Python SDK 457 unit/compatibility tests; Playwright TypeScript build; Go HTML-to-Markdown tests.

Not proven:

- Full API host `pnpm build` and harness snips cannot run with the Mac's missing FoundationDB native header/stale native module export; the Docker image build and live local API checks passed instead.
- Cloud SDK end-to-end tests need cloud credentials and were not run. Java, .NET, PHP, Elixir, and a supported Ruby runtime are unavailable on this host. Ruby 2.6 is below the SDK's declared Ruby 3.0 minimum.
- No GitHub CI, PR review, remote push, release, deployment, or production CRE write has occurred.

## Decisions made

- Retained the deleted workflows because restoring them would reverse the fork's deliberate zero-spend CI policy.
- Do not cache forced local OCR output. A Docling profile or environment setting can change the document result, so reusing it across OCR or auto calls would be unsafe. Upstream page-markdown cache variants remain for non-OCR modes.
- Did not enable the adapter in the root `.env` for the live API smoke because that mutates the operator's primary model/OCR profile. The adapter contract and API-side typed-error/cache tests prove the integration boundary without changing the steady-state local runtime.

## Left to the operator

- Review and approve the branch before any push, PR, or merge. A merge to `main` requires Cayman approval.
- Restore the reviewed local CRE source fixture only from its governed source of truth before treating the one Python collector test as green.
- Run cloud E2E SDK tests only with authorized cloud credentials and an explicitly approved test target.
