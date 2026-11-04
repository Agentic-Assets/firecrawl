# Synthetic Docling PostgreSQL fixture

Part of AGENTIC-2895. Run only after the separately reviewed Docling JSON
format and export integration is present, in a standalone `git clone
--no-hardlinks`. This fixture accepts no database address or external input;
its contents and parser label are synthetic and do not claim an actual PDF or
converter run. It makes no source, accuracy, production, or deployment claim.

```sh
python3 scripts/firecrawl-ops/cre_collector/tests/source_evidence/run_docling_json_postgres.py --execute-disposable-fixture
```

The explicit flag starts an owned PostgreSQL 17 cluster on a private Unix
socket using the existing fixture helper. It applies migration 019 only to
that newly created disposable database, using its required confirmation
syntax as fixture values, not as a production approval. The helper confirms
server stop before deleting its owned temporary directory; it retains the
directory if startup or stop settlement is uncertain. A delayed orphan after a
startup timeout cannot be ruled out.

The fixture checks an 80-cell, multi-chunk UTF-8 synthetic JSON document for
least-privilege append and exact replay, ordered readback, exact JSON export,
bounded reader full-text search, divergent replay and tampered readback rejection,
unauthorized access, and an unchanged unrelated sentinel. Search is chunk-local
text retrieval, not a structural JSON predicate or cross-chunk match guarantee. Its supported bounds and source-policy checks
come from the candidate code under test. A successful run does not authorize
production DDL, source admission, or document publication.
