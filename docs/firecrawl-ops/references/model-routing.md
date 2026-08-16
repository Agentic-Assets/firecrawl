# Firecrawl Model Routing

## Target outcome

Maximize throughput and value while preserving quality for hard tasks.

## Local configuration

The API's default model path uses OpenAI-compatible settings:

- `OPENAI_API_KEY`: provider key for OpenRouter, Vercel AI Gateway, or OpenAI
- `OPENAI_BASE_URL`: provider base URL
- `MODEL_NAME`: provider model id
- `MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK`: optional fallback model for invalid
  structured summary or JSON output
- `MODEL_EMBEDDING_NAME`: optional embedding model id

Use the guarded operator handoff from the repo root to inspect the local default
Vercel AI Gateway profile:

```bash
scripts/firecrawl-ops/firecrawl_operator_handoff.py model --profile gateway
```

After reviewing the plan, only a human operator may use the attested `--apply`
path. Agent surfaces must never apply it. If `.env` is missing, use the minimal
human-owned root template in `LOCAL_DEVELOPMENT_GUIDE.md`; do not use
`apps/api/.env.example` as a Docker Compose contract.

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
   - Use only for an explicit, authorized stronger-model window. This profile
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
