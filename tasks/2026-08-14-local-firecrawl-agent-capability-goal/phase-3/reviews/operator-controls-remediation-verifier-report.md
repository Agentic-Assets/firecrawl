# AGENTIC-2280 operator-controls remediation verifier report

Date: 2026-08-14
Reviewer: independent adversarial verifier
Worktree: `feat/local-firecrawl-agent-capabilities`
Scope: static and mocked verification only. No apply, local API, Docker/OrbStack,
`.env`, CRE, Linear, commit, or push action was performed.

## Verdict

The remediation fixes all six earlier findings inside the new model/OCR handoff
path, but AGENTIC-2280 is not ready to call complete. The public OCR script
retains direct `restart` and `start-docling` lifecycle routes. `restart` is an
unguarded profile/raw-capture bypass; `start-docling` can replace the Docling
backend used by an active adapter with caller-controlled runtime parameters.
Both are materially in scope for an operator-only OCR control boundary.

## Earlier findings: verification results

| Earlier finding | Verdict | Evidence |
| --- | --- | --- |
| Direct model-writer environment-marker bypass | Fixed | `set_model_profile.sh` is an unconditional refusal with no env/path/editor/Docker code. Its regression invokes every legacy profile with `FIRECRAWL_OPERATOR_HANDOFF=1` and proves fixture bytes stay identical. |
| Direct adapter start/restart/debug-capture bypass | Partially fixed | `start`, `start-adapter`, and `restart-adapter` now exec the handoff before Docker/state/output work; the mocked regression confirms no Docker or artifact directory is touched. `restart` remains direct. |
| Arbitrary restore receipt/key/path can overwrite or disclose secrets | Fixed | Production CLI no longer exposes alternate repo/env/receipt paths. Receipt schema requires exact allowlisted model or OCR key sets, retained source transitions, target map equality, and a matching final env digest. Malicious secret and CRE keys are rejected before writes. |
| Post-mutation failure without durable state | Fixed | The transition catches failures after a mutation begins, restores only the exact transition image, writes a mode-0600 `failed` receipt with the observed final digest, and returns a redacted error. Tests cover compose, healthcheck, and final-idle failures. |
| Missing OCR post-restart readback | Fixed | `ocr-adapter` reads safe settings after the restart and converts unsafe post-readback into a redacted failure receipt. |
| Automatic-restore digest mismatch | Fixed | Receipts now keep distinct before/transition/final digests; non-retained apply verifies the restored bytes and post-restore idle snapshot before a `restored` receipt. |
| CRE missing-env advice points to retired writer | Fixed | The CRE resource-profile message now names the upstream env template and a focused regression confirms it no longer advises `set_model_profile.sh`. |

## Confirmed remaining findings

### P1 — public `restart` remains a direct OCR profile and raw-capture bypass

`local_firepdf_ocr.sh restart` still calls `parse_adapter_flags`,
`stop_adapter`, `stop_docling`, `start_docling`, and `start_adapter` directly
(lines 588-594). The flags accept `--profile`, `--capture-json`, and
`--output-dir`; `start_adapter` then creates the caller-controlled output
directory and runs Docker with raw capture enabled (lines 139-175 and
294-365). It skips the handoff's idle checks, explicit attestation, fixed
no-capture contract, post-restart readback, and receipt.

This is a must-fix bypass, not merely a stale convenience alias. An agent can
run `restart --profile qa-debug --capture-json --output-dir …` to change the
active OCR profile and retain raw Docling data directly.

### P1 — public `start-docling` can mutate the backend serving active OCR

`start-docling` directly calls `start_docling` (lines 568-572), which removes
a stopped same-name container or starts `docker run` from caller-controlled
`LOCAL_FIREPDF_DOCLING_IMAGE`, container-name, port, and max-wait environment
values (lines 10-16 and 258-274). It does not itself toggle root `.env` or a
raw-capture profile, but the adapter is hardwired to that host Docling service.
Consequently it can replace the runtime backend of an active OCR route without
the operator handoff. The public `stop-adapter`, `stop-docling`, and `stop`
branches have the same unaccounted destructive lifecycle surface.

For AGENTIC-2280's stated operator-only model/OCR transition boundary, this
is also must-fix. If the narrower issue definition intentionally excludes
Docling container lifecycle, it must be documented explicitly and the direct
commands must carry a `Needs Cayman` operational exception; the present docs
instead describe the script as an agent-usable OCR surface.

### P2 — the receipt accepts a credential-like `--operator` string

`validate_apply_attestation()` calls `checked_reference()` for `--operator`
(line 650), while `--approval-ref` and `--handoff-ref` use
`ensure_no_secret_reference()`. A string such as `sk-secret` matches the
reference pattern and is stored verbatim in the body-free receipt. This does
not occur for ordinary `cayman`/`stace` use, but it contradicts the no-secret
contract for all accepted inputs.

Use `ensure_no_secret_reference()` for `--operator`, or better allow only the
configured operator identities. Add a sentinel test that asserts it cannot
enter an apply or failure receipt.

### P2 — the guarded adapter start does not bootstrap a missing adapter

`restart_safe_ocr_adapter()` begins with `docker rm -f
firecrawl-local-firepdf-adapter` (lines 743-793). Docker returns nonzero when
the container is absent, so the new `start`, `start-adapter`, and
`restart-adapter` aliases cannot start a clean machine even after Docling has
been brought up. They instead produce a failed handoff receipt. This is safe,
but breaks the documented bootstrap behavior and explains why a direct
`start-docling` workaround is tempting.

Let the handoff probe/remove the named adapter only when it exists, then
build/start it using the already fixed no-capture settings; keep the
preflight, attestation, and post-readback unchanged.

## Smallest safe backward-compatible lifecycle path

Do not preserve direct mutable execution just to keep legacy command names.
Keep those names as aliases, but make them delegate to one additional
operator-handoff operation, for example `ocr-lifecycle` with a narrow action
set:

1. `ensure-docling`: require the existing double idle snapshot and explicit
   apply attestation; start only the pinned Docling container at the fixed
   loopback port with no caller-provided image/config overrides; emit a
   body-free retained receipt.
2. `restart-stack`: require the same controls; stop/recreate Docling and the
   fixed no-capture adapter, then perform adapter settings readback. Preserve
   the existing `restart` command only as a dry-run delegation until an
   operator explicitly runs the canonical `--apply` command.
3. `stop`: either add a separately attested lifecycle action or reject direct
   stop commands before Docker. Stopping a shared backend should not be an
   invisible agent action.

The existing `ocr-adapter` operation should first be made bootstrap-capable
when no adapter container exists. That is the smallest shared primitive; it
avoids reintroducing the old shell's profile/capture/output-directory flags.
Route `start-docling`, `start`, `restart`, `stop-adapter`, `stop-docling`, and
`stop` through the new action or fail them closed. Reuse the legacy script only
for read-only diagnostics (`health`, `status`, `profiles`, `profile-env`, and
settings display) after clarifying their output boundaries.

## Verification performed

```text
python3 -m pytest -q <10 focused wrapper, helper, handoff, agent-safe, swarm, and CRE-resource test modules>
# 103 passed, 1 skipped, 138 subtests passed

python3 -m py_compile <scoped operator/helper files and tests>
bash -n <scoped CLI/MCP/model/OCR/CRE shell files>
git diff --check
```

The tests validate the remediated handoff and aliases. They do not exercise
the still-direct `restart`, `start-docling`, or stop branches, which is why the
remaining bypass persists despite a green suite.
