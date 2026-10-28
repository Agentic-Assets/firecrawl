Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

This is an **unapplied production-candidate design**, enabled only for an
explicitly named disposable fixture database and session marker. These checks
are tripwires, not security isolation or approval. The SQL is not a live writer,
not integrated with a collector, and cannot be applied unchanged to production.
Cross-repo object ownership and security/visibility policy must be reconciled,
with owner acknowledgement and separate production DDL approval, before an
independently reviewed production migration or writer can be enabled. The
writer and narrowly privileged non-login owner roles must be provisioned
separately and their memberships reviewed. The fixture function uses a fixed
search path and runs as that owner, rather than the migration executor.

Each immutable capture/page holds optional, caller-supplied assertions and
unverified provenance, a bounded explicitly allowed artifact with a checked SHA-256,
an asserted original hash and length, and ordered JSONB observation facts. The database requires the submitted facts
to equal the `records` array parsed from the hashed artifact. This binds facts
to that artifact but does not prove sanitization, confidentiality or source
authorization. The
database cannot verify the original hash without original bytes; verify it locally
before a dry run and keep the original privately outside the database. Source, generation, page, transaction and
property identity need not be known. This captures competing facts across
independent captures, and ordered arrays preserve repeated labels within an
observation. Exact retries are idempotent; differing payloads at the same
capture/page fail. Original private input, not JSONB or the allowed artifact, is authoritative for
duplicate JSON object keys, lexical numbers, quarantined facts and other details
that JSONB cannot preserve. Record omissions explicitly; this design cannot
persist those facts and cannot silently treat that as complete retention. A capture/page is a caller-issued receipt identity, not a
verified source page or property identity.

Allowed artifact bytes and observations may still contain confidential or credential-bearing
content. Database permissions, private storage, retention/deletion, encryption,
quotas, backup access, source-specific admission and safe logging require
review before any writer activation. Heuristic filtering is not proof that
source data is safe. No authorization is inferred from a source assertion or a
hash. This Buildout-oriented fixture does not enforce source identity or JLL withholding.
No real data may be passed to it: separate visibility and admission policies must
be designed and reviewed before production activation. Database owners and
administrators can access the table; role grants alone do not guarantee privacy.
No canonical listings, source index, status, freshness, lifecycle, or deletion
are written. No source availability, coverage or authenticity is claimed.

From a standalone `git clone --no-hardlinks`, run:

```sh
python3 tests/observation_store/run_buildout_observation_store_postgres.py --execute-disposable-fixture
```

The runner creates and removes a temporary local PostgreSQL cluster with no TCP
listener, accepts no database URL, and uses synthetic facts only. Rollback of a
future applied migration needs an approved policy for preserving stored evidence;
dropping the schema destroys evidence and is not an acceptable automatic rollback.
