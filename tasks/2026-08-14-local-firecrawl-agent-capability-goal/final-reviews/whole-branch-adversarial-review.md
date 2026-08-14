# Consolidated Branch Final Adversarial Review

Date: 2026-08-14
Branch reviewed: `feat/local-firecrawl-agent-capabilities`
Base reviewed: `origin/main`
Method: independent static review of the complete diff, task evidence, the
current source and documentation contracts. No host/API, Docker/OrbStack,
root `.env`, CRE runtime, Linear, commit, or push action was performed.

## Verdict: MUST-FIX BEFORE STAGING

Two confirmed findings remain. They are independent of the earlier preflight,
compatibility, agent-safe-pilot, and operator-control remediation reports.

### P1: The guarded operator handoff follows `.env` and receipt symlinks

**Evidence:** `scripts/firecrawl-ops/firecrawl_operator_handoff.py:972-974`
calls `.resolve()` for `env_path` and `receipt_dir` before `read_env()` or
`write_receipt()` gets a chance to check the path. `read_env()` therefore sees
the resolved target rather than the original symlink. A root `.env` symlink can
redirect an attested `--apply` transition to another file, and a receipt-dir
symlink can direct durable receipts outside the repository/tasks boundary.

**Failure scenario:** a stale or accidental `.env` symlink makes a
human-attested model/OCR transition overwrite a non-repository target. This is
not a protection boundary against a hostile same-UID process, but it is an
avoidable and unsafe filesystem surprise for the documented operator path.

**Required correction:** preserve the lexical paths long enough to reject a
symlinked `.env` and every existing component of the receipt path before any
read, write, chmod, or directory creation. Resolve only after that validation,
then ensure the receipt directory remains beneath the canonical repository
`tasks/` root. Add mocked fixture coverage for a symlinked `.env` and both a
symlinked receipt directory and parent component; prove no write, Docker call,
or out-of-tree file appears.

### P1: Canonical local setup/instructions still advertise retired mutation paths

**Evidence:** `LOCAL_DEVELOPMENT_GUIDE.md:24,83,104-123,164,239-281` still
directs users to `set_model_profile.sh`, wrapper profile flags, and direct
local OCR lifecycle/profile commands. The consolidated source now makes the
model writer unconditionally exit 2, rejects the wrapper flags, and routes
lifecycle operations to `firecrawl_operator_handoff.py`. Project
`AGENTS.md:52,110` also still describes `set_model_profile.sh` as the root
`.env` writer, despite being the operative repository guidance.

The replacement wording in the updated onboarding documents says to create
root `.env` from `apps/api/.env.example`, while `SELF_HOST.md:40-41` explicitly
says that upstream example is not a drop-in Compose contract. That leaves a
fresh-clone path internally contradictory.

**Failure scenario:** a new operator follows the top-level local guide or
canonical repo instructions, receives an unexplained refusal, or copies an
upstream application env file into Compose without a reviewed local bootstrap
contract. The resulting user experience defeats the promised simplified,
operator-only safety boundary and risks incorrect local configuration.

**Required correction:** revise `LOCAL_DEVELOPMENT_GUIDE.md` and the local
ops entries in root `AGENTS.md` in the same change as the new handoff. Replace
all retired command examples with dry-run and human-attested handoff examples.
Choose one safe first-run root-env procedure consistent with `SELF_HOST.md`:
either add/reuse a purpose-built non-secret Compose template, or require a
minimal manual root env with clearly documented Compose variables. Do not
describe `apps/api/.env.example` as the Compose bootstrap unless that upstream
document is made compatible. Extend the documentation-boundary test to include
the local development guide and the repo instructions.

## Confirmed safe/non-regression checks

- `git diff --check origin/main` passed.
- The diff remains in fork-owned ops/docs/skills/test/task paths. It does not
  modify `scripts/firecrawl-ops/cre_collector/`, CRE adapters/SQL, EQUIRE,
  Supabase/Postgres integration, launchd, or OM-facts paths. The only
  CRE-profile source change is its missing-env guidance.
- The configured gateway mappings in the operator handoff are correct:
  `gateway` uses `deepseek/deepseek-v4-flash-0731` and its structured fallback
  is `deepseek/deepseek-v4-pro-0813`; `gateway-pro` uses the latter directly.
- The four checked-in `tasks/agentic-2279/evidence/` receipt/metric pairs are
  body-free. Their helper and tooling-manifest SHA-256 values match the
  reviewed current files; they contain no source body, URL, header, job ID,
  absolute path, or provider secret.
- The current-goal reports were scanned for credential-shaped values and raw
  response fields. The sole `OPENAI_API_KEY=` occurrence is an explanatory
  finding about a previous risk, not a value. No secret-shaped value or raw
  source-body field was found in the new report/evidence directories.

## Findings considered and refuted

| Candidate | Verdict | Reason |
| --- | --- | --- |
| Agent-safe receipt provenance is stale | Refuted | The persisted helper and tooling-manifest SHA-256 values exactly match the reviewed source files. |
| The checked-in agent-safe pilot receipts retain content or a caller path | Refuted | Each schema is closed and contains projected numeric/enum metrics plus logical `artifact_ref` only; direct inspection found no content/path fields. |
| The operator handoff changes the CRE collection/resource contract | Refuted | No CRE collector/runtime path changed; the related resource-profile edit removes advice to invoke the retired writer. |
| The pin manifest defaults to an unpinned package | Refuted | Normal wrapper defaults are `firecrawl-cli@1.20.0` and `firecrawl-mcp@3.24.0`; `@latest` is isolated to the acknowledged upgrade probe. |

## Post-fix proof required

1. Run the affected mocked tests plus the existing preflight, compatibility,
   agent-safe, operator-handoff, wrapper, and CRE-resource-profile suites.
2. Re-run `ruff`, `py_compile`, shell syntax checks, and `git diff --check` for
   changed sources.
3. Repeat a static scan of only the new task report/evidence directories for
   secret-shaped values, raw response fields, and absolute source paths.
4. Request an independent final verifier pass after the fixes, before staging.
