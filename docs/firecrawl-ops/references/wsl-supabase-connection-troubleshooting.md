Related plan: [AGENTIC-3745](https://linear.app/agenticassets/issue/AGENTIC-3745).

# WSL Supabase connection troubleshooting

Use this guide when Firecrawl works locally but the CRE collector cannot reach
its Supabase PostgreSQL database. The repair below was verified on 2026-10-12.
Keep each computer's exact paths, installed versions and credential locations
in Context Engineering's ignored `local/machine.md`; never put secrets here.

## What failed and why

There were two separate connection problems:

1. **No route to the direct database endpoint.** The workstation's active
   Ethernet connection had IPv4 Internet access, but neither Windows nor WSL
   had a usable global IPv6 address/default route. The default Supabase direct
   endpoint required IPv6, so the collector failed with network unreachable.
   This establishes a routing limitation on that connection; it does not
   identify Windows, Wi-Fi, the router, ISP or VPN as the underlying cause.
2. **An untrusted certificate chain after changing transport.** The approved
   IPv4 session pooler was reachable, but Ubuntu's default public CA store did
   not establish trust for its Supabase certificate. Supplying the official
   Supabase CA with `sslmode=verify-full` fixed chain and hostname verification.
   Disabling certificate verification was unnecessary.

The Supabase CLI could already execute Management API queries over HTTPS.
That success did not test the collector's PostgreSQL connection, its database
credentials or its TLS trust configuration. The Codex Supabase connector also
had an independent reauthentication failure; it was not the database outage.

## Diagnose the failing layer

| Observation | What it establishes | Next check |
| --- | --- | --- |
| Local API readiness succeeds | Firecrawl HTTP service responds | Worker/browser smoke is separate from CRE database transport |
| Supabase CLI/MCP query succeeds | That authenticated API path works | Test the collector's actual psql path |
| Direct host has an IPv6 address, but connection reports network unreachable | No usable route to that address from the tested environment | Check Windows and WSL routing; consider the project's session pooler |
| TCP connects, but verify-full rejects the certificate | PostgreSQL is reachable, but TLS trust/name verification failed | Official project CA, Linux-readable `sslrootcert`, correct hostname and clock |
| Authenticated read-only psql canary succeeds | The selected database transport and query path work | Disposable ingestion contracts, then separately governed live refresh |

Inspect network capability from PowerShell without changing adapter policy:

```powershell
Get-NetConnectionProfile | Select-Object InterfaceAlias,IPv4Connectivity,IPv6Connectivity
Get-NetRoute -AddressFamily IPv6 -DestinationPrefix '::/0' -ErrorAction SilentlyContinue
wsl -d Ubuntu-24.04 -- ip -6 route show default
```

Check DNS and reachability from the environment that runs the collector, not
only from Windows. A missing IPv6 route alone is not a universal diagnosis;
combine it with the selected endpoint's address family and actual failure.
Do not change DNS, firewall, VPN or institution-managed network settings as a
substitute for identifying the failed layer.

## Use the actual session-pooler connection

For this persistent psql workload on an IPv4-only network, obtain the project's
**Session pooler** URL from Supabase's Connect panel, normally port `5432`,
with its `role.PROJECT_REF` username. Do not derive the pooler host from region
or substitute a transaction-pooler URL on `6543`. A paid direct IPv4 add-on is
another vendor option; it was not needed for this repair.

Supabase documents the connection choices in its
[Postgres connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres).
Preserve the Mac's working connection. Provision only the approved connection
for this host into a private Linux file such as `~/.config/cre/equire.env`;
use directory mode `0700` and file mode `0600`. Set `CRE_ENV_FILE` to that file
and source `~/.agentic-assets/env` before CRE commands when that context exists.
The loader prefers `POSTGRES_URL_NON_POOLING` over `POSTGRES_URL` inside the
selected file despite the older key's name; remove stale conflicting routing
rather than updating only the lower-priority key.

Configure the connection with:

- `sslmode=verify-full` to verify both chain and hostname;
- `sslrootcert` pointing to the official Supabase CA in the Linux filesystem;
- a bounded `connect_timeout`, for example `10` seconds.

Download the CA from the project's Database settings as described in
[SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement).
During this repair, the production CA URL published in the official
[Supabase Studio configuration](https://github.com/supabase/supabase/blob/master/apps/studio/hooks/custom-content/custom-content.json)
provided the same trusted certificate. Obtain the current CA rather than
assuming a dated URL/certificate remains valid forever. A Windows path inside
`sslrootcert` is not automatically a usable Linux path.

Keep passwords out of command arguments and logs. Use the repository's
`cre_ingest.psql_connection_env` and `psql_connection_args` helpers for bounded
diagnostics, rather than invoking `psql` with a credential-bearing URL.

## Distinguish the two TLS connections

With a pooler, client-to-pooler and pooler-to-Postgres are separate connections.
[PostgreSQL's `pg_stat_ssl`](https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-SSL-VIEW)
reports the connection observed by the database backend; it does not report the
external client's separate pooler connection. A saved `server_connection_ssl=false`
must remain visible, but alone does not prove client TLS is disabled.
For the approved collector snapshot path, require client `PGSSLMODE=verify-full`,
the trusted CA and hostname verification, and successful SSL `\conninfo` evidence.
Do not replace those checks with a backend `ssl=true` prerequisite or weaken them
because the backend value is false. Client proof alone does not establish encryption
on the pooler-to-Postgres leg. This distinction follows the [Supabase connection
model](https://supabase.com/docs/guides/database/connecting-to-postgres) and prior
[AGENTIC-2895 learning](https://linear.app/agenticassets/issue/AGENTIC-2895#comment-c4248f93-1909-4e27-a10a-1bb0fda547a8).

## Preserve checkpoint target identity

Shared Supabase pooler host/port/database values do not uniquely identify a
project. [PR112](https://github.com/Agentic-Assets/firecrawl/pull/112) binds the
checkpoint fingerprint to the explicit project tenant as well. Role/password
rotation within that project keeps its identity; malformed/conflicting user
routing fails closed. Nonempty URI `options` and alternate `.cluster.` users
are refused on the governed checkpoint path because they can change routing.
Use transaction-local `SET LOCAL` for diagnostic timeouts instead.

Start a **fresh checkpoint generation** after changing between direct and
pooled endpoints or upgrading an old pooled fingerprint. Do not edit saved
manifests to bypass target or platform binding. Ordinary direct endpoint
fingerprints and the Mac connection configuration remain unchanged.

## Verify in stages

1. Verify the intended project and privately inspect the selected connection
   file. CLI/MCP identity and PostgreSQL identity are separate checks.
2. Test the actual collector psql helpers with an outer process deadline, a
   bounded connect timeout, and a transaction containing:

   ```sql
   BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
   SET LOCAL statement_timeout = '15s';
   SET LOCAL lock_timeout = '3s';
   SELECT current_database(), current_setting('transaction_read_only'),
          to_regclass('credeals.cre_listings') IS NOT NULL AS listings_present,
          to_regclass('credeals.cre_scrape_jobs') IS NOT NULL AS jobs_present;
   COPY (SELECT 1 AS canary, 'copy-wire-readback'::text AS marker)
     TO STDOUT WITH (FORMAT csv);
   ROLLBACK;
   ```

   Require the expected database, read-only mode, both tables and COPY output.
   Schema presence alone does not identify the project: verify the qualified
   pooler tenant against the approved Connect-panel reference before access.
3. Run the existing `cre_validate.py` aggregate checks with bounded execution.
   Its canonical batch uses one psql session and a repeatable-read read-only
   transaction, with framed results and per-query timings. Separate ad hoc CLI
   API queries do not share that snapshot. Keep partial/time-limited results
   explicitly incomplete rather than treating query success as data freshness.
4. Prove temporary staging, `COPY FROM STDIN` and transaction cleanup against
   an owned disposable local PostgreSQL instance using synthetic data. COPY OUT
   on the live project does not prove COPY IN or production write privileges.
5. Before live ingestion, follow [START_HERE](../../../scripts/firecrawl-ops/cre_collector/START_HERE.md):
   verify no overlapping Mac writer/schedule, use a fresh supervised checkpoint,
   and obtain generation-exact database readback before a full-registry claim.

The repair passed verified TLS, bounded read-only psql/COPY OUT and the existing
aggregate validator in one consistent diagnostic transaction. Separate offline
PostgreSQL tests passed staging/COPY IN/cleanup. Existing data-quality and
collation-version warnings were reported, not repaired through transport setup.
These results establish connectivity and test coverage; they do not establish
a completed 51-source refresh, native Mac execution or an installed scheduler.
Current execution evidence and remaining work belong on
[AGENTIC-2902](https://linear.app/agenticassets/issue/AGENTIC-2902), not in a
permanent guide's live-status paragraph.

## Lessons for the next machine

- Test DNS/routing, TCP, TLS, authentication and query semantics separately.
- Keep the Firecrawl Docker queue database distinct from the remote CRE database.
- Prefer host-local connection settings over global network or project changes.
- A ready API, a successful CLI query, passing unit tests and a complete live
  refresh are different evidence. Name exactly which one passed.
- WSL2 is the supported Windows CRE environment for POSIX locks/process groups;
  keep Linux code and dependencies on ext4. Resource ceilings and Linux safety
  guards complement each other; VM measurements do not cover all Windows apps.
