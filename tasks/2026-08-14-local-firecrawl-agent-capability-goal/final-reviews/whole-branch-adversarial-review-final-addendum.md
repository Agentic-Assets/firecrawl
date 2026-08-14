# Final adversarial review: remediation verification addendum

**Review date:** 2026-08-14
**Branch reviewed:** `feat/local-firecrawl-agent-capabilities`
**Base:** `origin/main`
**Scope:** Final narrow verification of the two prior addendum findings only.
No runtime, Docker, `.env`, CRE, or Linear action was performed.

## Verdict: PASS

### Receipt publication

`write_receipt()` now rejects an already present or symlinked destination,
creates a private staging file with exclusive creation and `O_NOFOLLOW` when
the platform provides it, then atomically publishes with `os.link` without
following links. An existing final receipt leaf therefore fails closed and is
never overwritten or followed.

`test_receipt_symlink_leaf_is_refused_without_an_outside_write` fixes the UUID,
pre-creates an external-target symlink at the final receipt leaf, and verifies
the symlink and external file remain unchanged. The pre-existing `.env` and
receipt-directory-ancestor symlink tests remain present.

### Environment contract and CRE boundary

The corrected guidance in `model-routing.md` and the ops README now directs
humans to the minimal root template in `LOCAL_DEVELOPMENT_GUIDE.md`. The
CRE-only missing-env diagnostic has the same wording, with a dedicated test
that rejects both the old upstream-example and retired profile-helper advice.

The documentation guard now uses a DOTALL bootstrap pattern, covers the exact
agent-facing references that previously drifted, and requires a Compose-contract
warning wherever the upstream reference file is mentioned. A broad source scan
found no remaining instruction to create/copy root `.env` from
`apps/api/.env.example`.

### Verification

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py
  scripts/firecrawl-ops/tests/test_cre_resource_profile.py` passed: **37 tests**.
- `git diff --check origin/main` passed.
- A changed-path scan found no CRE collector, CRE SQL/scraper, EQUIRE/Supabase,
  launchd, or OM-identity change. `set_cre_resource_profile.sh` retains the
  same runtime behavior; only its missing-env diagnostic changed.
- Previously reviewed `tasks/agentic-2279/evidence` receipts and metrics remain
  body-free and secret-free; this remediation did not alter them.
