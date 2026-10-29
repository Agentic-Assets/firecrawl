READ ~/AGENTS.md FIRST (skip if missing).

# Source fields

This module owns source-qualified field promotion. Preserve source evidence and
existing listing values; never infer field ownership or freshness from generic
extra facts. Database writes remain within the ingest transaction.

Read `README.md` for provider scope and the distinction between a missing field
and an explicit unresolved observation. Do not erase per-field holds while
merging passes, infer floor-size units, restamp source clocks during replay,
or treat producer hashes as independent website authentication.
