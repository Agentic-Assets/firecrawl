Part of [AGENTIC-5134](https://linear.app/agenticassets/issue/AGENTIC-5134) and [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Reasoning-effort evidence, 2026-11-01

The experiment requests `xhigh`; acceptance, provider identity and reasoning-token usage can be measured, but neither proves the exact effective native setting nor a greater per-request compute budget. Gateway catalog lists `xhigh` for all three models and current selected endpoint status 0. Gateway says Chat Completions `reasoning.effort` maps to provider-native configuration, without exposing the exact transformation: https://vercel.com/ai-gateway/models/claude-haiku-5.5 and https://vercel.com/ai-gateway/models/muse-spark-1.3-contributor.

- Claude Haiku 5.5: Anthropic documents xhigh and max; xhigh requires adaptive thinking, not disabled thinking. https://platform.claude.com/docs/en/build-with-claude/effort ; Vercel corroborates xhigh availability and adaptive-thinking requirement https://vercel.com/changelog/claude-haiku-5-5-now-available-on-ai-gateway. Gateway path effective setting remains unverified.
- GPT-6 Luna: OpenAI lists none, low, medium, high, xhigh and max. https://developers.openai.com/api/docs/models/gpt-6-luna. Gateway path effective setting remains unverified.
- Muse Spark 1.3 Contributor: Meta's generic https://dev.meta.ai/docs/reasoning describes xhigh as deeper than high and says max unavailable for Contributor. Its https://dev.meta.ai/docs/cookbook/reasoning-thinking-tokens says xhigh maps to the same strength as high for standard Muse Spark 1.3. These primary pages conflict; whether that mapping applies through Gateway or to Contributor is unresolved. Gateway catalog lists xhigh and max, but do not assume max is supported given Meta's explicit exception. No change in native effort or compute can be inferred from a successful xhigh request. The separately published Muse Spark Open numeric strengths apply to a different open-weight model, not this Contributor route.

No paid inference in this research. Private contributor documents remain out of scope; user explicitly approved the frozen 30-document public corpus under Contributor's training terms.
