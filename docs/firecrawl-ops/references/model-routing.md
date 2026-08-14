# Firecrawl Model Routing

## Target outcome
Maximize throughput and value while preserving quality for hard tasks.

## Local configuration

The API's default model path uses OpenAI-compatible settings:

- `OPENAI_API_KEY`: provider key for OpenRouter, Vercel AI Gateway, or OpenAI
- `OPENAI_BASE_URL`: provider base URL
- `MODEL_NAME`: provider model id
- `MODEL_EMBEDDING_NAME`: optional embedding model id

Use the helper from the repo root. With no argument it selects the local default
Vercel AI Gateway profile:

```bash
scripts/firecrawl-ops/set_model_profile.sh
docker compose up -d --force-recreate api
```

If `.env` is missing, the helper creates a minimal gitignored file. Add the provider key manually before running AI-backed summary/json/query/extract tasks.

## Default routing policy

1. **Gateway default**
   - `deepseek/deepseek-v4-flash-0731`
   - Profile: `gateway` (the no-argument default)
   - Base URL: `https://ai-gateway.vercel.sh/v1`
   - For structured summary/JSON output only, the configured one-time fallback
     is `deepseek/deepseek-v4-pro-0813` when the Flash result is missing or
     schema-invalid. It is not a general per-agent model switch.

2. **Explicit OpenRouter budget alternative**
   - `deepseek/deepseek-v4-flash`
   - Profile: `budget`
   - Base URL: `https://openrouter.ai/api/v1`
   - Use for repetitive/easy/high-volume tasks.

3. **Explicit OpenRouter escalated alternative**
   - `deepseek/deepseek-v4-pro`
   - Profile: `escalated`
   - Base URL: `https://openrouter.ai/api/v1`
   - Use only in an authorized operator window for harder extraction/reasoning
     tasks. It is not an automatic retry for every budget-pass failure.

4. **Gateway Pro operator profile**
   - `deepseek/deepseek-v4-pro-0813`
   - Profile: `gateway-pro`
   - Base URL: `https://ai-gateway.vercel.sh/v1`
   - Use only for an explicit, authorized stronger-model window. The profile
     has no automatic structured-output fallback of its own.

5. **OpenAI direct**
   - `gpt-5.4-mini`
   - Profile: `openai-direct`
   - Base URL: `https://api.openai.com/v1`
   - Use when an OpenAI Platform key is available.

## Escalation rules

For the Gateway default, the API itself may make exactly one Pro retry only for
missing or schema-invalid structured summary/JSON output. All other escalation
decisions require the operator procedure: queue check, exclusive window,
provider-cost approval, recreate, bounded canary, and deliberate handoff.

## Cost-control rules

- Use the Gateway Flash snapshot default when the Vercel provider key is the
  configured local provider.
- Keep OpenRouter budget/escalated profiles as explicit alternatives, never as
  an ambient agent-side switch.
- Batch work where possible.
- Keep prompts minimal and field-specific for extraction.

## Troubleshooting

- If logs show `Failed to parse URL from /responses`, `OPENAI_BASE_URL` is missing or invalid.
- If logs show auth/provider failures, verify `OPENAI_API_KEY` is set inside root `.env` and recreate the API container.
- If non-AI scrape/parse works but summary/json fails, fix model env before debugging the endpoint.
- Use exact provider model ids; do not add an extra `openrouter/` prefix to OpenRouter model names.
