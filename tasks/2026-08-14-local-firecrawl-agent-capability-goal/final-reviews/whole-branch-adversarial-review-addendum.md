# Final adversarial review addendum: remediations re-check

**Review date:** 2026-08-14
**Branch reviewed:** `feat/local-firecrawl-agent-capabilities`
**Base:** `origin/main`
**Scope:** Follow-up review of the operator-handoff symlink remediation, local
environment setup guidance, receipt safety, and CRE isolation. No runtime,
Docker, `.env`, CRE, or Linear action was performed.

## Verdict: MUST-FIX before staging

The original two reported failures are materially improved, but this follow-up
found two remaining safety/comprehension gaps.

### 1. Receipt leaf writes can still follow a symbolic link

`scripts/firecrawl-ops/firecrawl_operator_handoff.py` now correctly rejects a
symlinked `.env` and symlinked receipt-directory ancestors before resolving or
writing. The two new regression tests prove both no-write cases.

However, `write_receipt()` constructs the leaf path and invokes
`Path.write_bytes()` without checking it or using no-follow/exclusive creation.
An extant `<receipt-id>.json` symlink would be followed. The generated UUID4
receipt id makes pre-creation improbable, but the claimed path-safety boundary
should fail closed for the final path too, especially because the code already
rejects a symlinked receipt on restore.

**Required correction:** publish receipt files through a no-follow/exclusive
creation path (or explicitly reject any extant leaf, including a symlink), and
add a regression that fixes the receipt ID, pre-creates a symlink at that leaf,
and proves neither its target nor the env file is changed.

### 2. The revised environment contract is not yet consistent everywhere

`LOCAL_DEVELOPMENT_GUIDE.md` and `AGENTS.md` correctly state that
`apps/api/.env.example` is reference material, not a root Compose contract,
and specify a minimal human-owned root `.env` for AI use. Three remaining
operator-facing paths contradict that contract:

- `docs/firecrawl-ops/references/model-routing.md:21-24`
- `scripts/firecrawl-ops/README.md:309-311`
- `scripts/firecrawl-ops/set_cre_resource_profile.sh:61-65` (a changed
  missing-env diagnostic only; no CRE runtime semantics were changed)

Each says to create root `.env` from `apps/api/.env.example`. Replace that
direction with the reviewed minimal-template/reference path. Extend
`test_operator_mutation_boundaries.py` so it catches the semantic instruction
across line wrapping rather than only the exact substring
`from \`apps/api/.env.example\``.

## Confirmed safe in this pass

- `resolve_scoped_transition_path()` checks every lexical component before
  canonical resolution. The new env and receipt-ancestor tests passed.
- Targeted command passed: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py` — 32 tests.
- `git diff --check origin/main` passed.
- A changed-path CRE scan found no `cre_collector`, CRE SQL/scraper,
  EQUIRE/Supabase, launchd, or OM-identity changes. The sole
  `set_cre_resource_profile.sh` diff is the text diagnostic cited above.
- All eight committed `tasks/agentic-2279/evidence/*.json` artifacts contain
  body-free receipts/metrics only: `body_retained_bytes: 0`, no retained input
  or absolute path, and no secret-bearing values. Their live receipts remain
  suitable to commit.

## Re-review gate

After the two corrections, rerun the 32 targeted tests above, add the leaf
symlink regression, run `git diff --check origin/main`, and re-scan the three
environment-guidance paths before staging.
