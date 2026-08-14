# Firecrawl Model Routing

## Target outcome
Maximize throughput and value while preserving quality for hard tasks.

## Local configuration

The API's default model path uses OpenAI-compatible settings:

- `OPENAI_API_KEY`: provider key for OpenRouter, Vercel AI Gateway, or OpenAI
- `OPENAI_BASE_URL`: provider base URL
- `MODEL_NAME`: provider model id
- `MODEL_EMBEDDING_NAME`: optional embedding model id

The direct model helper is retired. Agents may request only this dry-run plan:

```bash
scripts/firecrawl-ops/firecrawl_operator_handoff.py model --profile gateway
```

After a reviewed plan, a human operator may use the attested `--apply` path;
agent surfaces must never apply it. If `.env` is missing, use the minimal
human-owned root template in `LOCAL_DEVELOPMENT_GUIDE.md`; do not use
`apps/api/.env.example` as a Docker Compose contract.

## Default routing policy

1. **Budget/base pass**
   - `deepseek/deepseek-v4-flash`
   - Profile: `budget`
   - Base URL: `https://openrouter.ai/api/v1`
   - Use for repetitive/easy/high-volume tasks.

2. **Escalated pass**
   - `deepseek/deepseek-v4-pro`
   - Profile: `escalated`
   - Base URL: `https://openrouter.ai/api/v1`
   - Use for harder extraction/reasoning/coding tasks, and all budget-pass failures.

3. **Gateway pass**
   - `deepseek/deepseek-v4-flash-0731`
   - Profile: `gateway`
   - Base URL: `https://ai-gateway.vercel.sh/v1`
   - Use when the Vercel AI Gateway key is the available provider key.

4. **Gateway Pro pass**
   - `deepseek/deepseek-v4-pro-0813`
   - Profile: `gateway-pro`
   - Base URL: `https://ai-gateway.vercel.sh/v1`
   - Use for difficult extraction after the gateway Flash pass.

5. **OpenAI direct**
   - `gpt-5.4-mini`
   - Profile: `openai-direct`
   - Base URL: `https://api.openai.com/v1`
   - Use when an OpenAI Platform key is available.

## Escalation rules

Escalate from DeepSeek V4 Flash -> DeepSeek V4 Pro when:
- extraction confidence is low
- malformed/partial output repeats
- domain pages are noisy/complex
- coding/terminal-style multi-step reasoning is required

## Cost-control rules

- Start with DeepSeek V4 Flash for bulk jobs.
- Escalate only failed/low-confidence items to DeepSeek V4 Pro, not all items.
- Batch work where possible.
- Keep prompts minimal and field-specific for extraction.

## Troubleshooting

- If logs show `Failed to parse URL from /responses`, `OPENAI_BASE_URL` is missing or invalid.
- If logs show auth/provider failures, verify `OPENAI_API_KEY` is set inside root `.env` and recreate the API container.
- If non-AI scrape/parse works but summary/json fails, fix model env before debugging the endpoint.
- Use exact provider model ids; do not add an extra `openrouter/` prefix to OpenRouter model names.
