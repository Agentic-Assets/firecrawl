Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

This is an offline **synthetic-only** adapter between the lossless cache evidence
and the disposable restricted-store fixture. It has no production client,
network or database connection. The caller-controlled synthetic marker is a
tripwire, not authorization or proof the input is synthetic. Do not use real
source data. An external disposable PostgreSQL runner alone connects to its
own temporary local database.

The planner rebuilds evidence from the exact synthetic original and requires a
byte-for-byte match, then sends screened typed record trees and a capture-level screened tree in a
new artifact. The latter preserves unmapped context and ambiguous candidates. Its `records` array exactly matches submitted fixture observations.
The original is not transferred; its hash and length are unverified claims to
the database. Record identity and source, generation, and page claims remain
unresolved; the receipt slot is not a source page. Ordered repeated keys,
attributes and numeric token spellings remain in the typed trees. Quarantine
counts cover the input evidence, including wrapper fields; the private
original is the only recovery authority for excluded material. Ambiguous
inventory is retained as one unresolved capture-level observation without
selecting a row or claiming unavailability.

Screening is heuristic and incomplete. A matching digest and synthetic marker
cannot prove sensitive data is absent, authorize a provider or enforce JLL
withholding. The database does not verify those policies or that bytes are safe.
This fixture does not establish production privacy or durable storage readiness;
owner approval, source admission, restrictive access and separate production
DDL approval remain open. A conflicting retry for one capture/slot fails; it
is not erased or promoted. Distinct captures require an independently reviewed
identity and retention design before production use. When there are at least
1,000 row paths, the adapter preserves only the capture-level tree and records
the count of omitted individual row views. Artifact-size limits fail closed;
the original stays private for recovery.

In a standalone `git clone --no-hardlinks`, run:

```sh
python3 -m pytest tests/test_buildout_lossless_cache_evidence.py tests/observation_store/test_buildout_lossless_store_fixture.py -q
python3 tests/observation_store/run_buildout_lossless_store_fixture_postgres.py --execute-disposable-fixture
```

The latter starts a disposable private socket-only local PostgreSQL server and
accepts no database URL. It uses synthetic data only, bounds subprocess waits,
and provides a minimal environment. After successful startup it removes the
cluster only after confirming the server stopped. If startup fails or shutdown
cannot be confirmed, it retains the private cluster directory for inspection;
a human must verify no server remains before removing it.
