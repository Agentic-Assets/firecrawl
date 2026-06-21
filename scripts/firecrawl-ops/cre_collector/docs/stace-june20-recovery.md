# Stace June20 Recovery Notes

## Verdict

Use `origin/stace-june20` as source research and adapter source code only. Do not merge it wholesale.

## Confirmed Useful Adapters

- Matthews: sitemap enumeration plus throttled plain fetch.
- Franklin Street: dual Buildout plugin tokens.
- SRS: open Cloud Run search API.
- Hanley: embedded `rethink_properties` JSON.
- Kidder Mathews: open public listing API.

## Research To Preserve

- Buildout firm token list and discovery workflow.
- Top-30 feasibility notes.
- Voit LoopLink and CoStar dead-end warning.
- Generic sitemap plus LLM extraction design.

## Write Risk

`cre_ingest_rest.py` can delete and replace active brokerage rows. It must not be used until current-main mark-missing, source completeness, and dry-run gates are ported into it.
