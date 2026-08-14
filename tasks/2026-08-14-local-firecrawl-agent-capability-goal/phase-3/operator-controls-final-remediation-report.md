# AGENTIC-2280 final operator-controls remediation report

Date: 2026-08-14

## Closed lifecycle boundary

- Added `ocr-lifecycle` to `firecrawl_operator_handoff.py`. Its only actions
  are `ensure`, `restart`, and `stop`; the action is dry-run by default and an
  apply requires the existing double-idle plus pre-mutation idle snapshots,
  an allowlisted operator identity, approval reference, provider-cost
  acknowledgement, exact apply confirmation, and a mandatory retained-state
  acknowledgement.
- Lifecycle actions do not accept caller-controlled profiles, capture/output
  settings, images, containers, ports, or environment overrides. Docling is
  fixed to the pinned digest, exact container name, and
  `127.0.0.1:5001:5001`; adapter restart is fixed to the no-capture contract.
  Docling readiness plus image/binding inspection is required after ensure;
  restart additionally requires a safe adapter settings readback before a
  retained receipt.
- `start-docling`, `start`, `restart`, `stop-adapter`, `stop-docling`, and
  `stop` in `local_firepdf_ocr.sh` are now delegation-only migration shims.
  They cannot reach the script's direct Docker functions, state directory, or
  output-directory path. `start-adapter` and `restart-adapter` likewise route
  only to the fixed `ocr-adapter` handoff and reject legacy profile/capture
  arguments through the handoff parser. Read-only diagnostics remain intact.
- The handoff permits only `cayman` or `stace` operator identities and rejects
  credential-like strings before they can enter a receipt. Lifecycle receipts
  have no environment key/value maps, retain no bodies, and validate before
  mode-0600 persistence. A lifecycle failure after Docker work starts writes a
  body-free failed/manual-handoff receipt rather than claiming a restore.
- Adapter bootstrap now checks for the exact container before removal. An
  absent adapter therefore follows the fixed build/run path instead of failing
  on an unconditional `docker rm -f`.

## Focused proof

```text
python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py \
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py
# 28 passed, 39 subtests passed

python3 -m pytest -q <11 focused operator/wrapper/helper/CRE test modules> \
  -k 'not preflight_bypasses_ambient_proxy_and_connects_directly'
# 140 passed, 1 skipped, 1 deselected, 177 subtests passed

python3 -m py_compile <changed operator-boundary Python files and tests>
bash -n scripts/firecrawl-ops/local_firepdf_ocr.sh \
  scripts/firecrawl-ops/set_model_profile.sh \
  scripts/firecrawl-ops/set_cre_resource_profile.sh
uvx ruff check <three changed operator-control Python files>
uvx ruff format --check <same scope>
git diff --check
```

The one deselected compatibility-doctor test starts a temporary loopback HTTP
server and cannot bind inside the workspace sandbox (`PermissionError`). The
otherwise identical full focused command reached `140 passed, 1 skipped`
before that sandbox-only test failed; it does not indicate a Firecrawl API or
operator-control regression.

## Deliberately not exercised

No operator apply, Firecrawl API call, Docker/OrbStack command, root `.env`
read/write, CRE pipeline/process, Linear update, commit, or push was run.
Temporary test fixtures and mocked runners supplied all mutation/readback
coverage.
