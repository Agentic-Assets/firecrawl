#!/usr/bin/env python3
"""Run the full CRE registry as bounded, serial checkpoint generations.

Each source gets its own `cre_checkpoint_refresh.py` run so no source's
observation window is extended by unrelated slow providers. Source-local
collection or coverage failures are recorded and the series continues. CPU,
database, validation, infrastructure, and operator interruptions stop the
series immediately. Every live write remains inside the checkpoint runner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from cre_checkpoint_refresh import (
    COLLECTOR_DIR,
    DEFAULT_CPU_SAMPLE_SECONDS,
    DEFAULT_CPU_SUSTAIN_SECONDS,
    DEFAULT_MAX_HOST_CPU_PERCENT,
    DEFAULT_MAX_RESUME_AGE_HOURS,
    SOURCE_KEYS,
    atomic_write_json,
    git_identity,
    utc_now,
)


SCHEMA_VERSION = 1
DEFAULT_OUT_ROOT = COLLECTOR_DIR / "out" / "checkpoint-series"
SOURCE_LOCAL_FAILURE_PREFIXES = (
    "RefreshError: source checkpoints remain incomplete:",
    "RefreshError: aggregate coverage gate is not established for source(s):",
)
SUCCESS_STATUS = "supported_scope_complete"
RESOURCE_GUARD_STATUS = "resource_guard_interrupted"


class SeriesError(RuntimeError):
    """The bounded checkpoint series cannot proceed safely."""


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def parse_sources(value: str) -> tuple[str, ...]:
    if value.strip().lower() == "all":
        return SOURCE_KEYS
    sources = tuple(item.strip() for item in value.split(",") if item.strip())
    if not sources:
        raise ValueError("at least one source is required")
    unknown = [source for source in sources if source not in SOURCE_KEYS]
    if unknown:
        raise ValueError("unknown source(s): " + ", ".join(unknown))
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate sources are not allowed")
    return sources


def series_config(
    *,
    sources: Sequence[str],
    page_cap: int,
    concurrency: int,
    attempts_per_source: int,
    max_resume_age_hours: float,
    max_host_cpu_percent: float,
    cpu_sustain_seconds: float,
    cpu_sample_seconds: float,
    nice: int,
) -> dict[str, Any]:
    return {
        "sources": list(sources),
        "transactions": ["sale", "lease"],
        "page_cap": page_cap,
        "concurrency": concurrency,
        "source_workers": 1,
        "attempts_per_source": attempts_per_source,
        "max_resume_age_hours": max_resume_age_hours,
        "host_cpu_guard": {
            "max_host_cpu_percent": max_host_cpu_percent,
            "sustain_seconds": cpu_sustain_seconds,
            "sample_seconds": cpu_sample_seconds,
        },
        "nice": nice,
        "continue_source_local_failures": True,
    }


def new_manifest(
    series_dir: Path,
    *,
    git_sha: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "series_id": series_dir.name,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "collector_git_sha": git_sha,
        "collector_git_dirty": False,
        "config": dict(config),
        "sources": {
            source: {
                "state": "pending",
                "attempts": [],
                "checkpoint_run": None,
                "checkpoint_status": None,
                "error": None,
            }
            for source in config["sources"]
        },
        "error": None,
    }


def save_manifest(series_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(series_dir / "manifest.json", manifest)


def load_resume_manifest(
    manifest_path: Path,
    *,
    git_sha: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeriesError(f"cannot read series manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise SeriesError("unsupported checkpoint-series manifest")
    if value.get("collector_git_sha") != git_sha:
        raise SeriesError("cannot resume series with a different collector Git SHA")
    if value.get("config") != dict(config):
        raise SeriesError("resume configuration differs from the series manifest")
    checkpoints = value.get("sources")
    if not isinstance(checkpoints, dict) or set(checkpoints) != set(config["sources"]):
        raise SeriesError("series source checkpoints do not match configuration")
    return value


def build_checkpoint_argv(
    source: str,
    *,
    child_out_root: Path,
    env_file: str | None,
    config: Mapping[str, Any],
    resume_run: Path | None = None,
) -> list[str]:
    guard = config["host_cpu_guard"]
    argv = [
        "/usr/bin/nice",
        "-n",
        str(config["nice"]),
        sys.executable,
        str(COLLECTOR_DIR / "cre_checkpoint_refresh.py"),
        "--out-root",
        str(child_out_root),
        "--sources",
        source,
        "--transactions",
        "both",
        "--page-cap",
        str(config["page_cap"]),
        "--concurrency",
        str(config["concurrency"]),
        "--source-workers",
        "1",
        "--attempts-per-source",
        str(config["attempts_per_source"]),
        "--max-resume-age-hours",
        str(config["max_resume_age_hours"]),
        "--max-host-cpu-percent",
        str(guard["max_host_cpu_percent"]),
        "--cpu-sustain-seconds",
        str(guard["sustain_seconds"]),
        "--cpu-sample-seconds",
        str(guard["sample_seconds"]),
    ]
    if resume_run is not None:
        argv.extend(["--resume", str(resume_run)])
    if env_file:
        argv.extend(["--env-file", env_file])
    return argv


def source_local_failure(child_manifest: Mapping[str, Any]) -> bool:
    if child_manifest.get("status") != "failed":
        return False
    error = child_manifest.get("error")
    return isinstance(error, str) and error.startswith(SOURCE_LOCAL_FAILURE_PREFIXES)


def _load_child_manifest(
    child_out_root: Path,
    *,
    before: set[Path],
    source: str,
    git_sha: str,
) -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in child_out_root.glob("*/manifest.json"):
        if manifest_path in before:
            continue
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            value.get("collector_git_sha") == git_sha
            and (value.get("config") or {}).get("sources") == [source]
        ):
            candidates.append((manifest_path, value))
    if len(candidates) != 1:
        raise SeriesError(
            f"expected exactly one new checkpoint manifest for {source}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _terminate_child(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def checkpoint_series_sigterm_handler(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def run_series(
    series_dir: Path,
    manifest: dict[str, Any],
    *,
    env_file: str | None,
    retry_failed: bool,
) -> int:
    config = manifest["config"]
    git_sha = manifest["collector_git_sha"]
    child_out_root = series_dir / "runs"
    child_out_root.mkdir(parents=True, exist_ok=True)
    log_root = series_dir / "logs"
    log_root.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(config["sources"], start=1):
        checkpoint = manifest["sources"][source]
        prior_state = checkpoint["state"]
        if prior_state == "complete":
            continue
        if prior_state == "failed_source" and not retry_failed:
            continue

        resume_run = None
        if (
            prior_state in {"resource_guard_interrupted", "interrupted"}
            and checkpoint.get("checkpoint_run")
        ):
            resume_run = series_dir / checkpoint["checkpoint_run"]

        attempt_number = len(checkpoint["attempts"]) + 1
        log_path = log_root / f"{index:02d}-{source}-attempt-{attempt_number}.log"
        attempt = {
            "number": attempt_number,
            "started_at": utc_now(),
            "finished_at": None,
            "rc": None,
            "log": str(log_path.relative_to(series_dir)),
        }
        checkpoint["attempts"].append(attempt)
        checkpoint["state"] = "running"
        checkpoint["error"] = None
        save_manifest(series_dir, manifest)

        before = set(child_out_root.glob("*/manifest.json"))
        argv = build_checkpoint_argv(
            source,
            child_out_root=child_out_root,
            env_file=env_file,
            config=config,
            resume_run=resume_run,
        )
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[{utc_now()}] command: {' '.join(argv)}\n")
            log.flush()
            proc = subprocess.Popen(
                argv,
                cwd=COLLECTOR_DIR,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                _terminate_child(proc)
                attempt["finished_at"] = utc_now()
                attempt["rc"] = 130
                checkpoint["state"] = "interrupted"
                checkpoint["error"] = "operator interruption"
                manifest["status"] = "interrupted"
                manifest["error"] = f"interrupted while running {source}"
                manifest["finished_at"] = utc_now()
                save_manifest(series_dir, manifest)
                return 130
            log.write(f"[{utc_now()}] rc={rc}\n")

        attempt["finished_at"] = utc_now()
        attempt["rc"] = rc
        try:
            if resume_run is not None:
                child_manifest_path = resume_run / "manifest.json"
                child_manifest = json.loads(
                    child_manifest_path.read_text(encoding="utf-8")
                )
            else:
                child_manifest_path, child_manifest = _load_child_manifest(
                    child_out_root,
                    before=before,
                    source=source,
                    git_sha=git_sha,
                )
            if not isinstance(child_manifest, dict):
                raise SeriesError("checkpoint manifest root must be an object")
        except (OSError, json.JSONDecodeError, SeriesError) as exc:
            checkpoint["state"] = "failed_global"
            checkpoint["error"] = str(exc)
            manifest["status"] = "failed"
            manifest["error"] = str(exc)
            manifest["finished_at"] = utc_now()
            save_manifest(series_dir, manifest)
            return rc or 1

        checkpoint["checkpoint_run"] = str(
            child_manifest_path.parent.relative_to(series_dir)
        )
        checkpoint["checkpoint_status"] = child_manifest.get("status")
        checkpoint["error"] = child_manifest.get("error")

        if rc == 0 and child_manifest.get("status") == SUCCESS_STATUS:
            checkpoint["state"] = "complete"
            save_manifest(series_dir, manifest)
            continue
        if rc == 75 or child_manifest.get("status") == RESOURCE_GUARD_STATUS:
            checkpoint["state"] = "resource_guard_interrupted"
            manifest["status"] = "resource_guard_interrupted"
            manifest["error"] = f"host CPU guard interrupted {source}"
            manifest["finished_at"] = utc_now()
            save_manifest(series_dir, manifest)
            return 75
        if source_local_failure(child_manifest):
            checkpoint["state"] = "failed_source"
            save_manifest(series_dir, manifest)
            continue

        checkpoint["state"] = "failed_global"
        manifest["status"] = "failed"
        manifest["error"] = (
            f"global checkpoint failure for {source}: "
            f"{child_manifest.get('error') or f'exit {rc}'}"
        )
        manifest["finished_at"] = utc_now()
        save_manifest(series_dir, manifest)
        return rc or 1

    failed_sources = [
        source
        for source, checkpoint in manifest["sources"].items()
        if checkpoint["state"] == "failed_source"
    ]
    manifest["status"] = (
        "complete_with_source_failures" if failed_sources else "complete"
    )
    manifest["error"] = (
        "source-local failures: " + ", ".join(failed_sources)
        if failed_sources
        else None
    )
    manifest["finished_at"] = utc_now()
    save_manifest(series_dir, manifest)
    print(series_dir)
    return 2 if failed_sources else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--sources", default="all")
    parser.add_argument("--page-cap", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--attempts-per-source", type=int, default=3)
    parser.add_argument(
        "--max-resume-age-hours",
        type=float,
        default=DEFAULT_MAX_RESUME_AGE_HOURS,
    )
    parser.add_argument(
        "--max-host-cpu-percent",
        type=float,
        default=DEFAULT_MAX_HOST_CPU_PERCENT,
    )
    parser.add_argument(
        "--cpu-sustain-seconds",
        type=float,
        default=DEFAULT_CPU_SUSTAIN_SECONDS,
    )
    parser.add_argument(
        "--cpu-sample-seconds",
        type=float,
        default=DEFAULT_CPU_SAMPLE_SECONDS,
    )
    parser.add_argument("--nice", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        sources = parse_sources(args.sources)
    except ValueError as exc:
        parser.error(str(exc))
    if args.page_cap < 1 or not 1 <= args.concurrency <= 6:
        parser.error("page-cap must be positive and concurrency must be between 1 and 6")
    if args.attempts_per_source < 1:
        parser.error("attempts-per-source must be positive")
    if not math.isfinite(args.max_resume_age_hours) or args.max_resume_age_hours <= 0:
        parser.error("max-resume-age-hours must be finite and positive")
    if (
        not math.isfinite(args.max_host_cpu_percent)
        or not 0 < args.max_host_cpu_percent < 100
    ):
        parser.error("max-host-cpu-percent must be finite and between 0 and 100")
    if not math.isfinite(args.cpu_sustain_seconds) or args.cpu_sustain_seconds <= 0:
        parser.error("cpu-sustain-seconds must be finite and positive")
    if (
        not math.isfinite(args.cpu_sample_seconds)
        or args.cpu_sample_seconds <= 0
        or args.cpu_sample_seconds > args.cpu_sustain_seconds
    ):
        parser.error(
            "cpu-sample-seconds must be positive and no greater than cpu-sustain-seconds"
        )
    if not 0 <= args.nice <= 20:
        parser.error("nice must be between 0 and 20")

    git_sha, git_dirty = git_identity()
    if git_dirty:
        raise SeriesError("refusing checkpoint series from a dirty checkout")
    config = series_config(
        sources=sources,
        page_cap=args.page_cap,
        concurrency=args.concurrency,
        attempts_per_source=args.attempts_per_source,
        max_resume_age_hours=args.max_resume_age_hours,
        max_host_cpu_percent=args.max_host_cpu_percent,
        cpu_sustain_seconds=args.cpu_sustain_seconds,
        cpu_sample_seconds=args.cpu_sample_seconds,
        nice=args.nice,
    )

    if args.resume:
        supplied = Path(args.resume).expanduser().resolve()
        manifest_path = supplied if supplied.name == "manifest.json" else supplied / "manifest.json"
        series_dir = manifest_path.parent
        manifest = load_resume_manifest(
            manifest_path,
            git_sha=git_sha,
            config=config,
        )
        manifest["status"] = "running"
        manifest["error"] = None
        manifest["finished_at"] = None
    else:
        series_dir = Path(args.out_root).expanduser().resolve() / _run_id()
        if series_dir.exists():
            raise SeriesError(f"series directory already exists: {series_dir}")
        series_dir.mkdir(parents=True)
        manifest = new_manifest(series_dir, git_sha=git_sha, config=config)
    save_manifest(series_dir, manifest)

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, checkpoint_series_sigterm_handler)
    try:
        return run_series(
            series_dir,
            manifest,
            env_file=args.env_file,
            retry_failed=args.retry_failed,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
