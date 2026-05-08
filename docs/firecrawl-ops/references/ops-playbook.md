# Firecrawl Ops Playbook

## Start / restart
```bash
cd ~/Documents/GitHub/firecrawl
docker compose up -d
# restart
docker compose down && docker compose up -d
```

## Health check
```bash
scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

## Logs
```bash
cd ~/Documents/GitHub/firecrawl
docker compose ps
docker compose logs api --tail 200
docker compose logs playwright-service --tail 200
```

## Safe operations
- Do not expose postgres port publicly.
- Keep `USE_DB_AUTHENTICATION=false` unless you explicitly configure teams/api keys.
- Treat model key and provider keys as secrets; never commit `.env` secrets.

## Benchmark refresh
Use ArtificialAnalysis snapshot script when you need current leaderboard guidance:
```bash
python3 scripts/firecrawl-ops/artificialanalysis_snapshot.py
```

## Env vars (fork-specific)
Set in the repo-root `.env` so `docker-compose.yaml` picks them up:
- `OPENROUTER_API_KEY` — required for the model routing layer
- `MODEL_NAME` — default LLM (rewritten by `scripts/firecrawl-ops/set_model_profile.sh budget|escalated`)
- `SWARM_SUPABASE_URL`, `SWARM_SUPABASE_KEY` — optional, only if using `firecrawl_swarm_pipeline.py` telemetry

Upstream API vars live in `apps/api/.env.example`. Copy that to root `.env` for first-time setup, then layer the fork vars above on top.
