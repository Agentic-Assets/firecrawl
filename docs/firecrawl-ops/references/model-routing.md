# Firecrawl Model Routing

## Target outcome
Maximize throughput and value while preserving quality for hard tasks.

## Local configuration

The API's default model path uses OpenAI-compatible settings:

- `OPENAI_API_KEY`: provider key for OpenRouter, Vercel AI Gateway, or OpenAI
- `OPENAI_BASE_URL`: provider base URL
- `MODEL_NAME`: provider model id
- `MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK`: optional, bounded retry model for
  missing or schema-invalid JSON extraction or summary output
- `MODEL_EMBEDDING_NAME`: optional embedding model id

Use the helper from the repo root:

```bash
scripts/firecrawl-ops/set_model_profile.sh
docker compose up -d --force-recreate api
```

If `.env` is missing, the helper creates a minimal gitignored file. Add the provider key manually before running AI-backed summary/json/query/extract tasks.

## Default routing policy

1. **Gateway pass (default local profile)**
   - Primary: `deepseek/deepseek-v4-flash-0731`
   - Structured-output fallback: `deepseek/deepseek-v4-pro-0813`
   - Profile: `gateway`
   - Base URL: `https://ai-gateway.vercel.sh/v1`
   - Use when the Vercel AI Gateway key is the available provider key. Flash
     handles normal work. For JSON extraction and summary, a missing or
     schema-invalid result retries once with Pro; a schema-valid direct
     provider response is accepted without retry.

2. **Budget alternative**
   - `deepseek/deepseek-v4-flash`
   - Profile: `budget`
   - Base URL: `https://openrouter.ai/api/v1`
   - Use only with an OpenRouter key for repetitive/easy/high-volume tasks.
     This explicit single-model profile has no automatic Pro fallback.

3. **Escalated alternative**
   - `deepseek/deepseek-v4-pro`
   - Profile: `escalated`
   - Base URL: `https://openrouter.ai/api/v1`
   - Use only with an OpenRouter key for deliberately harder extraction or
     reasoning tasks.

4. **OpenAI direct**
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
- Keep `MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK` empty for profiles where no
  automatic structured-output escalation is wanted.
- Batch work where possible.
- Keep prompts minimal and field-specific for extraction.

## Troubleshooting

- If logs show `Failed to parse URL from /responses`, `OPENAI_BASE_URL` is missing or invalid.
- If logs show auth/provider failures, verify `OPENAI_API_KEY` is set inside root `.env` and recreate the API container.
- If non-AI scrape/parse works but summary/json fails, fix model env before debugging the endpoint.
- Use exact provider model ids; do not add an extra `openrouter/` prefix to OpenRouter model names.
