# Firecrawl Model Routing (OpenRouter)

## Target outcome
Maximize throughput and value while preserving quality for hard tasks.

## Default routing policy

1. **Budget/base pass**
   - `deepseek/deepseek-v4-flash`
   - Use for repetitive/easy/high-volume tasks.

2. **Escalated pass**
   - `deepseek/deepseek-v4-pro`
   - Use for harder extraction/reasoning/coding tasks, and all budget-pass failures.

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
