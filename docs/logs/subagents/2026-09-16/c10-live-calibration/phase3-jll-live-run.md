# Phase 3: live JLL admission run

Outcome: BLOCKED before any JLL request. No live admission data exists.

1. Attempt 1 (Phase 3 agent) stopped at `SharedLock.acquire()`: the canonical `out/daily/.cre.lock` held pytest
   quarantine residue from 2026-09-15 (dead pid 40557, `bold-jll-128`, `candidate_baseline_rollback_failed`).
2. Test leak: still live. Session-wide guard added in `tests/conftest.py` plus `tests/test_lock_isolation_guard.py`.
   It caught a live leak in `test_capacity_c10_production.py::test_production_dry_run_rejects_counterbalance_without_all_p1_admissions`.
   Full suite left lock hashes unchanged. Commit `95167b66f`.
3. The governed recovery needed the exact runtime baseline. `firecrawl-playwright-service-1` had drifted to 4G mem /
   15.7G shm from the repo-declared 16G/16G/8G. The orchestrator recreated only that service from `docker-compose.yaml`
   (it was idle, and the healthcheck plus scrape smoke passed afterward). Pre-recreate inspect is at
   `tasks/tmp/c10-live-calibration/playwright-pre-recreate-inspect.json`.
   The Phase 3 subagent declined to do this on its own authority, so the orchestrator ran it directly.
4. `recover-quarantine` dry run passed. The first `--execute` failed before claiming the guard with
   `CpuTelemetryError: Darwin CPU tick counters did not advance` (defect candidate: sampling window). The retry
   completed: phase=completed, archive `out/daily/.cre-quarantine-forensics/e68ea0eb...`, receipt sha `b5544a42...`.
5. Attempt 2 (fresh agent, runner rebuilt at `95167b66f`) stopped at `SharedLock.acquire()` in 0.55s. The completed
   recovery guard binds absolute host paths and `(st_dev, st_ino)`. Neither survives the OrbStack bind mount
   (host `(16777235, 38957426)` vs container `(35, 5067)`). The fail-closed identity check is correct and was not weakened.
6. Orchestrator proof: `fcntl.flock` does NOT coordinate across the macOS host / OrbStack container boundary. A host
   process and a container process both acquired LOCK_EX|LOCK_NB on the same bind-mounted file at the same time.
   So the Phase 2 Linux runner cannot safely take part in the canonical CRE lock alongside host collectors.
   Changing that needs a design decision, not a patch: a host-side lock broker, a real Linux host, or moving all
   CRE lock holders into one kernel.

No DB, authority-pin, scheduler, or P0/P1 action was taken. No C10 sidecar was ever started.
