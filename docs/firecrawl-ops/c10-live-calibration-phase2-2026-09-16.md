# C10 live calibration, Phase 2 (2026-09-16)

Continuation of the JLL admission bridge work
(`docs/firecrawl-ops/c10-jll-admission-lane.md`,
`docs/firecrawl-ops/c10-jll-admission-closeout-2026-09-16.md`). Phase 2 rebuilt
the sidecar image, built and proved a generic Linux runner for the
Linux-only host coordinator, ran the offline JLL preflight, and ran every
collector gate. **No provider request was made.** No database, cache,
listing, scheduler, or authority write occurred.

## 1. Sidecar image build

```bash
cd /Users/caymanseagraves/Github/agentic-assets/firecrawl
C10_BROWSER_PRIVATE_ENV_FILE=tasks/tmp/c10-live-calibration/dummy_c10_env_file.env \
MAX_CONCURRENT_PAGES=1 C10_PROFILE_SHA256=$(printf '0%.0s' {1..64}) \
C10_BROWSER_CPUS=1 C10_BROWSER_PIDS=100 C10_BROWSER_HOST_PORT=39999 \
docker compose -f docker-compose.yaml -f docker-compose.c10.yaml build playwright-service-c10
```

`docker-compose.c10.yaml` requires several `?`-marked env vars (private env
file, capacity/profile/resource limits, host port) to interpolate even for a
plain `build`, though none of them affect the build itself. The six
placeholder values above are for build-time interpolation only; a real run
must never reuse them (`host_sidecar.py`'s `DockerComposeSidecar.start`
supplies the reviewed values itself).

Result: build succeeded, ~8s wall time (base/Playwright layers cached from a
prior build), image `firecrawl-playwright-service-c10:local`,
id `a5225c40abd6`, size `2.47GB`.

## 2. Generic Linux runner for the host coordinator

`capacity_c10.host_store.PrivateReceiptStore` refuses on any
`sys.platform != "linux"` by design, and `host_sidecar.py` shells out to
`docker compose` directly (Docker-outside-of-Docker, not a nested daemon).
On a macOS operator machine the Python controller and its `node --import tsx`
child must therefore run inside a Linux container that can still reach the
host's own Docker daemon and the sidecar's `127.0.0.1:<port>` publication.

New, generic (non-JLL, non-C10-specific beyond "run this controller on
Linux") tooling:

- `docker-compose.c10-runner.yaml` -- opt-in overlay defining `c10-linux-runner`:
  `network_mode: host` (required -- `host_orchestration.py` and
  `admission_controller.py` hardcode `http://127.0.0.1:<port>`, which is only
  reachable from a container sharing the daemon's own network namespace),
  the host Docker socket bind-mounted (DooD), the repo bind-mounted at
  `/workspace/firecrawl`, and a named volume for the collector's
  `node_modules` (so a Linux container never overwrites the operator's
  macOS/arm64 `node_modules` through the repo bind mount).
- `scripts/firecrawl-ops/cre_collector/capacity_c10/tools/linux-runner.Dockerfile`
  -- `node:22-bookworm-slim` (matches the collector's own `>=22` engine
  requirement) plus `python3` (stdlib only -- `capacity_c10` has no pip
  dependencies) and the Docker Compose v2 CLI plugin (Debian bookworm's
  `docker.io` package does not ship it; installed as the pinned static
  binary from `docker/compose` releases, not a generic curl-pipe-shell).
- `scripts/firecrawl-ops/cre_collector/capacity_c10/tools/run_linux_controller.sh`
  -- generic wrapper: `build|up|down|shell|exec CMD...|npm-install`. It only
  manages the runner container; it has no knowledge of JLL, admission, or
  P0/P1.
- `scripts/firecrawl-ops/cre_collector/capacity_c10/tools/c10_linux_preflight.py`
  -- generic environment preflight (see below).

Proven end-to-end on this Mac (OrbStack, Docker server `linux/arm64`):

```bash
cd /Users/caymanseagraves/Github/agentic-assets/firecrawl
bash scripts/firecrawl-ops/cre_collector/capacity_c10/tools/run_linux_controller.sh build
bash scripts/firecrawl-ops/cre_collector/capacity_c10/tools/run_linux_controller.sh up
bash scripts/firecrawl-ops/cre_collector/capacity_c10/tools/run_linux_controller.sh npm-install
bash scripts/firecrawl-ops/cre_collector/capacity_c10/tools/run_linux_controller.sh \
  exec python3 scripts/firecrawl-ops/cre_collector/capacity_c10/tools/c10_linux_preflight.py
```

All five preflight checks passed inside the runner: `linux_platform`,
`private_receipt_store` (a real `PrivateReceiptStore.create()` seal/read/close
round-trip on a fresh temp 0700 root), `docker_socket` (`docker version`
reached the host daemon over the mounted socket), `c10_image_present`, and
`compose_config_renders` (the exact `docker compose -f docker-compose.yaml -f
docker-compose.c10.yaml config playwright-service-c10` invocation
`host_sidecar.py` itself uses, confirming the image reference resolves
correctly from the container's view of the repo). `node --import tsx` also
verified working under Node 22. The runner container was torn down
(`run_linux_controller.sh down`) after verification; nothing was left
running.

The preflight never calls `docker compose ... up`, so it never starts the
sidecar and never touches the network at all -- not even the sidecar's own
loopback health check.

## 3. JLL admission preflight (offline, no network)

The lane's own offline dry-run validates the fixed request graph without
Docker or Linux:

```bash
cd /Users/caymanseagraves/Github/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector
python3 -m capacity_c10.jll_admission collect-jll
```

Output confirms `external_calls: false` and the fixed intent: source `jll`,
selection rule `jll-canonical-url-lexicographic-v1`, one GraphQL enumeration
card (`sale`/`office`/page 1), 16 members, all `no_write` counters at zero.

The current repository JLL adapter implementation digest (from
`capacity_c10.authority.repository_implementation_sha256("jll")` on this
checkout, computed offline, no network):

```
94c6dae7bda753e28f206c8e23e6f3d6f5a724689dfcb6f2a4588d4995789527
```

This value changes if `sources/jll.ts` or any file it depends on changes; the
next phase must recompute it on the exact commit it runs from rather than
reuse this number.

**No live network collection was performed in this phase.** Actually invoking
`capacity_c10.production.execute_jll_admission_collection` starts the sidecar
and immediately issues the real GraphQL enumeration request against
`property.jll.com` -- there is no partial "start sidecar, verify health, stop"
mode in that function, so exercising it for real *is* the live run. That is
explicitly out of scope for this phase.

> **Superseded (2026-09-16, live-calibration session):** section 4 below, as
> originally written, recommended the generic Linux runner container as "a
> viable substitute" for a real Linux host on a non-Linux operator machine.
> That is no longer accepted: this session established that `fcntl.flock`
> does not coordinate between a macOS host process and a process reached
> through an OrbStack bind mount (a host process and a container process
> have both been observed holding `LOCK_EX|LOCK_NB` on the same file at
> once), so the runner container must never hold the canonical CRE lock on
> a macOS host. See
> `docs/firecrawl-ops/c10-live-calibration-jll-run-2026-09-16.md` ("Recommended
> next step") and `docs/firecrawl-ops/c10-live-calibration-2026-09-16.md` for
> the full finding and the gated options going forward
> (`CRE_LOCK_DOMAIN_UNTRUSTED` now fails closed on this in code). Section 4's
> command sequence is still the right admission steps once run from a
> trusted lock domain; only the "runner container as substitute host" claim
> below is retracted.

## 4. Exact commands for the next phase (live run)

On a real Linux production host only (not this Mac, and not the generic
Linux runner container from section 2: `PrivateReceiptStore` requires real
Linux, but the runner container is reached from macOS over an OrbStack bind
mount whose `fcntl.flock` does not coordinate with the host -- see the
superseded note above. Do not substitute the container for a real Linux
host for any lock-holding live-run step):

```bash
cd /Users/caymanseagraves/Github/agentic-assets/firecrawl

# 1. Fresh, empty, owner-0700 receipt root. Never reuse an existing root --
#    execute_jll_admission_collection requires a fresh, empty, 0700 leaf and
#    a prior partial/quarantined root must never be resumed.
install -d -m 0700 tasks/tmp/c10-live-calibration/receipts-run-$(date +%Y%m%dT%H%M%S)

# 2. Rebuild the sidecar image from the exact reviewed checkout being run
#    (skip only if step 1 of this doc was already done on this exact commit):
docker compose -f docker-compose.yaml -f docker-compose.c10.yaml build playwright-service-c10

# 3. From a Python REPL or a short one-off script with cwd at
#    scripts/firecrawl-ops/cre_collector, call the sole supported
#    provider-facing admission action directly -- there is still no CLI
#    wrapper for it (only tests call it today; adding a production CLI is a
#    separately reviewed change, not part of this phase):
python3 - <<'PY'
from pathlib import Path
from capacity_c10 import production
from capacity_c10.authority import repository_implementation_sha256

repo_root = Path("/absolute/path/to/firecrawl")  # adjust
receipt_root = Path("/absolute/path/to/receipts-run-XXXXXXXX")  # from step 1

adapter_sha256 = repository_implementation_sha256("jll")  # recompute on this commit

# planSha256/cohortSha256/policySha256/sourceSha256/armSha256 are operator-
# supplied provenance identifiers for *this* run (any valid 64-hex-char
# sha256-shaped string); execute_jll_admission_collection does not check
# them against a checked-in authority (that happens later, at
# `build-jll-bundle` / `render-jll-authority` / a reviewed pin PR).
# implementationSha256 MUST equal adapter_sha256 -- it is cross-checked.
binding = {
    "planSha256": "<sha256 naming this run's intended plan>",
    "cohortSha256": "<sha256 naming the JLL cohort>",
    "policySha256": "<sha256 naming the policy this run claims>",
    "sourceSha256": "<sha256 naming the jll source recipe>",
    "armSha256": "<sha256 naming this arm>",
    "implementationSha256": adapter_sha256,
}

result = production.execute_jll_admission_collection(
    repo_root=repo_root,
    receipt_root=receipt_root,
    binding=binding,
    adapter_implementation_sha256=adapter_sha256,
)
print(result)
PY

# 4. Build the reviewable bundle from the sealed receipts, then render (never
#    silently install) the proposed authority:
python3 -m capacity_c10.jll_admission build-jll-bundle \
  --receipt-root /absolute/path/to/receipts-run-XXXXXXXX \
  --receipt-manifest /absolute/path/to/receipts-run-XXXXXXXX/<manifest-file> \
  --admission-root /absolute/path/to/admission-root
python3 -m capacity_c10.jll_admission render-jll-authority \
  --bundle /absolute/path/to/admission-root/<bundle-file>

# 5. Open a distinct, human-reviewed pin PR installing the rendered authority
#    proposal into the checked-in JLL authority file. Only after that pin is
#    merged can the ordinary production controller use the JLL plan for a
#    P0/P1 arm -- and P0/P1 arming remains separately gated (operator
#    approval, this session's own restrictions).
```

Receipt/bundle/admission roots must all live under a gitignored path (e.g.
`tasks/tmp/c10-live-calibration/...` or `scripts/firecrawl-ops/cre_collector/out/...`),
never inside a tracked directory. If the sidecar teardown at the end of step 3
cannot be proven, the run fails closed: the canonical lock stays retained, a
quarantine record names the exact compose project, and
`docker ps --all --filter label=com.docker.compose.project=<project>` is the
documented recovery check (`production.py`'s own message on a failed
`up`/teardown names the exact rebuild command if the image is somehow
missing).

## 5. Gates (exact head, this branch)

| Gate | Result |
| --- | --- |
| `python3 -m pytest tests/ -q -p no:cacheprovider` | 3468 passed, 1 skipped |
| `test_nested_dummy_process_is_reaped_before_cohort_worker_shutdown` in isolation | passed |
| `npx tsc --noEmit` | pass |
| `npm run test:unit` | 976 passed, 1 skipped |
| `uvx ruff check` / `ruff format --check` on changed Python | clean |
| `python3 -m py_compile` on changed Python | clean |
| `bash -n` shell-syntax guard (auto-discovers `*.sh`, now includes `run_linux_controller.sh`) | clean |

`apps/playwright-service-ts` was not touched this phase; its gates were not
re-run.

## Files added this phase

- `docker-compose.c10-runner.yaml`
- `scripts/firecrawl-ops/cre_collector/capacity_c10/tools/linux-runner.Dockerfile`
- `scripts/firecrawl-ops/cre_collector/capacity_c10/tools/run_linux_controller.sh`
- `scripts/firecrawl-ops/cre_collector/capacity_c10/tools/c10_linux_preflight.py`
- `scripts/firecrawl-ops/cre_collector/tests/test_run_linux_controller_sh.py`
- `scripts/firecrawl-ops/cre_collector/tests/test_c10_linux_preflight.py`
- This document.

No existing file was modified.
