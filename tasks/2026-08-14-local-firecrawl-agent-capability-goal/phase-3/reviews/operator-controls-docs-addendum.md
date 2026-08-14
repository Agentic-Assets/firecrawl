# AGENTIC-2280 operator-controls documentation addendum

Date: 2026-08-14
Scope: consolidated worktree documentation and one static regression only. No runtime, `.env`, Docker/OrbStack, CRE, Linear, commit, or push action was performed.

## Result

The non-blocking P3 documentation drift is closed for the named agent-facing
and local OCR references. Retired direct model/profile/capture/lifecycle
recipes now point to a dry-run `firecrawl_operator_handoff.py` plan and clearly
reserve `--apply` for a reviewed, human-attested operation.

Updated paths:

- `.agents/skills/firecrawl-ops/SKILL.md`
- `.agents/skills/firecrawl-local-api/SKILL.md`
- `scripts/firecrawl-ops/README.md`
- `scripts/firecrawl-ops/CLAUDE.md`
- `docs/firecrawl-ops/references/local-pdf-ocr-plan.md`
- `docs/firecrawl-ops/references/local-pdf-ocr-research-agent-plan.md`
- `docs/firecrawl-ops/references/tools-capabilities.md`
- `docs/firecrawl-ops/references/ops-playbook.md`
- `docs/firecrawl-ops/references/model-routing.md`
- `docs/firecrawl-ops/references/partner-orbstack-onboarding.md`
- `scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py`

The guidance preserves the configured Vercel mappings: `gateway` uses
`deepseek/deepseek-v4-flash-0731`, and `gateway-pro` uses
`deepseek/deepseek-v4-pro-0813`. It does not alter CRE procedures or runtime
configuration.

## Regression and verification

`test_agent_docs_do_not_publish_retired_mutation_commands` statically checks
all ten named documents. It rejects executable examples of the retired model
writer, legacy OCR start/restart/stop aliases, CLI model/Docker wrapper flags,
and request-helper model-profile flags; it also requires the canonical
operator handoff reference in every document.

```text
python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py \
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py \
  scripts/firecrawl-ops/tests/test_set_model_profile.py \
  scripts/firecrawl-ops/tests/test_cre_resource_profile.py
# 35 passed, 59 subtests passed

python3 -m py_compile scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py
git diff --check
```

The globally installed `ruff` binary was unavailable in this worktree, so no
lint result is claimed. The targeted test, Python compile, and diff check all
passed.
