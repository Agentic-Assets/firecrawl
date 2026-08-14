# AGENTIC-2280 operator-controls implementation report

Date: 2026-08-14

## Delivered in the consolidated worktree

- Added `scripts/firecrawl-ops/firecrawl_operator_handoff.py`, the sole new
  operator transition surface. It is dry-run by default and writes a mode-0600,
  body-free receipt under `tasks/tmp/firecrawl-operator-handoff/`.
- The handoff permits only loopback GET preflight checks, disables ambient proxy
  routing and redirects, requires two idle queue/crawl snapshots, and rechecks
  immediately before an apply. Queue arithmetic, active crawls, malformed
  state, adapter activity, and raw-Docling-capture profiles fail closed.
- `model` plans or changes only `OPENAI_BASE_URL`, `MODEL_NAME`, and
  `MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK`. Gateway uses Flash 0731 with the
  Pro 0813 structured-output fallback; Gateway Pro uses Pro 0813 without one.
- `ocr-routing` owns only the listed non-secret routing keys and rejects an
  existing nonempty external `FIRE_PDF_API_KEY`. `ocr-adapter` allows a named
  no-raw-capture adapter profile only. Restore verifies the recorded
  post-transition full-env digest before a write.
- Apply requires operator, approval reference, provider-cost acknowledgement,
  exact confirmation, and a separate retain confirmation/reference. It never
  uses `docker compose down`, retries, or an automatic canary.
- Narrowed `set_model_profile.sh` to an internal allowlisted renderer invoked
  only by the handoff marker; it no longer bootstraps `.env` or writes OCR,
  provider-key, or unrelated settings.
- `local_firepdf_ocr.sh enable-firecrawl` now refuses direct mutation rather
  than silently selecting the old budget profile when `.env` is absent.
- The legacy swarm pipeline now rejects `--restart-between-stages` before it
  reads input or calls the API; all model/Docker switching code was removed.

## Ordinary agent-surface completion

After AGENTIC-2279 completed its stable review, the ordinary direct HTTP
helper removed its model-profile, Firecrawl-directory, Docker-recreate, and
healthcheck parser controls and its profile-application implementation. The
CLI wrapper now rejects its former mutation options before compatibility
resolution or `npx`; the MCP wrapper has no such arguments or mutation
surface. The agent-tooling reference now points model/OCR/Docker transitions
to the operator handoff plan first.

## Verification

Passed without contacting the host API or running an apply/Docker path:

```text
python3 -m pytest -q \
  scripts/firecrawl-ops/tests/test_firecrawl_operator_handoff.py \
  scripts/firecrawl-ops/tests/test_operator_mutation_boundaries.py \
  scripts/firecrawl-ops/tests/test_set_model_profile.py \
  scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py
# 21 passed, 14 subtests passed

python3 -m py_compile <five scoped Python files>
bash -n scripts/firecrawl-ops/set_model_profile.sh scripts/firecrawl-ops/local_firepdf_ocr.sh
uvx ruff check <five scoped Python files>
uvx ruff format --check <five scoped Python files>
git diff --check
```

No `.env`, Docker/OrbStack state, CRE code, API code, Supabase/Postgres,
launchd, Linear state, or remote branch was changed.
