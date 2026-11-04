# Collector tests

Use [SETUP's isolated verification recipe](../SETUP.md#isolated-verification)
before running collector suites. They must run from a separate standalone
`git clone --no-hardlinks`, not a runtime checkout or shared worktree. See
[AGENTS.md](AGENTS.md) for test boundaries, fixture requirements, and the
disposable PostgreSQL contracts.

| Work | Start here |
| --- | --- |
| Python collector, ingest, gate, refresh, and runtime contracts | Root `test_*.py` files; search by module or behavior, then run the relevant tests |
| TypeScript helpers and source adapters | [ts/](ts/README.md); `npm test` runs typecheck and the unit suite |
| Source evidence and source fields | [source_evidence/](source_evidence/) and [source_fields/](source_fields/) |
| Document extraction | [document_text/](document_text/) and [document_text's own tests](../document_text/) |
| Observation-store fixtures and disposable database contracts | [observation_store/](observation_store/README.md) |
| Native source fixtures | [native/](native/); see `test:jll-native` in [package.json](../package.json) |
| Windows launcher and capture contracts | [windows/](../windows/); these are separate from the collector test directories |
| Shared synthetic inputs | [fixtures/](fixtures/) |

From the collector directory in the isolated clone, `../../../.venv/bin/python -m pytest tests/ -q`
and `npm test` run the main Python and TypeScript suites. Some disposable
PostgreSQL contracts are separate commands; consult [AGENTS.md](AGENTS.md) and
the relevant subsystem guide for their prerequisites and scope.
