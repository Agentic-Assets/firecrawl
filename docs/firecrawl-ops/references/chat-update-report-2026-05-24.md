# Firecrawl Local Ops Update Report

Date: 2026-05-24

## Summary

This chat brought the fork up to date with upstream Firecrawl, hardened the local self-hosted operations layer, and made local PDF/OCR tooling clearer and safer for Codex, Claude, Cursor, and other agents on this Mac.

## Upstream Sync

- Synced the fork with `firecrawl/firecrawl:main` on a branch instead of directly on `main`.
- Preserved fork-specific ops files, skills, local docs, model-routing guidance, and self-hosted workflow docs.
- Verified protected fork areas remained intact after the sync.

## Local Runtime And CLI

- Confirmed OrbStack is the Docker runtime for this setup.
- Kept Firecrawl available locally at `http://localhost:3002`.
- Added/updated guidance for the local Firecrawl CLI wrapper, direct HTTP helper, and MCP wrapper.
- Clarified that local API/parse usage does not spend Firecrawl cloud credits, though external AI/OCR providers can still have their own costs.
- Added guidance for model profiles including OpenRouter, Vercel AI Gateway, and OpenAI-compatible routing.

## Skills And Cross-Agent Setup

- Refined the repo-canonical skills:
  - `.agents/skills/firecrawl-ops`
  - `.agents/skills/firecrawl-local-api`
  - `.cursor/skills/firecrawl-local-api`
- Added `scripts/firecrawl-ops/sync_agent_skills.sh` so repo skills can be copied to `~/.agents/skills` and symlinked into `~/.codex/skills`, `~/.claude/skills`, and `~/.cursor/skills`.
- Added advisory git hook guidance/reminders so skill updates are easy to propagate after commits and pushes.
- Synced the updated skills to the user-level folders multiple times, including immediately before the final commit.

## Local PDF/OCR Work

- Chose Docling Serve behind a local Fire PDF-compatible adapter as the best local free OCR/layout path.
- Added and documented named OCR profiles such as `research-page-aware`, `tables-accurate`, `tables-fast`, `scanned-english`, `qa-debug`, and `figure-enrichment-lab`.
- Added page-aware OCR output support using `FIRECRAWLPAGEBREAK`.
- Added raw Docling JSON capture for debug/QA workflows.
- Added repeatable benchmark tooling for `fast` / `auto` / `ocr` mode and profile comparisons.
- Updated docs to emphasize that OCR quality is document-dependent and unfamiliar PDFs should be benchmarked.

## OCR Hardening

- Added local OCR concurrency backpressure:
  - Default `LOCAL_FIREPDF_MAX_CONCURRENT_OCR=2`
  - Extra concurrent OCR requests return `SCRAPE_PDF_OCR_BACKPRESSURE` / HTTP 429
- Added Docling timeout mapping:
  - `SCRAPE_PDF_OCR_TIMEOUT` / HTTP 504
  - New Docling starts default `DOCLING_SERVE_MAX_SYNC_WAIT=900`
- Added low-quality OCR detection:
  - Mostly empty output or publisher/license boilerplate returns `SCRAPE_PDF_LOW_QUALITY` / HTTP 422
  - Diagnostic override: `LOCAL_FIREPDF_FAIL_LOW_QUALITY=false`
- Added OCR quality metadata propagation:
  - Successful Firecrawl responses may include `data.metadata.pdfOcr`

## Docs Updated

- `AGENTS.md`
- `LOCAL_DEVELOPMENT_GUIDE.md`
- `.agents/skills/firecrawl-ops/SKILL.md`
- `.agents/skills/firecrawl-local-api/SKILL.md`
- `.cursor/skills/firecrawl-local-api/SKILL.md`
- `docs/firecrawl-ops/references/tools-capabilities.md`
- `docs/firecrawl-ops/references/ops-playbook.md`
- `docs/firecrawl-ops/references/local-pdf-ocr-plan.md`
- `docs/firecrawl-ops/references/local-pdf-ocr-research-agent-plan.md`

## Tests Added Or Expanded

- Expanded FirePDF unit tests for robustFetch failure parsing and typed OCR errors.
- Added serde round-trip tests for new PDF OCR transportable errors.
- Added Python unit tests for local adapter quality heuristics and OCR backpressure behavior.

## Validation Run

- `pnpm build`
- Targeted Jest tests for FirePDF and OCR error serde
- Python adapter unit tests
- Python compile checks
- shell syntax checks
- `git diff --check`
- `scripts/firecrawl-ops/firecrawl_healthcheck.sh`
- `scripts/firecrawl-ops/sync_agent_skills.sh`

## Commit And Push

- Commit: `50610da04 chore: harden local Firecrawl PDF OCR ops`
- Branch pushed: `codex/fix-local-firecrawl-pdf-skills`
- Remaining untracked scratch artifacts: `tasks/tmp/`

