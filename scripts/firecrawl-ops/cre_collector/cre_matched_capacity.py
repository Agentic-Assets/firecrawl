#!/usr/bin/env python3
"""AGENTIC-2900: foreground, no-write matched detail calibration.

No runtime startup, profile transition, inventory request, or database connection
is provided. Execution requires an already reviewed, private, pinned runtime and
fresh immutable cohort. The canonical lock excludes the ordinary CRE pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_owned_runtime as owned
from cre_memory_guard import HostMemoryGuard
from cre_owned_processes import (
    OwnedProcessGroup,
    _defer_cleanup_signals,
    settle_owned_groups,
)
from cre_performance import (
    capture_runtime_configuration,
    run_observed_command,
    runtime_api_endpoint,
)
from cre_resource_usage import observe_resource_usage

MAX_BYTES = 8 * 1024 * 1024
WIDTHS = (1, 2, 4, 8)
LIMIT_FIELDS = (
    "cpu_limit",
    "memory_limit_bytes",
    "memory_swap_limit_bytes",
    "pids_limit",
    "shm_size_bytes",
)


class CalibrationRejected(RuntimeError):
    """A bounded calibration stopped; no later cell may run."""


def chained_error(error, kind):
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, kind):
            return item
        pending.extend((item.__cause__, item.__context__))
    return None


class RuntimeActivitySampler:
    """Sampled lower bounds only; never infer provider attempts or exact peaks."""

    def __init__(
        self,
        directory: Path,
        api: str,
        browser: str,
        *,
        health=owned.read_health,
        interval=2.0,
    ):
        self.path = directory / "runtime-activity.jsonl"
        self.api, self.browser, self.health, self.interval = (
            api,
            browser,
            health,
            interval,
        )
        self.event = threading.Event()
        self.thread = None
        self.failed = False
        self.count = 0
        self.previous = None
        self.max_gap = 0.0
        self.started = time.monotonic()
        self.peaks = {
            key: 0
            for key in (
                "jobsInQueue",
                "activeJobsInQueue",
                "waitingJobsInQueue",
                "activePages",
            )
        }
        self.fd = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )

    def sample(self):
        start = time.monotonic()
        values = {}
        try:
            queue = self.health(self.api + "/v2/team/queue-status", start + 4)
            queue = queue.get("data", queue)
            page = self.health(self.browser + "/health", start + 4)
            values = {key: queue.get(key) for key in self.peaks if key != "activePages"}
            values["activePages"] = page.get("activePages")
            if page.get("status") != "healthy" or any(
                type(value) is not int or value < 0 for value in values.values()
            ):
                raise CalibrationRejected("activity counters unavailable")
            if time.monotonic() - start > 4:
                raise CalibrationRejected("activity read exceeded budget")
        except Exception:  # noqa: BLE001 - body-free diagnostic classification
            self.failed = True
        observed = time.monotonic()
        gap = 0.0 if self.previous is None else observed - self.previous
        self.max_gap = max(self.max_gap, gap)
        self.previous = observed
        if gap > 7:
            self.failed = True
        if self.event.is_set():
            return
        record = {
            "observed_at": refresh.utc_now(),
            "elapsed_seconds": observed - self.started,
            "sample_duration_seconds": observed - start,
            "gap_seconds": gap,
            "status": "unavailable" if self.failed else "observed",
            "values": values,
        }
        try:
            raw = (json.dumps(record, allow_nan=False) + "\n").encode()
            if len(raw) > 2048 or os.fstat(self.fd).st_size + len(raw) > 1024 * 1024:
                raise CalibrationRejected("activity evidence exceeded bound")
            if os.write(self.fd, raw) != len(raw):
                raise CalibrationRejected("activity journal short write")
            os.fsync(self.fd)
            self.count += 1
            for key, value in values.items():
                if type(value) is int:
                    self.peaks[key] = max(self.peaks[key], value)
        except Exception:  # noqa: BLE001 - required evidence fails closed
            self.failed = True

    def _run(self):
        while not self.event.wait(self.interval):
            self.sample()
            if self.failed:
                return

    def __enter__(self):
        try:
            self.sample()
            if self.failed:
                raise CalibrationRejected("initial activity evidence unavailable")
        except BaseException:
            os.close(self.fd)
            raise
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, _kind, error, _tb):
        with _defer_cleanup_signals():
            try:
                self.event.set()
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    raise refresh.ProcessCleanupUnknown(
                        "activity sampler did not quiesce"
                    )
                os.close(self.fd)
                write_json(
                    self.path.with_suffix(".summary.json"),
                    {
                        "sampled_high_water_lower_bounds": self.peaks,
                        "samples": self.count,
                        "elapsed_seconds": time.monotonic() - self.started,
                        "max_gap_seconds": self.max_gap,
                        "coverage": "periodic_samples_can_miss_between_sample_peaks",
                        "status": "unavailable" if self.failed else "observed",
                    },
                )
            except (Exception, KeyboardInterrupt) as cleanup:
                unknown = chained_error(error, refresh.ProcessCleanupUnknown)
                if unknown is not None:
                    raise unknown from cleanup
                raise refresh.ProcessCleanupUnknown(
                    "activity finalization evidence unavailable"
                ) from cleanup
            if self.failed:
                unknown = chained_error(error, refresh.ProcessCleanupUnknown)
                if unknown is not None:
                    raise unknown
                raise CalibrationRejected(
                    "activity telemetry failed; later cells forbidden"
                ) from error


def read_bytes(path: Path, limit=MAX_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or meta.st_nlink != 1
            or meta.st_mode & 0o077
            or meta.st_size > limit
        ):
            raise CalibrationRejected("unsafe private input")
        chunks, count = [], 0
        while count <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            chunks.append(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise CalibrationRejected("private input exceeded size bound")
        return raw
    finally:
        os.close(fd)


def read_json(path: Path) -> dict:
    value = json.loads(read_bytes(path))
    if not isinstance(value, dict):
        raise CalibrationRejected("private input must be an object")
    return value


def verify_inventory(cohort: dict, raw: bytes, max_age_hours: float) -> None:
    body = cohort["cohort"]
    artifact = json.loads(raw)
    meta = artifact.get("runMeta", {})
    enumeration = (
        artifact.get("kind") == "cre_matched_inventory"
        and meta.get("mode") == "enumeration_only"
    )
    if body["source"] == "jll" and not enumeration:
        raise CalibrationRejected("JLL native enumeration artifact required")
    if (
        hashlib.sha256(raw).hexdigest() != body.get("inventorySha256")
        or (not enumeration and meta.get("mode") != "monitor")
        or meta.get("startedAt") != body.get("inventoryStartedAt")
        or meta.get("finishedAt") != body.get("inventoryFinishedAt")
        or not isinstance(artifact.get("listings"), list)
    ):
        raise CalibrationRejected("original enumeration artifact binding rejected")
    if enumeration:
        native = artifact.get("nativeResponse", {})
        scope = artifact.get("scope", {})
        if (
            body["source"] != "jll"
            or scope.get("source") != "jll"
            or scope.get("page") != 1
            or scope.get("clientDispatches") != 1
            or scope.get("fullInventory") is not False
            or not isinstance(native.get("body"), str)
            or hashlib.sha256(native["body"].encode()).hexdigest()
            != native.get("sha256")
        ):
            raise CalibrationRejected("native enumeration evidence binding rejected")
    start = datetime.fromisoformat(body["inventoryStartedAt"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - start).total_seconds()
    if not 0 <= age <= max_age_hours * 3600:
        raise CalibrationRejected("original inventory exceeded predeclared age")
    for member in body["members"]:
        rows = [
            row
            for row in artifact["listings"]
            if str(row.get("id")) == member["providerId"]
            and row.get("url") == member["canonicalUrl"]
            and row.get("sourceKey") == body["source"]
        ]
        if (
            len(rows) != 1
            or rows[0] != member["baseRow"]
            or rows[0].get("detailObservedAt")
            or rows[0].get("freshnessProvenance", {}).get("detailScope")
            == "detail_page"
        ):
            raise CalibrationRejected("full original inventory row binding rejected")


def write_json(path: Path, value: dict) -> None:
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
    write_bytes(path, raw)


def write_bytes(path: Path, raw: bytes, limit=MAX_BYTES) -> None:
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
    ):
        raise CalibrationRejected("unsafe output directory")
    if len(raw) > limit:
        raise CalibrationRejected("output exceeded size bound")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # Do not overwrite or retry a partial evidence file.
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def validate_inputs(cohort: dict | None, profile: dict, identity: dict) -> None:
    owned.validate_identity(identity)
    body = cohort.get("cohort", {}) if cohort is not None else {}
    if cohort is not None and (
        body.get("source") not in {"jll", "marcus-millichap"}
        or not isinstance(body.get("members"), list)
        or not 8 <= len(body["members"]) <= (24 if body.get("source") == "jll" else 128)
        or not isinstance(cohort.get("cohortSha256"), str)
        or len(cohort["cohortSha256"]) != 64
    ):
        raise CalibrationRejected("invalid cohort envelope")
    # The real worker independently validates every identity, observation and
    # the canonical complete-cohort digest before importing a source adapter.
    if (
        profile.get("schema_version") != 1
        or profile.get("kind") != "cre_matched_capacity_profile"
        or profile.get("project") != identity["project"]
        or not isinstance(profile.get("review_reference"), str)
        or not profile["review_reference"].strip()
        or set(profile.get("services", {})) != {"api", "playwright-service"}
    ):
        raise CalibrationRejected("explicit reviewed runtime profile required")
    for service in profile["services"].values():
        if (
            not isinstance(service, dict)
            or set(service) != set(LIMIT_FIELDS)
            or any(
                type(service[key]) not in (int, float)
                or not math.isfinite(service[key])
                or service[key] < 0
                for key in LIMIT_FIELDS
            )
        ):
            raise CalibrationRejected("all actual finite runtime limits required")
        if service["cpu_limit"] <= 0 or service["memory_limit_bytes"] <= 0:
            raise CalibrationRejected("bounded CPU and memory required")
    for key in ("api_queue_cap", "browser_page_cap"):
        if type(profile.get(key)) is not int or not 1 <= profile[key] <= 32:
            raise CalibrationRejected("bounded runtime admission caps required")


def resource_evidence(
    run_dir: Path,
    cell_dir: Path,
    identity: dict,
    result: dict,
    lifecycle_start: str,
    *,
    qualifying: bool,
    work_deadline=None,
) -> dict:
    """Require saved core measurements; never relabel lifecycle idle as load."""

    def timestamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    started = timestamp(lifecycle_start)
    acquired_start, acquired_end = (
        timestamp(result["startedAt"]),
        timestamp(result["finishedAt"]),
    )
    expected = {
        row["id"]
        for row in identity["containers"]
        if row["service"] in {"api", "playwright-service"}
    }
    deadline = (
        min(time.monotonic() + 10, work_deadline)
        if work_deadline is not None
        else time.monotonic() + 10
    )
    while True:
        rows = []
        try:
            raw = read_bytes(run_dir / "resource-usage.jsonl", 16 * 1024 * 1024)
            for line in raw.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    continue  # only the concurrent writer's unfinished final record
                record = json.loads(line)
                observed = timestamp(record["observed_at"])
                if observed > time.time() + 1:
                    raise CalibrationRejected(
                        "resource observation clock is in the future"
                    )
                if observed >= started:
                    rows.append((observed, record))
        except FileNotFoundError:
            pass
        valid, overlapping = [], []
        for observed, record in rows:
            host, runtime = record.get("host", {}), record.get("runtime", {})
            containers = runtime.get("containers", [])
            numbers = [
                host.get("cpu_percent"),
                host.get("memory_total_bytes"),
                host.get("vm", {}).get("compressor_bytes"),
                host.get("swap", {}).get("used_bytes"),
            ]
            numbers.extend(
                value
                for row in containers
                for value in (row.get("cpu_percent"), row.get("memory_current_bytes"))
            )
            duration = record.get("sample_duration_seconds")
            if (
                record.get("kind") != "cre_resource_usage"
                or runtime.get("availability") != "available"
                or {row.get("id") for row in containers} != expected
                or host.get("memory_pressure") not in {"normal", "warning", "critical"}
                or type(duration) not in (int, float)
                or not math.isfinite(duration)
                or not 0 <= duration <= 4
                or any(
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                    for value in numbers
                )
            ):
                raise CalibrationRejected(
                    "required cell resource measurements degraded"
                )
            valid.append(observed)
            if observed <= acquired_end and observed + duration >= acquired_start:
                overlapping.append(observed)
        if valid:
            gaps = [later - earlier for earlier, later in pairwise(valid)]
            if any(gap > 10 for gap in gaps):
                raise CalibrationRejected("required cell resource coverage gap")
            if overlapping or not qualifying or time.monotonic() >= deadline:
                summary = {
                    "lifecycle_usable_samples": len(valid),
                    "acquisition_overlapping_read_windows": len(overlapping),
                    "max_observed_gap_seconds": max(gaps) if gaps else None,
                    "measurement_complete_for_qualification": bool(overlapping),
                    "coverage": "sample_read_windows; periodic observations can miss peaks; lifecycle idle is not acquisition load",
                }
                write_json(cell_dir / "resource-evidence.json", summary)
                if qualifying and not overlapping:
                    raise CalibrationRejected(
                        "qualifying cell lacks acquisition resource coverage"
                    )
                return summary
        if time.monotonic() >= deadline:
            raise CalibrationRejected("required cell resource journal missing")
        time.sleep(0.1)


def bounded_child(
    argv: Sequence[str], log_path: Path, *, env: Mapping[str, str], timeout: float
) -> int:
    """Own a fresh group; never reap before the last possible cleanup signal."""
    if timeout <= 0:
        raise TimeoutError("experiment work deadline exceeded")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    log = os.fdopen(fd, "w")
    try:
        with _defer_cleanup_signals():
            proc = subprocess.Popen(
                list(argv),
                cwd=refresh.COLLECTOR_DIR,
                env=dict(env),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
            try:
                group = OwnedProcessGroup.from_spawn(proc)
            except (Exception, KeyboardInterrupt) as registration:
                raise refresh.ProcessCleanupUnknown(
                    "spawned child ownership registration unavailable"
                ) from registration
        try:
            rc = proc.wait(timeout=timeout)
        except BaseException as interrupted:
            with _defer_cleanup_signals():
                settled = settle_owned_groups([group], grace_seconds=15, kill_seconds=5)
                try:
                    write_json(
                        log_path.with_suffix(".settlement.json"),
                        {
                            "local_settled": settled.settled,
                            "groups": list(settled.groups),
                            "errors": list(settled.errors),
                            "remote_settlement": "not_proven",
                        },
                    )
                except (Exception, KeyboardInterrupt) as evidence:
                    raise refresh.ProcessCleanupUnknown(
                        "matched local settlement evidence unavailable"
                    ) from evidence
                if not settled.settled:
                    raise refresh.ProcessCleanupUnknown(
                        "matched child cleanup_unknown"
                    ) from interrupted
            raise
        # A normally reaped leader can leave a descendant. The shared helper
        # refuses to signal an ambiguously reused group and fences uncertainty.
        settled = settle_owned_groups([group], grace_seconds=0, kill_seconds=0.1)
        try:
            write_json(
                log_path.with_suffix(".settlement.json"),
                {
                    "local_settled": settled.settled,
                    "groups": list(settled.groups),
                    "errors": list(settled.errors),
                    "remote_settlement": "not_proven",
                },
            )
        except (Exception, KeyboardInterrupt) as evidence:
            raise refresh.ProcessCleanupUnknown(
                "matched local settlement evidence unavailable"
            ) from evidence
        if not settled.settled:
            raise refresh.ProcessCleanupUnknown("matched child descendants unresolved")
        return rc
    finally:
        with _defer_cleanup_signals():
            active = sys.exception()
            try:
                log.close()
            except (Exception, KeyboardInterrupt) as exc:
                if active is not None:
                    raise active from exc
                raise


def no_write_env(api: str) -> dict[str, str]:
    env = refresh.safe_process_env()
    for key in list(env):
        if key.startswith(
            ("SUPABASE", "PG", "POSTGRES", "DATABASE", "SWARM_")
        ) or key in {"DB_URL", "CRE_ENV_FILE", "CRE_ACTIVATE_STATUS"}:
            env.pop(key)
    env.update(
        FIRECRAWL_API_URL=api,
        API_URL=api,
        CRE_REQUIRE_FRESH_DETAILS="1",
        CRE_SCRAPE_MAX_ATTEMPTS="1",
    )
    return env


def check_runtime(
    runtime,
    identity: dict,
    profile: dict,
    api: str,
    browser: str,
    directory: Path,
    env: dict,
) -> dict:
    owned.admit_runtime(runtime, identity, api, browser)
    snapshot = capture_runtime_configuration(directory, env=env)
    if not snapshot or snapshot.get("availability") != "available":
        raise CalibrationRejected("runtime settings evidence unavailable")
    rows = {row["service"]: row for row in snapshot["containers"]}
    pins = {row["service"]: row for row in identity["containers"]}
    for service, expected in profile["services"].items():
        actual = rows.get(service, {})
        if any(actual.get(key) != value for key, value in expected.items()) or any(
            actual.get(key) != pins[service].get(key)
            for key in ("id", "image", "started_at")
        ):
            raise CalibrationRejected(
                "runtime limits or epoch differ from reviewed profile"
            )
    deadline = time.monotonic() + 5
    queue = owned.read_health(api + "/v2/team/queue-status", deadline)
    queue = queue.get("data", queue)
    page = owned.read_health(browser + "/health", deadline)
    if (
        type(queue.get("maxConcurrency")) is not int
        or type(page.get("maxConcurrentPages")) is not int
        or queue.get("maxConcurrency") != profile["api_queue_cap"]
        or page.get("maxConcurrentPages") != profile["browser_page_cap"]
    ):
        raise CalibrationRejected("runtime concurrency differs from reviewed profile")
    return {
        **snapshot,
        "api_queue_cap": queue["maxConcurrency"],
        "browser_page_cap": page["maxConcurrentPages"],
    }


def run_cells(
    args,
    cohort: dict,
    profile: dict,
    identity: dict,
    runtime,
    env: dict,
    cpu,
    *,
    runner=bounded_child,
    admission=check_runtime,
    clock=time.monotonic,
    deadline=None,
) -> list[dict]:
    """Sequential fresh cells; every rejection prevents subsequent dispatch."""
    deadline = clock() + args.total_seconds if deadline is None else deadline
    cells = []
    first_baseline = None
    for replicate in range(1, args.replicates + 1):
        latency_baseline = None
        for width in WIDTHS:
            # Reserve bounded settings/admission and final remote readback time.
            if deadline - clock() < 45:
                raise TimeoutError("experiment work deadline exceeded")
            lifecycle_start = datetime.now(timezone.utc).isoformat()
            generation = refresh.allocate_run_id()
            verify_inventory(
                cohort,
                read_bytes(args.inventory, 32 * 1024 * 1024),
                args.max_inventory_age_hours,
            )
            cell_dir = args.out / generation
            cell_dir.mkdir(mode=0o700)
            cache = cell_dir / "cache"
            cache.mkdir(mode=0o700)
            refresh.set_cpu_guard_context(
                cpu,
                phase="collection",
                source=cohort["cohort"]["source"],
                active_operation=f"matched_r{replicate}_c{width}",
            )
            snapshot = admission(
                runtime,
                identity,
                profile,
                args.api_url,
                args.browser_url,
                cell_dir,
                env,
            )
            body = {
                **cohort,
                "inventoryArtifact": str(args.inventory),
                "maxInventoryAgeHours": args.max_inventory_age_hours,
                "generationId": generation,
                "concurrency": width,
                "context": {
                    "runtimeSnapshot": snapshot,
                    "resourceJournal": str(args.out / "resource-usage.jsonl"),
                    "concurrentHostWork": "unknown",
                },
            }
            if first_baseline is not None:
                body["baseline"] = first_baseline
            write_json(cell_dir / "input.json", body)
            child_env = {
                **env,
                "CRE_REFRESH_GENERATION": generation,
                "CRE_REFRESH_STARTED_AT": cohort["cohort"]["inventoryStartedAt"],
                "CRE_MATCHED_CAPACITY_INPUT": str(cell_dir / "input.json"),
                "CRE_MATCHED_CAPACITY_OUTPUT": str(cell_dir),
                "JLL_DETAIL_CACHE_DIR": str(cache),
            }
            commands = (
                ["node", "--import", "tsx", "sources/matched-capacity-worker.ts"],
                [
                    sys.executable,
                    "cre_ingest.py",
                    "--in",
                    str(cell_dir / "collector.json"),
                    "--dry-run",
                    "--require-strict-freshness",
                    "--keep-artifacts",
                    str(cell_dir / "sql"),
                ],
            )
            cell_deadline = min(deadline - 32, clock() + args.cell_seconds)
            with RuntimeActivitySampler(cell_dir, args.api_url, args.browser_url):
                for index, command in enumerate(commands):
                    rc = run_observed_command(
                        command,
                        cell_dir / f"command-{index}.log",
                        env=child_env,
                        runner=lambda argv, log_path, *, env, end=cell_deadline: runner(
                            argv, log_path, env=env, timeout=end - clock()
                        ),
                    )
                    if rc != 0:
                        raise CalibrationRejected(
                            "detail worker or strict SQL dry-run rejected"
                        )
            result = read_json(cell_dir / "cell.json")
            if (
                result.get("cohortSha256") != cohort["cohortSha256"]
                or result.get("generationId") != generation
                or result.get("requestedConcurrency") != width
                or result.get("peakActiveEnrichments") != width
            ):
                raise CalibrationRejected("cell result binding mismatch")
            p95 = result.get("latencyMs", {}).get("p95")
            if type(p95) not in (int, float) or not math.isfinite(p95) or p95 <= 0:
                raise CalibrationRejected("cell latency evidence invalid")
            latency_baseline = p95 if latency_baseline is None else latency_baseline
            if p95 > latency_baseline * args.max_p95_ratio:
                raise CalibrationRejected("predeclared tail latency regression")
            if first_baseline is None:
                first_baseline = result
            paired = None
            if args.paired_jll:
                pair_dir = cell_dir / refresh.allocate_run_id()
                pair_dir.mkdir(mode=0o700)
                pair_output = pair_dir / "output"
                pair_output.mkdir(mode=0o700)
                direct_generation = refresh.allocate_run_id()
                write_json(
                    pair_dir / "input.json",
                    {
                        "schemaVersion": 1,
                        "kind": "cre_jll_captured_pair_request",
                        "renderedCellDirectory": str(cell_dir),
                        "inventoryArtifact": str(args.inventory),
                        "cohortSha256": cohort["cohortSha256"],
                        "requestedConcurrency": width,
                        "directGenerationId": direct_generation,
                        "maxInventoryAgeHours": args.max_inventory_age_hours,
                    },
                )
                pair_env = {
                    **child_env,
                    "CRE_REFRESH_GENERATION": direct_generation,
                    "CRE_JLL_PAIR_INPUT": str(pair_dir / "input.json"),
                    "CRE_JLL_PAIR_OUTPUT": str(pair_output),
                }
                rc = run_observed_command(
                    ["node", "--import", "tsx", "sources/jll-direct-pair-worker.ts"],
                    pair_dir / "command.log",
                    env=pair_env,
                    runner=lambda argv, log_path, *, env, end=cell_deadline: runner(
                        argv, log_path, env=env, timeout=end - clock()
                    ),
                )
                if rc != 0:
                    raise CalibrationRejected(
                        "paired direct acquisition or audit rejected"
                    )
                paired = read_json(pair_output / "pair.json")
                if (
                    paired.get("status") != "paired_equivalent"
                    or paired.get("cohortSha256") != cohort["cohortSha256"]
                    or paired.get("requestedConcurrency") != width
                    or paired.get("directGenerationId") != direct_generation
                    or not isinstance(paired.get("pairs"), list)
                    or [pair.get("sampleId") for pair in paired["pairs"]]
                    != [member["sampleId"] for member in cohort["cohort"]["members"]]
                ):
                    raise CalibrationRejected("paired audit binding rejected")
            measurements = resource_evidence(
                args.out,
                cell_dir,
                identity,
                result,
                lifecycle_start,
                qualifying=args.replicates == 3,
                work_deadline=deadline - 22,
            )
            if paired is not None:
                direct_measurements = resource_evidence(
                    args.out,
                    pair_output,
                    identity,
                    paired["directArm"],
                    lifecycle_start,
                    qualifying=args.replicates == 3,
                    work_deadline=deadline - 22,
                )
                measurements = {
                    "rendered": measurements,
                    "paired_direct": direct_measurements,
                    "measurement_complete_for_qualification": measurements[
                        "measurement_complete_for_qualification"
                    ]
                    and direct_measurements["measurement_complete_for_qualification"],
                }
            # A client result never proves the remote queue/browser settled.
            settled = cell_dir / refresh.allocate_run_id()
            settled.mkdir(mode=0o700)
            admission(
                runtime, identity, profile, args.api_url, args.browser_url, settled, env
            )
            if clock() > deadline:
                raise TimeoutError("experiment work deadline exceeded")
            cells.append(
                {
                    "replicate": replicate,
                    "concurrency": width,
                    "generation": generation,
                    "cell": str(cell_dir / "cell.json"),
                    "strict_sql_dry_run": "passed",
                    "remote_idle": "freshly_observed",
                    "resource_measurements": measurements,
                    "paired_audit": str(pair_output / "pair.json")
                    if paired is not None
                    else None,
                }
            )
    return cells


def execute(args, cohort: dict | None, profile: dict, identity: dict) -> None:
    deadline = time.monotonic() + args.total_seconds
    env = no_write_env(args.api_url)
    # Docker diagnostics honor this exact daemon/context and Compose selection.
    env["DOCKER_CONTEXT"] = identity["daemon"]["context"]
    env["COMPOSE_PROJECT_NAME"] = identity["project"]
    runtime = owned.DockerRuntime(identity["daemon"]["context"])
    cpu = refresh.HostCpuGuard(
        args.out / "host-cpu.jsonl",
        max_percent=90,
        sustain_seconds=10,
        sample_seconds=2,
    )
    memory = HostMemoryGuard(
        args.out / "host-memory.jsonl",
        on_trip=lambda **values: refresh._trip_host_memory_guard(
            **values, context=cpu.incident_context
        ),
        context_provider=lambda: dict(cpu.incident_context),
        sustain_seconds=20,
        sample_seconds=2,
    )
    refresh._clear_cpu_guard_trip()
    previous = signal.signal(signal.SIGTERM, refresh.checkpoint_sigterm_handler)
    admitted = False
    armed = False
    try:
        with (
            refresh.SharedLock(refresh.checkpoint_lock_dir(None)) as shared_lock,
            owned.runtime_lease(identity),
        ):
            try:
                with (
                    refresh.resource_guard_boundary(memory, cpu),
                    observe_resource_usage(
                        args.out,
                        env=env,
                        cpu_sampler=refresh.DarwinCpuSampler(
                            initial_interval_seconds=2
                        ),
                        context_provider=lambda: dict(cpu.incident_context),
                    ),
                ):
                    memory.preflight()
                    memory.start()
                    if refresh.run_cpu_guard_preflight(cpu) >= 90:
                        raise CalibrationRejected("host CPU admission requires below90")
                    cpu.start()
                    admission_dir = args.out / refresh.allocate_run_id()
                    admission_dir.mkdir(mode=0o700)
                    check_runtime(
                        runtime,
                        identity,
                        profile,
                        args.api_url,
                        args.browser_url,
                        admission_dir,
                        env,
                    )
                    admitted = True
                    try:
                        shared_lock.arm_benchmark(
                            {
                                "schema_version": 1,
                                "kind": "cre_matched_capacity_active",
                                "state": "active",
                                "pid": os.getpid(),
                                "result_path": str(args.out / "result.json"),
                            }
                        )
                        armed = True
                    except Exception as marker_error:
                        raise refresh.ProcessCleanupUnknown(
                            "durable calibration interlock unavailable"
                        ) from marker_error
                    if cohort is None:
                        prep = args.out / refresh.allocate_run_id()
                        prep.mkdir(mode=0o700)
                        write_json(
                            prep / "input.json",
                            {
                                "kind": "prepare_marcus"
                                if args.prepare_marcus
                                else "prepare_jll",
                                "transaction": args.jll_transaction,
                                "propertyType": args.jll_property_type,
                                "members": args.members,
                            },
                        )
                        prep_env = {
                            **env,
                            "CRE_MATCHED_CAPACITY_INPUT": str(prep / "input.json"),
                            "CRE_MATCHED_CAPACITY_OUTPUT": str(prep),
                        }
                        refresh.set_cpu_guard_context(
                            cpu,
                            phase="collection",
                            source="marcus-millichap" if args.prepare_marcus else "jll",
                            active_operation="matched_inventory_preparation",
                        )
                        rc = run_observed_command(
                            [
                                "node",
                                "--import",
                                "tsx",
                                "sources/matched-capacity-worker.ts",
                            ],
                            prep / "command.log",
                            env=prep_env,
                            runner=lambda argv, log_path, *, env: bounded_child(
                                argv,
                                log_path,
                                env=env,
                                timeout=min(
                                    args.cell_seconds, deadline - time.monotonic() - 45
                                ),
                            ),
                        )
                        if rc != 0:
                            raise CalibrationRejected(
                                "bounded native inventory preparation rejected"
                            )
                        cohort = read_json(prep / "cohort.json")
                        args.inventory = prep / "inventory.json"
                        validate_inputs(cohort, profile, identity)
                        verify_inventory(
                            cohort,
                            read_bytes(args.inventory, 32 * 1024 * 1024),
                            args.max_inventory_age_hours,
                        )
                        write_json(args.out / "cohort.json", cohort)
                    cells = run_cells(
                        args,
                        cohort,
                        profile,
                        identity,
                        runtime,
                        env,
                        cpu,
                        deadline=deadline,
                    )
                # Both safety threads have quiesced; preserve the marker until
                # final remote idle and terminal evidence are durably observed.
                terminal = args.out / refresh.allocate_run_id()
                terminal.mkdir(mode=0o700)
                check_runtime(
                    runtime,
                    identity,
                    profile,
                    args.api_url,
                    args.browser_url,
                    terminal,
                    env,
                )
                write_json(
                    args.out / "result.json",
                    {
                        "status": "matched_cells_complete",
                        "qualification": "three_cold_replicates"
                        if args.replicates == 3
                        else "exploratory_only",
                        "measurement": "complete"
                        if all(
                            cell["resource_measurements"][
                                "measurement_complete_for_qualification"
                            ]
                            for cell in cells
                        )
                        else "measurement_incomplete",
                        "profile": profile,
                        "cells": cells,
                        "database_writes": False,
                        "adoption": "requires_review",
                        "runtime": "left_running_fresh_idle",
                    },
                )
                with _defer_cleanup_signals():
                    shared_lock.disarm_benchmark()
                    armed = False
            except BaseException as error:
                with _defer_cleanup_signals():
                    stopped = {"ok": False, "action": "not_admitted_no_stop"}
                    cleanup_error = None
                    if admitted:
                        try:
                            stopped = owned.stop_runtime(runtime, identity, seconds=30)
                            write_json(args.out / "stop.json", stopped)
                        except (Exception, KeyboardInterrupt) as cleanup:  # noqa: BLE001 - attempt terminal evidence even when cleanup fails
                            cleanup_error = cleanup
                            stopped = {
                                "ok": False,
                                "action": "cleanup_evidence_unavailable",
                            }
                    # Resource/timeout errors deliberately retain an operator
                    # recovery fence, even when local settlement was observed.
                    recovery = (
                        chained_error(
                            error,
                            (
                                TimeoutError,
                                subprocess.TimeoutExpired,
                                refresh.CpuGuardTrip,
                                refresh.ProcessCleanupUnknown,
                            ),
                        )
                        is not None
                        or refresh._peek_cpu_guard_trip() is not None
                        or (admitted and not stopped.get("ok"))
                        or cleanup_error is not None
                    )
                    code = (
                        "resource_guard"
                        if refresh._peek_cpu_guard_trip() is not None
                        or isinstance(error, refresh.CpuGuardTrip)
                        else "child_timeout"
                        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
                        else "cleanup_unknown"
                        if chained_error(error, refresh.ProcessCleanupUnknown)
                        is not None
                        or cleanup_error is not None
                        else "operator_interrupt"
                        if isinstance(error, KeyboardInterrupt)
                        else "qualification_rejected"
                        if isinstance(error, CalibrationRejected)
                        else "ordinary_failure"
                    )
                    try:
                        write_json(
                            args.out / "failure.json",
                            {
                                "status": "stopped_first_failure",
                                "phase": cpu.incident_context.get("phase", "preflight"),
                                "code": code,
                                "local_process_settlement": "see_private_child_settlement_records",
                                "child_settlement_records": [
                                    str(path.relative_to(args.out))
                                    for path in sorted(
                                        args.out.glob("**/command*.settlement.json")
                                    )
                                ][:64],
                                "remote_cleanup": "stopped_verified"
                                if stopped.get("ok")
                                else "unresolved"
                                if admitted
                                else "not_admitted_no_stop",
                                "recovery_fence_required": recovery,
                                "database_writes": False,
                            },
                        )
                    except (Exception, KeyboardInterrupt) as evidence:
                        unknown = chained_error(error, refresh.ProcessCleanupUnknown)
                        if unknown is not None:
                            raise unknown from evidence
                        raise refresh.ProcessCleanupUnknown(
                            "calibration failure evidence unavailable"
                        ) from evidence
                    if recovery:
                        unknown = chained_error(error, refresh.ProcessCleanupUnknown)
                        if unknown is not None:
                            raise unknown from cleanup_error
                        raise refresh.ProcessCleanupUnknown(
                            "calibration requires operator recovery"
                        ) from error
                    if armed:
                        shared_lock.disarm_benchmark()
                        armed = False
                raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--cohort", type=Path)
    selection.add_argument("--prepare-jll", action="store_true")
    selection.add_argument("--prepare-marcus", action="store_true")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--members", type=int, choices=range(8, 129), default=16)
    parser.add_argument("--jll-transaction", choices=("sale", "lease"), default="sale")
    parser.add_argument(
        "--jll-property-type",
        choices=(
            "office",
            "industrial",
            "retail",
            "land",
            "medical",
            "multifamily",
            "lab",
            "coworking",
            "data-center",
        ),
        default="office",
    )
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--browser-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replicates", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--cell-seconds", type=float, default=300)
    parser.add_argument("--total-seconds", type=float, default=1800)
    parser.add_argument("--max-p95-ratio", type=float, required=True)
    parser.add_argument("--max-inventory-age-hours", type=float, default=2)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--paired-jll", action="store_true")
    args = parser.parse_args(argv)
    if args.prepare_jll and args.members > 24:
        parser.error("JLL preparation is bounded to 24 members")
    if bool(args.cohort) != bool(args.inventory):
        parser.error(
            "cohort requires its original inventory; prepare-jll accepts neither"
        )
    for field, low, high in (
        ("cell_seconds", 30, 900),
        ("total_seconds", 60, 3600),
        ("max_p95_ratio", 1, 4),
        ("max_inventory_age_hours", 0.01, 24),
    ):
        value = getattr(args, field)
        if not math.isfinite(value) or not low <= value <= high:
            parser.error(f"{field} must be finite within [{low},{high}]")
    args.out = args.out.absolute()
    try:
        refresh.validate_run_id(args.out.name)
    except ValueError:
        parser.error("out basename must be YYYY-MM-DDTHHMMSSZ[-12hex]")
    if args.out.exists() or args.out.is_symlink():
        parser.error("out must be a new private evidence directory")
    # Literal loopback families only; no ambiguous localhost selection.
    for key in ("api_url", "browser_url"):
        value = getattr(args, key)
        if (
            runtime_api_endpoint({"FIRECRAWL_API_URL": value}) != value
            or "localhost" in value
        ):
            parser.error("literal loopback origin required")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cohort = read_json(args.cohort) if args.cohort else None
        identity, profile = (read_json(path) for path in (args.identity, args.profile))
        validate_inputs(cohort, profile, identity)
        if args.paired_jll and (
            args.prepare_marcus
            or (cohort is not None and cohort["cohort"]["source"] != "jll")
            or not (
                refresh.COLLECTOR_DIR / "sources/jll-direct-pair-worker.ts"
            ).is_file()
        ):
            raise CalibrationRejected("paired worker unavailable or source is not JLL")
        original_inventory = (
            read_bytes(args.inventory, 32 * 1024 * 1024) if cohort is not None else None
        )
        if cohort is not None:
            verify_inventory(cohort, original_inventory, args.max_inventory_age_hours)
        if not args.execute:
            print(
                "plan validated; execution requires --execute and a free canonical CRE lock"
            )
            return 0
        args.out.mkdir(mode=0o700, parents=False)
        if original_inventory is not None:
            write_bytes(
                args.out / "inventory.json", original_inventory, 32 * 1024 * 1024
            )
            args.inventory = args.out / "inventory.json"
        for name, value in (
            ("cohort", cohort),
            ("identity", identity),
            ("profile", profile),
        ):
            if value is not None:
                write_json(args.out / f"{name}.json", value)
        execute(args, cohort, profile, identity)
        return 0
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - sanitized terminal boundary
        # Never expose source rows, URLs, contacts or caller exception strings.
        resource = refresh._peek_cpu_guard_trip() is not None
        print(
            "calibration stopped: resource_guard"
            if resource
            else "calibration stopped: rejected_or_interrupted",
            file=sys.stderr,
        )
        return 75 if resource else 1


if __name__ == "__main__":
    raise SystemExit(main())
