# CRE Collector Security Review (2026-06-14)

Branch reviewed: `feat/cre-brokerage-collectors-2026-06-12` (tip `bb2241b5d` at
review time).
Scope: security implications of the changes this branch introduces vs `main`.
Reviewer: security pass (one identification agent plus two adversarial
verifiers), findings and fixes integrated by the main session.

This document is both the security report and the activity log for the review.
For the SQL/database advisor deep dives it references, see the dated reports
under `../sql/advisor-reports/`.

## 1. Objective and method

Goal: find high-confidence, newly introduced security vulnerabilities with real
exploitation potential, not a general code review. Threat model centered on the
one untrusted input this system actually ingests: remotely scraped broker
content (HTML, JSON-LD, sitemaps), which is attacker influenceable (a broker
site could be compromised or serve crafted content). The review traced whether
such scraped fields reach a sensitive sink (SQL execution, subprocess, file
path) without correct handling.

Process (3 independent passes):

1. Identification sweep across all executable code changed on the branch.
2. Adversarial verifier A: tasked to break the Python to `psql` SQL path
   (refute the "no SQL injection" claim).
3. Adversarial verifier B: tasked to break the Supabase authorization posture
   (refute the "RLS locked down" claim), with live verification against the
   database.

Confidence bar for reporting: greater than 80 percent confidence of actual
exploitability. Excluded by policy: denial of service, resource exhaustion,
secrets at rest, rate limiting, and pure hardening preferences.

## 2. What was reviewed

Executable code (the security relevant surface). The bulk of the branch diff is
markdown, archived JSON/HTML capture artifacts, and test-run output, which were
out of scope.

- Python (shells out to `psql`, builds SQL):
  `cre_ingest.py`, `cre_monitor.py`, `cre_gate.py`, `cre_validate.py`.
- TypeScript (web scrapers): `collect.ts`, `types.ts`, `lib/*.ts`
  (`config`, `scrape`, `util`, `broker`, `html`), `sources/*.ts` (15 sources).
- Shell: `cre_daily_update.sh`, `run_colliers_main_full.sh`,
  `launchd/cre_run_tier.sh`.
- SQL migrations: `../sql/001`..`../sql/008`, `000_run_all.sql`,
  `005_cre_views.sql`, and `../sql/advisor-reports/2026-06-13-cre-live-hardening.sql`.
- PowerShell: `apps/api/scripts/check-domains.ps1`.
- Config: `.claude/settings.json`, `.gitignore`, launchd plists.

## 3. Overall verdict

No exploitable vulnerability (HIGH, MEDIUM, or LOW) met the reporting bar. The
two highest risk surfaces (the Python SQL construction and the SQL
authorization posture) were examined line by line and are implemented
correctly. One latent dependency was found and has been fixed (Section 5). One
defense in depth item is deliberately deferred and was left as is (Section 6).

## 4. Category results

| Category | Result | Why |
|----------|--------|-----|
| SQL injection (Python to `psql`) | Not found | All COPY data flows through one `copy_field()` that escapes backslash first, then tab/newline/CR, so a scraped value cannot forge a COPY row, column, or `\.` terminator. All inline literals flow through `sql_lit()` (single quote doubling), safe under `standard_conforming_strings = on`. Numeric and timestamp values are coerced to native types or cast (`::uuid`, `::timestamptz`). The only identifier position interpolation (`slug`) is bound to a closed allowlist, not scraped. |
| Command injection | Not found | Every `subprocess.run` uses argv list form, no `shell=True`, no `os.system`/`os.popen`. The DB URL is always a single argv element, never concatenated into a shell string. Shell scripts consume only trusted env vars and CLI args plus a numeric only (`grep -oE '^[0-9]+'`) value. |
| Code execution / deserialization | Not found | TypeScript parses remote content with `JSON.parse` and cheerio (no `eval`, `Function`, `child_process`). Python uses `json.load` only (no `pickle`, no `yaml.load`). |
| Path traversal | Not found | Cache paths derived from remote influenced input are SHA hashed or stripped to `[a-z0-9]`. Output paths come from trusted CLI flags. |
| Authorization / RLS / data exposure | Locked down | All 11 CRE tables enable RLS with no permissive policy (deny all to non owner). All 5 views set `security_invoker = true` with explicit REVOKE from PUBLIC/anon/authenticated and GRANT only to `service_role`. No GRANT to anon/authenticated/PUBLIC on any CRE table, view, or function. Functions are SECURITY INVOKER with pinned `search_path = ''`, service role only EXECUTE. |
| Secrets / data exposure | Not found | No hardcoded keys, passwords, or tokens added. DB URL read from a gitignored env file at runtime and never printed (only the env file path is logged). Seed `scrape_config` JSON holds only public scrape hints. |

Live confirmation (verifier B), impersonating the internet reachable roles
against Supabase project `fhqycqubkkrdgzswccwd`:

- `SET ROLE anon; SELECT count(*) FROM credeals.cre_listings` returns
  `permission denied for table cre_listings`.
- `SET ROLE authenticated; SELECT count(*) FROM credeals.v_cre_listings_full`
  returns `permission denied for view`.

Access fails at the privilege layer before RLS is consulted, so the data is
protected by two independent layers (no API role grant and RLS deny).

## 5. Concern A (fixed): latent SQL-literal escaping dependency

Finding. `sql_lit()` escapes a single quote by doubling it. That is provably
sufficient only when PostgreSQL runs with `standard_conforming_strings = on`
(the default since PostgreSQL 9.1, and non negotiable on Supabase). Under that
setting a backslash inside a `'...'` literal is literal, so a doubled single
quote is the only escape a scraped value can need. The dependency was implicit:
nothing in the generated SQL enforced it. If a future change disabled the GUC,
or introduced an `E'...'` escape string literal around an interpolated value,
the scraped text INSERT path (`cre_monitor.py` change events, plus
artifact derived values in `cre_ingest.py` and `cre_gate.py`) would become
injectable. Classified as a latent dependency, not a present vulnerability.

Fix. Convert the implicit invariant into an explicit, self enforced one. Every
generated SQL transaction now pins the GUC itself, immediately after `BEGIN;`
and before any literal or COPY bearing statement, in all three `psql -f` write
paths:

- `cre_ingest.py` `build_sql()` (bulk upsert)
- `cre_monitor.py` `build_write_sql()` (change event INSERTs, the actual
  scraped free text to literal sink)
- `cre_gate.py` `build_baseline_sql()` (coverage baseline upsert)

```sql
BEGIN;
SET LOCAL statement_timeout = '600s';
SET LOCAL standard_conforming_strings = on;   -- added, with an explanatory comment
```

Properties: idiomatic (`pg_dump` emits the same statement), a behavioral no op
on a correctly configured server, and zero new attack surface (a static string,
no interpolation). The COPY escaping in `copy_field()` is independent of this
GUC, so the change only reinforces the `'...'` literal path. Each insertion
carries an inline comment that records the invariant and warns against wrapping
an interpolated value in `E'...'`.

Verification.

- Test driven: 3 ordering tests written first (confirmed failing), then the
  implementation, then confirmed passing. Tests assert the statement appears and
  is ordered after `BEGIN;` and before the first literal or COPY statement:
  - `tests/test_ingest_status_activation.py::test_build_sql_pins_standard_conforming_strings`
  - `tests/test_monitor_events.py::test_build_write_sql_pins_standard_conforming_strings`
  - `tests/test_gate.py::TestMonitorSQLSafetyAssertion::test_sql_pins_standard_conforming_strings`
- Full Python suite: 264 passed (261 baseline plus 3 new).
- `py_compile` clean on all three source files.
- The existing observe only SQL safety greps are unaffected: the added line
  contains no `status` or `deleted_at` substring and no `INSERT INTO`.

Files changed: `cre_ingest.py` (+7), `cre_monitor.py` (+5), `cre_gate.py` (+9),
plus the 3 tests.

## 6. Concern B (deferred by design, left as is): base-table REVOKE

Observation. The table lock down currently rests on the absence of any
anon/authenticated grant rather than an explicit deny policy on the base tables.

Disposition. This is a documented, deliberate decision, not an oversight. The
advisor report `../sql/advisor-reports/2026-06-13-cre-rls-enabled-no-policy.md`
analyzes it and records the recommendation as ACCEPT: effective access is
already blocked two ways (no API role grant, and the 5 views are
`security_invoker` with explicit REVOKE at `005_cre_views.sql:337-362`). The
`USING (false)` deny policies for anon/authenticated are kept as a hardened,
ready to apply optional Appendix B in that report, with a standing instruction
not to create a `008` policy migration. No migration was altered and no live
DDL was applied here.

Optional follow up (your call, gated live DDL). If belt and suspenders is
wanted later, apply Appendix B from that report. It makes the schema self
defending against a future broad `GRANT SELECT ON ALL TABLES IN SCHEMA credeals
TO anon`. It is a documentation and advisor count change, not an access change,
and targets only anon and authenticated (never `service_role`).

## 7. Standing guidance for future changes

- Keep all scraped or artifact derived data on the existing encoders:
  `copy_field()` for COPY data, `sql_lit()` (and `_sql_text` / `_sql_uuid`) for
  inline literals. Do not concatenate scraped values into a SQL string by any
  other path.
- Never wrap an interpolated value in an `E'...'` escape string literal. The
  `SET LOCAL standard_conforming_strings = on` guard protects regular `'...'`
  literals only; `E'...'` always reprocesses backslashes.
- Keep `subprocess.run` in argv list form, never `shell=True`, and never place
  the DB URL anywhere but a single argv element.
- Database objects under `credeals` are service role only. Read
  `archive/SUPABASE_SECURITY_NOTE_2026-06-12.md` and the advisor reports before
  changing any grant, view privilege, or RLS setting.

## 8. References

- Reviewed code: `cre_ingest.py`, `cre_monitor.py`, `cre_gate.py`,
  `cre_validate.py`, `collect.ts`, `lib/`, `sources/`, `../sql/`.
- SQL authorization deep dives: `../sql/advisor-reports/2026-06-13-cre-rls-enabled-no-policy.md`,
  `../sql/advisor-reports/2026-06-13-cre-functions-and-grants.md`,
  `../sql/advisor-reports/2026-06-13-cre-live-hardening.sql`.
- Test contracts: `tests/CLAUDE.md`, `tests/test_ingest_status_activation.py`,
  `tests/test_monitor_events.py`, `tests/test_gate.py`, `tests/test_monitor.py`.
