# Foreground CRE runtime ownership

Part of AGENTIC-2895; implements AGENTIC-4023.

`cre_owned_runtime.py` is an explicit opt-in wrapper around the existing
`cre_checkpoint_series.py`. Ordinary series commands retain their current
behavior. The wrapper never starts or restarts a stack, changes its settings,
purges queues, or runs a scheduler.

Use it only for a dedicated local project named `firecrawl-cre-*` that the
operator owns. Pin explicit full container IDs for every service intended to
stop. The helper checks Compose project/service labels, image IDs, restart
policies, Docker context endpoint, daemon ID and server version. Only local
Unix-socket Docker contexts are supported. It refuses replacement identities
and does not discover a replacement container by name.

From a clean merged checkout, with the selected stack already running:

```sh
cd scripts/firecrawl-ops/cre_collector
python3 cre_owned_runtime.py pin \
  --context "$OWNED_DOCKER_CONTEXT" --project "$OWNED_COMPOSE_PROJECT" \
  --container "api=$API_CONTAINER_ID" \
  --container "playwright-service=$BROWSER_CONTAINER_ID" \
  --container "redis=$REDIS_CONTAINER_ID" \
  --container "nuq-postgres=$NUQ_CONTAINER_ID" \
  --container "rabbitmq=$RABBITMQ_CONTAINER_ID" \
  --out /private/path/owned-runtime.json
python3 cre_owned_runtime.py run \
  --identity /private/path/owned-runtime.json \
  --out /private/path/new-runtime-run \
  --api-url http://127.0.0.1:3302 --browser-url http://127.0.0.1:3303 \
  -- --sources all --page-cap 400 --concurrency 2
```

The variables above are explicit operator-selected full IDs, not names or
prefixes. Endpoints must use literal loopback addresses and match the selected
API's `3002/tcp` and browser's `3000/tcp` published ports. Before launch, the
wrapper requires unchanged running container epochs, an empty API queue and a
healthy browser with zero occupied pages. Each health request has an independent
two-second process deadline; it rejects redirects and proxy routing. It sets
both collector API environment variables to the admitted endpoint and pins
the Docker context/project for diagnostics. Existing freshness, SHA, database
target, CPU/memory and additive-ingest gates still belong to the series.

Private identity and exit evidence is created once, capped at 64 KiB per file,
with owner-only access. A foreground advisory lock for each selected
daemon/container prevents overlapping cooperative wrapper owners across clones
or copied identity files. It does not claim authority over another operator's
unwrapped process.

Every collector exit, failed admission or operator SIGINT/SIGTERM triggers
exact-ID runtime shutdown. Cancellation starts runtime containment without
waiting for an unresponsive collector. The default runtime shutdown budget is
30 seconds (`--shutdown-seconds`, 15 to 120); each Docker command has at most
five seconds, with time reserved for fresh readback. A final leader wait/kill
adds at most three seconds. Detached checkpoint process groups remain owned
by the series, and leader reaping is labeled separately from runtime settlement.
This is a bounded shutdown protocol, not a guarantee of immediate cessation
of all load after a resource guard's decision.

`exit.json` records the original `collector_rc`, received signal, leader
reaping, cleanup errors and a fresh stopped/unknown state for **every selected
service**. Two fresh inspections must agree, including restart epoch, before
claiming stopped. Both observations require typed non-running, non-restarting,
non-dead state and retain OOM, exit-code and finish-time diagnostics. A changed
pre-stop epoch is never signaled. Missing containers, daemon changes and restarts remain
unknown. A stop return code alone is not sufficient evidence. A nonzero
collector result remains the wrapper result; operator signals yield 128 plus
signal number. If the collector succeeds but cleanup is unproven, the wrapper
returns nonzero while preserving `collector_rc: 0`.

Stopped means observed stopped at readback time. Restart policies and later
daemon/operator actions can change that state. This does not prove queue
deletion, rollback, database acceptance or future restart safety. Every later
invocation requires fresh queue/browser admission and unchanged pinned epochs;
after any restart, pin a new identity. Do not edit historical manifests or
relax the series' immutable resume binding to reuse old artifacts.

Verification runs from a separate `git clone --no-hardlinks`: pure fake
Docker/health/child fault tests in `tests/test_cre_owned_runtime.py`, plus the
opt-in `python3 tests/run_owned_runtime_contract.py`. The latter uses cached
`postgres:18.6-alpine` only as a shell/sleep image, with no database process,
network, ports or persistent volume. It verifies fresh stops for two owned
containers while an unrelated sentinel remains running, then removes only
its recorded created IDs. It is not a live CRE pipeline acceptance test.
