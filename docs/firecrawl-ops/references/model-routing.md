# Firecrawl Model Routing (OpenRouter)

## Target outcome
Maximize throughput and value while preserving quality for hard tasks.

## Default routing policy

1. **Budget/base pass**
   - `openrouter/minimax/minimax-m2.5`
   - Use for repetitive/easy/high-volume tasks.

2. **Escalated pass**
   - `moonshotai/kimi-k2.5`
   - Use for harder extraction/reasoning/coding tasks, and all budget-pass failures.

## Escalation rules

Escalate from MiniMax M2.5 -> Kimi K2.5 when:
- extraction confidence is low
- malformed/partial output repeats
- domain pages are noisy/complex
- coding/terminal-style multi-step reasoning is required

## Cost-control rules

- Start with MiniMax M2.5 for bulk jobs.
- Escalate only failed/low-confidence items to Kimi K2.5, not all items.
- Batch work where possible.
- Keep prompts minimal and field-specific for extraction.
