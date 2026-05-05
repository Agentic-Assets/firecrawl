---
name: firecrawl-ops
description: Operate, explain, and optimize a self-hosted Firecrawl Docker stack (local or server). Use when the user asks about Firecrawl capabilities/tools/endpoints, setup/runtime health, model cost-performance routing, scraping workflows, or benchmark-informed model selection (including ArtificialAnalysis-based comparisons).
---

# Firecrawl Ops

Use this skill to run and explain Firecrawl as an internal scraping platform.

## Quick workflow

1. Verify runtime health first (`scripts/firecrawl_healthcheck.sh`).
2. Pick the right endpoint from `references/tools-capabilities.md`.
3. Apply model profile policy from `references/model-routing.md`.
4. For model intelligence/cost refresh, run `scripts/artificialanalysis_snapshot.py`.
5. If making persistent runtime changes, update the Firecrawl `.env` and restart compose.

## Default model policy

Use this routing unless the user overrides:
- Budget/base pass: `openrouter/minimax/minimax-m2.5`
- Escalated pass: `moonshotai/kimi-k2.5`

For repetitive high-volume low-risk tasks, prefer MiniMax M2.5 first.
For hard extraction/reasoning/coding, escalate to Kimi K2.5.

## Persistent runtime settings

Use `scripts/set_model_profile.sh <profile>` to update Firecrawl runtime model defaults in:
- `~/Documents/GitHub/firecrawl/.env`

Profiles:
- `budget` (MiniMax M2.5 default)
- `escalated` (Kimi K2.5 default)

Then restart:
- `docker compose down && docker compose up -d`

## References

- `references/tools-capabilities.md` - endpoint-by-endpoint capability map
- `references/model-routing.md` - model strategy, escalation rules, and profile choices
- `references/ops-playbook.md` - health checks, debugging, and safe operations
- `references/cayman-use-cases-and-playbooks.md` - mapped responses to Cayman workflow ideas (research/CRE/coding)
- `references/cre-access-matrix.md` - CRE platform accessibility matrix (what's scrapable vs blocked)
- `references/google-flights-scraping.md` - Google Flights scraping for travel deals

## Scripts

- `scripts/firecrawl_healthcheck.sh` - verify local Firecrawl is running
- `scripts/set_model_profile.sh` - update LLM model profile (budget/escalated)
- `scripts/artificialanalysis_snapshot.py` - refresh model benchmark data
- `scripts/platform_access_probe.py` - test accessible vs blocked sources quickly
- `scripts/cre_access_matrix.py` - CRE platform accessibility testing (news/research/brokerage/government)
- `scripts/bulk_triage_runner.py` - budget-first triage and escalation batch generation
- `scripts/crawl_swarm.py` - parallel map+scrape swarm runner from seed URLs
- `scripts/firecrawl_swarm_pipeline.py` - end-to-end source-probe -> budget -> escalated with confidence/provenance output
- `scripts/google_flights_scrape.py` - multi-region Google Flights deal scraper (Atlas)
- `scripts/parse_flight_deals.py` - parse Firecrawl markdown to CSV/JSON (Atlas)

## Google Flights Scraping (Atlas Use Case)

For travel deal scraping, Firecrawl works excellently with Google Flights explore pages.

**Working URL patterns:**
- `https://www.google.com/travel/explore?q=flights+from+Tulsa` - General explore
- `https://www.google.com/travel/explore?q=flights+from+Tulsa+to+Hawaii` - Region-specific
- `https://www.google.com/travel/explore?q=flights+from+Tulsa+to+Europe` - Destination type
- `https://www.google.com/travel/explore?q=flights+from+Tulsa+to+Mexico&curr=USD&hl=en` - Full params

**Optimal scrape settings:**
```json
{
  "url": "https://www.google.com/travel/explore?q=flights+from+Tulsa+to+Hawaii&curr=USD&hl=en",
  "formats": ["markdown"],
  "waitFor": 8000,
  "onlyMainContent": false
}
```

**What you get:** Structured markdown with destination, dates, duration, stops, and price - ready to parse.

**Reference:** See `references/google-flights-scraping.md` for full workflow and parser script.

## CRE Platform Access Matrix

For CRE (Commercial Real Estate) research, not all platforms are scrapable. Use `scripts/cre_access_matrix.py` to test accessibility.

**🟢 Fully accessible:** CBRE, Cushman Wakefield, GlobeSt, Census, FRED, REIT.com, Marcus & Millichap (teasers)
**🟡 Partial/teasers:** Marcus & Millichap (full reports gated), NREI, PI Executive, PREA, ULI
**🔴 Blocked:** CoStar, LoopNet, Knight Frank (bot protection)

Run quick test:
```bash
python3 scripts/cre_access_matrix.py --sources news
python3 scripts/cre_access_matrix.py --sources research --output cre_results.json
```

See `references/cre-access-matrix.md` for full source-by-source breakdown and recommended workflows.

## Supabase integration (optional)

If you want persistent swarm telemetry, apply:
- `references/supabase-schema-firecrawl-swarm.sql`

Then set env vars before running swarm pipeline:
- `SWARM_SUPABASE_URL`
- `SWARM_SUPABASE_KEY`
