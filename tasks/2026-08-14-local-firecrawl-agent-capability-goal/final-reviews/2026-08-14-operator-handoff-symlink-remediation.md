# Operator handoff symlink remediation

Date: 2026-08-14

## Finding addressed

`firecrawl_operator_handoff.py` resolved the injected environment and receipt
paths before rejecting symlinks. A symlink could therefore redirect an
operator-handoff write or receipt outside its intended local boundary.

## Remediation

- Resolve transition paths only after checking every component within the
  transition scope for a symbolic link.
- Keep the canonical CLI paths bounded to the repository root after
  canonicalization.
- Keep unit-test path injection hermetic by deriving a shared transition scope
  from its temporary env and receipt paths; it cannot change CLI paths.
- Add regression coverage for a symlinked root `.env` and for a symlinked
  receipt-directory ancestor. Each refusal happens before API calls, writes,
  or receipt creation.
- Publish receipts through a private `0600` staging file and an exclusive
  hard-link publish, so an existing or symlinked receipt leaf is refused
  without following or overwriting its target.
- Add a patched-UUID regression for an expected receipt leaf that is a
  symlink; the out-of-tree target remains byte-identical.

## Verification

- `python3 -m pytest -q scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py`
  - `25 passed, 24 subtests passed`
- `uvx ruff check scripts/firecrawl-ops/firecrawl_operator_handoff.py scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py`
- `uvx ruff format --check scripts/firecrawl-ops/firecrawl_operator_handoff.py scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py`
- `python3 -m py_compile scripts/firecrawl-ops/firecrawl_operator_handoff.py`
- `git diff --check`

No Docker, `.env`, CRE, database, or runtime configuration was modified.
