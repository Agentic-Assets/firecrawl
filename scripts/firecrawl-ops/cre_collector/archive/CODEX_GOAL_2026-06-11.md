Completed goal record. Do not use this file as current run status. Start with
`START_HERE.md`, `CLAUDE.md`, `HANDOFF_LOG_2026-06-11.md`, and
`LESSONS_2026-06-11.md`.

/goal Finish the Firecrawl CRE listing collector as a verified daily production path for EQUIRE listings for sale and for lease.

Read first: AGENTS.md, scripts/firecrawl-ops/CLAUDE.md, cre_collector/CLAUDE.md, the exported transcript, collect.ts, cre_ingest.py, cre_daily_update.sh, sql/*.sql, docs/firecrawl-ops/references/cre-listing-system-design.md, and prometheus/. Use plugins @supabase, @vercel, @python-development and skills $supabase:supabase, $build-web-apps:supabase-postgres-best-practices, $firecrawl-local-api, $firecrawl-ops before important edits.

Current state: cre_collector already contains the main system. collect.ts uses local Firecrawl at http://localhost:3002 and covers CBRE, CBRE Deal Flow, JLL, JLL Investor, Cushman, Newmark, Marcus & Millichap, Avison Young, Savills, SVN, NAI Global, and Lee. Colliers and Transwestern are unsupported because usable paths appear POST-only. cre_ingest.py upserts JSON into Supabase credeals via psql, cre_daily_update.sh wraps daily refresh, and docs/SQL were updated. The transcript stopped after Newmark and Savills patches, so reverify latest code.

Work in checkpoints. Start with scripts/firecrawl-ops/firecrawl_healthcheck.sh. Run a small probe covering one API source, one rendered source, and one limited or empty source, then run python3 cre_ingest.py --dry-run. Fix real failures. Add missing validation plumbing, especially pinned TypeScript typecheck support with typescript and @types/node if needed. Keep dependencies pinned and edits scoped unless EQUIRE integration requires otherwise.

Next run or resume a full collection with --source=all --transaction=both --max-items=0, safe page caps, and safe concurrency. Save totals, counts, errors, skipped sources, wall-clock time, and exact command. Reconcile known limits: Cushman API path upgraded after the latest ingest and needs a fresh full run, Avison Young first sidebar batch, NAI first batch/no links, Savills US lease empty fallback filtering, Buildout rate limits, Lee tolerance, and Colliers/Transwestern POST-only status. Improve adapters only where a practical local Firecrawl or public GET path exists. Do not fake coverage.

Verify Supabase project fhqycqubkkrdgzswccwd, schema credeals, without printing secrets. Use psql or Supabase MCP safely. Confirm migrations are idempotent, advisors have no unresolved CRE issues, search_path is fixed, RLS/Data API assumptions are documented, and v_cre_* plus search_cre_listings work. Ingest the full run only when source errors are understood. Use --mark-missing only for clean full runs meeting the ingestor floor and per-broker guards. After ingest, query counts by brokerage and transaction_type, recent jobs, soft deletes, and sample searches.

Done when healthcheck, probe, ingest dry-run, Python compile, and TypeScript validation pass or have exact blocker evidence; a latest all-source artifact exists; Supabase ingest is verified or safely deferred with evidence; docs match behavior; daily runner and launchd instructions are correct; git status is understood; no secrets or generated node_modules/out data are staged; and the final report gives files, commands, counts, Supabase proof, known limits, and the next daily-run command.
