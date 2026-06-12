# Codex Goal - CRE Brokerage Completion

## Goal

/goal Finish the CRE brokerage listing system so EQUIRE has a verified, URL-only Supabase dataset for every defensibly collectable public for-sale and for-lease listing from CBRE, CBRE Deal Flow, JLL, JLL Investor Center, Cushman & Wakefield, Newmark, Marcus & Millichap, Avison Young, Savills, SVN, NAI Global, Lee & Associates, Colliers, and Transwestern. For each brokerage, find the best public path, improve the collector, capture listing data plus source, broker, contact, profile, VCard, document, and image URLs, collect sale and lease listings, ingest into Supabase `credeals`, and prove the load with saved evidence.

## Boundaries

Start from `START_HERE.md`, `CLAUDE.md`, current status, handoff, lessons, validation report, completion playbook, and `cre_scrapers/brokers/*/README.md`. Use local Firecrawl at `http://localhost:3002`, Browser or devtools for discovery, Python practices, Supabase through `cre_ingest.py` and `psql`, and Vercel only if app evidence is needed. Store PDFs and images as URLs only. Do not download binaries, print secrets, or claim gated, consent-blocked, authenticated, or unsafe POST coverage unless a safe repeatable path is proven.

## Iteration Policy

Work one brokerage at a time. Inspect the site, find public APIs or reliable rendered paths, test pagination and details, patch code, run targeted probes, and update that broker's README and status. Prefer JSON APIs over cards. When details are needed, scrape `rawHtml`, `markdown`, and `links`, parse JSON-LD, scan raw HTML for asset URLs, dedupe media variants, and keep per-listing `detailError` instead of failing a source. After each broker, choose the next highest-coverage gain.

## Verification

Run `bash scripts/firecrawl-ops/firecrawl_healthcheck.sh`, `npm run typecheck`, `python3 -m compileall -q ../cre_scrapers`, and targeted `npx tsx collect.ts --source=<key> --transaction=both --max-items=<small>` probes. For complete sources, run full sale and lease collection, then `python3 cre_ingest.py --in <artifact> --dry-run --keep-artifacts <dir>`. Live ingest only after errors are understood. Validate Supabase counts, duplicates, child orphans, bad URLs, missing titles, invalid states, impossible coordinates, malformed prices or cap rates, missing raw_data, and sample `search_cre_listings`. Use `--mark-missing` only after a clean full run.

## Deliverables

Deliver updated collectors, broker READMEs, `BROKERAGE_STATUS_YYYY-MM-DD.md`, handoff and lesson updates, full JSON artifacts and logs under `out/`, dry-run SQL artifacts where useful, and a validation report with source, collected, and ingested totals, errors, skipped or blocked sources, Supabase proof, and remaining limits. Label brokerages complete only when pagination, detail enrichment, URL-only assets, and Supabase validation are proven. Upload all extracted data to Supabase and verify it.

## Blocked Stop Condition

Stop and report blocked only after at least three serious attempts show no safe repeatable path, or when credentials, site authorization, POST-body support, anti-bot behavior, rate limits, or legal/product constraints prevent defensible collection. Separate complete, partial, and blocked sources, commands run, artifacts, errors, and the next input or integration that would unlock progress.

## Skills And Plugins

Use these and other relevant skills/plugins: [@supabase](plugin://supabase@claude-plugins-official) [@vercel](plugin://vercel@claude-plugins-official) [$supabase:supabase](/Users/caymanseagraves/.codex/plugins/cache/claude-plugins-official/supabase/0.1.11/skills/supabase/SKILL.md) [$build-web-apps:supabase-postgres-best-practices](/Users/caymanseagraves/.codex/plugins/cache/openai-curated/build-web-apps/c6ea566d/skills/supabase-best-practices/SKILL.md) [@python-development](plugin://python-development@claude-code-workflows) [$firecrawl-local-api](/Users/caymanseagraves/.agents/skills/firecrawl-local-api/SKILL.md) [$firecrawl-ops](/Users/caymanseagraves/.agents/skills/firecrawl-ops/SKILL.md).
