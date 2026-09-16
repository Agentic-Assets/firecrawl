# Phase 2: image and Linux runner

- Image `firecrawl-playwright-service-c10:local` rebuilt (id a5225c40abd6, 2.47GB, cached ~8s). Compose needs placeholder
  values for its required env vars just to interpolate at build time (by design).
- Run path: generic Linux runner container (`docker-compose.c10-runner.yaml`, `capacity_c10/tools/linux-runner.Dockerfile`,
  `capacity_c10/tools/run_linux_controller.sh`) with repo bind mount, host network (sidecar endpoint is 127.0.0.1), and
  Docker socket for the controller's own `docker compose` sidecar lifecycle. Linux-only receipt store check untouched.
- `capacity_c10/tools/c10_linux_preflight.py`: offline 5-check preflight (platform, real PrivateReceiptStore round trip,
  docker socket, image, compose config). All 5 passed inside runner. Offline `collect-jll` (no `--execute`) intent OK.
- Gates: pytest 3468 passed / 1 skipped; tsc clean; unit 976 passed / 1 skipped; ruff clean.
- Commit `9dcfb4300` pushed. Runbook: `docs/firecrawl-ops/c10-live-calibration-phase2-2026-09-16.md`.
