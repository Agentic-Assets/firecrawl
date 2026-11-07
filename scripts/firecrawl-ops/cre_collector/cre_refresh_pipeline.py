"""Bounded scheduling helpers for the existing foreground checkpoint owner."""

from __future__ import annotations

import threading
import json
import uuid
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CALLBACK: ContextVar[Callable[[], None] | None] = ContextVar(
    "cre_pipeline_poll_callback", default=None
)
_IN_CALLBACK: ContextVar[bool] = ContextVar("cre_pipeline_in_callback", default=False)


@contextmanager
def command_poll_hook(
    callback: Callable[[], None],
) -> Iterator[Callable[[], None]]:
    """Keep one owner's acquisitions moving during DB/compute command waits.

    Only the main coordinator may install this hook. Its callback must perform
    bounded collection settlement/publication/dispatch, never database work.
    The yielded callable and command-wait polling share the reentrancy guard.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("pipeline polling requires the main owner thread")
    if _CALLBACK.get() is not None:
        raise RuntimeError("nested pipeline polling contexts are forbidden")
    if not callable(callback):
        raise TypeError("pipeline polling requires a callable")

    def invoke() -> None:
        if _IN_CALLBACK.get():
            return
        token = _IN_CALLBACK.set(True)
        try:
            callback()
        finally:
            _IN_CALLBACK.reset(token)

    token = _CALLBACK.set(invoke)
    try:
        yield invoke
    finally:
        _CALLBACK.reset(token)


def poll_hook() -> Callable[[], None] | None:
    """Return the current scoped, non-reentrant collection callback."""
    return _CALLBACK.get()


def wait_with_poll_hook(process: Any) -> int:
    """Preserve the original wait unless overlap was explicitly installed."""
    callback = poll_hook()
    if callback is None:
        return process.wait()
    while True:
        rc = process.poll()
        if rc is not None:
            return rc
        callback()
        time.sleep(0.05)


# These are dispatch bounds, never artifact acceptance limits.

_OWNER_CHECK: ContextVar[Callable[[], None] | None] = ContextVar(
    "cre_pipeline_owner_check", default=None
)
_OWNER_LEASE: ContextVar[str | None] = ContextVar(
    "cre_pipeline_owner_lease", default=None
)

_COMPUTE_ROOT: ContextVar[Path | None] = ContextVar(
    "cre_pipeline_compute_root", default=None
)
MAX_COMPUTE_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class PipelineBudget:
    max_pending: int = 5
    soft_bytes: int = 512 * 1024**2
    output_reservation: int = 64 * 1024**2
    min_free_bytes: int = 1024**3

    def can_dispatch(self, *, pending_count, observed_bytes, active_sizes, free_bytes):
        remaining = sum(max(0, self.output_reservation - size) for size in active_sizes)
        return (
            pending_count < self.max_pending
            and observed_bytes + remaining < self.soft_bytes
            and free_bytes >= self.min_free_bytes + remaining + self.output_reservation
        )


def resolve_source_workers(pipeline: bool, configured: int | None) -> int:
    value = (2 if pipeline else 1) if configured is None else configured
    if type(value) is not int or not 1 <= value <= 4:
        raise ValueError("source-workers must be between 1 and 4")
    return value


def compute_root(path: Path) -> Path:
    return _COMPUTE_ROOT.get() or path.parent


@contextmanager
def artifact_compute_scope(root: Path) -> Iterator[None]:
    """Isolate pure inspection for an already-held owner, without polling work.

    This scope conveys no lock or database authority. Worker subprocesses and
    unscoped offline callers retain local computation, preventing recursion.
    """
    token = _COMPUTE_ROOT.set(root)
    try:
        yield
    finally:
        _COMPUTE_ROOT.reset(token)


def artifact_compute_enabled() -> bool:
    return _COMPUTE_ROOT.get() is not None or poll_hook() is not None


def _private_compute_directory(root: Path) -> Path:
    """Create/open the private artifact directory without following links.

    Existing directories are never chmodded: an unsafe path is a refusal that
    needs operator inspection, not something the producer should repair.
    """
    import cre_checkpoint_refresh as refresh
    from cre_stop_guarded_tier import StopRefused, _safe_stat

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(root_fd, directory=True)
        try:
            os.mkdir("pipeline", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        pipeline_fd = os.open(
            "pipeline",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            _safe_stat(pipeline_fd, directory=True)
            try:
                os.mkdir("compute", 0o700, dir_fd=pipeline_fd)
            except FileExistsError:
                pass
            compute_fd = os.open(
                "compute",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=pipeline_fd,
            )
            try:
                observed = _safe_stat(compute_fd, directory=True, private=True)
                if (
                    not observed.st_mode & stat.S_IWUSR
                    or not observed.st_mode & stat.S_IXUSR
                ):
                    raise StopRefused("unsafe local evidence")
                named = os.stat("compute", dir_fd=pipeline_fd, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (observed.st_dev, observed.st_ino):
                    raise StopRefused("changed local evidence")
                return root / "pipeline" / "compute"
            finally:
                os.close(compute_fd)
        finally:
            os.close(pipeline_fd)
    except OSError as exc:
        raise refresh.GlobalStageError("unsafe pipeline artifact directory") from exc
    finally:
        os.close(root_fd)


def run_artifact_compute(root: Path, action: str, arguments: dict) -> dict:
    """Run pure heavy work in one owned subprocess; consume bounded metadata."""
    import cre_checkpoint_refresh as refresh

    directory = _private_compute_directory(root)
    request_id = uuid.uuid4().hex
    request = directory / f"{request_id}.request.json"
    response = directory / f"{request_id}.response.json"
    refresh.atomic_write_json(
        request,
        {
            "request_id": request_id,
            "action": action,
            "arguments": arguments,
        },
    )
    argv = [
        refresh.sys.executable,
        str(Path(__file__).with_name("cre_pipeline_artifact_worker.py")),
        "--request",
        str(request),
        "--response",
        str(response),
    ]
    rc = refresh.run_command(
        argv,
        root / "logs" / f"compute-{request_id}.log",
        env=refresh.safe_process_env(),
    )
    if rc != 0 or response.is_symlink() or not response.is_file():
        raise refresh.GlobalStageError("pipeline artifact worker failed")
    if response.stat().st_size > MAX_COMPUTE_RESPONSE_BYTES:
        raise refresh.GlobalStageError(
            "pipeline artifact response exceeds metadata bound"
        )
    try:
        result = json.loads(response.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise refresh.GlobalStageError("invalid pipeline artifact response") from exc
    if not isinstance(result, dict) or result.get("request_id") != request_id:
        raise refresh.GlobalStageError("pipeline artifact response identity mismatch")
    if result.get("ok") is not True:
        if (result.get("ok") is False and result.get("daum_all_identities_held") is True
                and set(result) == {"request_id", "ok", "daum_all_identities_held"}
                and action == "admit" and arguments.get("source") == "daum-commercial"
                and arguments.get("phase") == "source"):
            raise refresh.DaumAllTargetHeld("all emitted DAUM observations are held")
        if result.get("artifact_rejected") is True:
            raise refresh.ArtifactValidationError(
                result.get("error", "artifact rejected")
            )
        raise refresh.GlobalStageError("pipeline artifact computation failed")
    value = result.get("value")
    if not isinstance(value, dict):
        raise refresh.GlobalStageError("pipeline artifact metadata is invalid")
    if action in {"hash", "validate"}:
        if (
            not isinstance(value.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
            or type(value.get("bytes")) is not int
            or value["bytes"] < 0
        ):
            raise refresh.GlobalStageError(
                "pipeline artifact digest metadata is invalid"
            )
    if action == "validate":
        for key in (
            "flat_listings",
            "staged_unique",
            "inventory_only",
            "rejected_by_ingest",
            "detail_errors",
        ):
            if type(value.get(key)) is not int or value[key] < 0:
                raise refresh.GlobalStageError(
                    "pipeline artifact statistics are invalid"
                )
        if value["bytes"] <= 0:
            raise refresh.GlobalStageError(
                "pipeline artifact must contain complete JSON"
            )
    if action == "admit":
        expected_report = response.with_suffix(".admission.json")
        if (
            value.get("report_path") != str(expected_report)
            or not isinstance(value.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
        ):
            raise refresh.GlobalStageError(
                "pipeline admission response path or digest changed"
            )
    return value


def require_pipeline_owner(manifest: dict, expected_lease: str | None) -> None:
    """Observe the existing canonical owner; acquire no additional authority."""
    import cre_checkpoint_refresh as refresh

    callback = _OWNER_CHECK.get()
    if callback is not None:
        callback()
    lock_path = (manifest.get("preflight") or {}).get("shared_lock")
    if not isinstance(lock_path, str):
        raise refresh.GlobalStageError("pipeline canonical owner evidence is missing")
    lock = Path(lock_path)
    if (
        refresh._lock_owner(lock) != os.getpid()
        or not expected_lease
        or refresh._lock_lease(lock) != expected_lease
    ):
        raise refresh.GlobalStageError("pipeline canonical owner changed")


def pending_disk_bytes(run_dir: Path, excluded: set[Path]) -> int:
    """Charge unconsumed source outputs; retained cache/audit use free disk."""
    total = 0
    for directory, _, files in os.walk(run_dir / "sources", followlinks=False):
        for filename in files:
            path = Path(directory) / filename
            if path not in excluded:
                try:
                    total += path.lstat().st_size
                except FileNotFoundError:
                    pass  # a collector may atomically replace its own temp file
    return total


def archive_failed_candidate(run_dir: Path, manifest: dict, source: str) -> None:
    import cre_checkpoint_refresh as refresh

    checkpoint = manifest["sources"][source]
    attempt = checkpoint["attempts"][-1]
    # Franklin may already have archived the original before derivation failed.
    # Keep both that evidence and any derived candidate in their private paths.
    if source in {"franklin-street", "nai-global"} and attempt.get("rejected_artifact"):
        return
    attempt["rejected_artifact"], attempt["rejected_artifact_sha256"] = (
        refresh.archive_rejected_with_evidence(
            run_dir / checkpoint["candidate"]["path"], run_dir, manifest, source, attempt
        )
    )
    if source == "matthews":
        refresh.retain_matthews_partial_candidate(run_dir, manifest, attempt)


def prepare_candidate(run_dir: Path, manifest: dict, source: str) -> bool:
    import cre_checkpoint_refresh as refresh

    checkpoint = manifest["sources"][source]
    candidate = checkpoint.get("candidate") or {}
    path = run_dir / candidate["path"]
    generation_bound = (
        source in refresh.STRICT_FRESHNESS_SOURCE_KEYS
        or source in refresh.PROPERTY_DETAIL_FRESHNESS_SOURCE_KEYS
    )
    try:
        stats = run_artifact_compute(
            run_dir,
            "validate",
            {
                "path": str(path),
                "source": source,
                "file_identity": candidate["file_identity"],
                "attempt_started_at": candidate["attempt_started_at"],
                "kwargs": {
                    "require_strict_freshness": source
                    in refresh.STRICT_FRESHNESS_SOURCE_KEYS,
                    "expected_generation_id": manifest["run_id"]
                    if generation_bound
                    else None,
                    "expected_generation_started_at": manifest["started_at"]
                    if generation_bound
                    else None,
                    "expected_transactions": manifest["config"]["transactions"],
                    "daum_verified_subset": refresh.current_inventory.is_verified_subset(
                        manifest["config"], source
                    ),
                    "pyramid_subset_allowed": refresh.pyramid_subset_authorized(manifest["config"], source),
                },
            },
        )
    except refresh.ArtifactValidationError as source_error:
        if source == "nai-global":
            from datetime import datetime, timezone
            from nai_partial.inventory_recovery import _FRESHNESS_ERROR, prepare_owned
            attempts = checkpoint.get("attempts") or []
            attempt = attempts[-1] if attempts else {}
            if (_FRESHNESS_ERROR.fullmatch(str(source_error))
                    and attempt.get("number") == candidate.get("attempt_number")
                    and type(attempt.get("rc")) is int and attempt["rc"] == 0
                    and not attempt.get("rejected_artifact")):
                original = path.lstat()
                identity = [original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns]
                if identity != candidate.get("file_identity"):
                    raise refresh.GlobalStageError("NAI recovery source candidate changed")
                rejected = run_dir / "rejected" / f"{source}-attempt-{attempt['number']}.json"
                if os.path.lexists(rejected):
                    raise refresh.GlobalStageError("NAI recovery rejected evidence path occupied")
                attempt["error"] = str(source_error)
                relative, digest = refresh.archive_rejected_with_evidence(path, run_dir, manifest, source, attempt)
                attempt["rejected_artifact"], attempt["rejected_artifact_sha256"] = relative, digest
                refresh.save_manifest(run_dir, manifest)
                owner = getattr(_OWNER_CHECK.get(), "__self__", None)
                require_pipeline_owner(manifest, _OWNER_LEASE.get())
                recovery = prepare_owned(run_dir, manifest, owner, now=datetime.now(timezone.utc))
                require_pipeline_owner(manifest, _OWNER_LEASE.get())
                if recovery is not None:
                    return prepare_candidate(run_dir, manifest, source)
                raise source_error
        if source != "franklin-street" or checkpoint.get("franklin_original"):
            raise
        attempts = checkpoint.get("attempts") or []
        if not attempts or attempts[-1].get("number") != candidate.get("attempt_number"):
            raise refresh.GlobalStageError("Franklin candidate attempt changed")
        attempt = attempts[-1]
        original_stat = path.lstat()
        identity = [original_stat.st_dev, original_stat.st_ino, original_stat.st_size,
                    original_stat.st_mtime_ns, original_stat.st_ctime_ns]
        if identity != candidate.get("file_identity"):
            raise refresh.GlobalStageError("Franklin candidate changed after inspection")
        rejected = run_dir / "rejected" / f"{source}-attempt-{attempt['number']}.json"
        if os.path.lexists(rejected):
            raise refresh.GlobalStageError("Franklin rejected evidence path already occupied")
        # Archive the original exactly once before creating the private subset.
        relative, digest = refresh.archive_rejected_with_evidence(path, run_dir, manifest, source, attempt)
        attempt["rejected_artifact"], attempt["rejected_artifact_sha256"] = relative, digest
        if not refresh.prepare_franklin_partial_candidate(run_dir, manifest, attempt, candidate_only=True):
            raise
        candidate = checkpoint["candidate"]
        path = run_dir / candidate["path"]
        return prepare_candidate(run_dir, manifest, source)
    # The subprocess cannot replace the parent's canonical source state.
    checkpoint["artifact"] = {
        **stats,
        "path": candidate["path"],
        "attempt_started_at": candidate["attempt_started_at"],
    }
    checkpoint["state"] = "validated"
    refresh.save_manifest(run_dir, manifest)
    return True


def preseal_essex_identity(run_dir: Path, manifest: dict, artifact: Path, env_file,
                           coverage_path: Path, coverage_digest: str, coverage_gate: dict) -> bool:
    """Hold only a fully bound positive Essex history conflict before sealing."""
    import hashlib
    import sys

    import cre_checkpoint_refresh as refresh
    from cre_code_handoff_git import read_file
    from cre_essex_identity import projection_digest, stage_essex_identity

    source = "essex-realty"
    checkpoint = manifest["sources"][source]
    artifact_record = dict(checkpoint["artifact"])
    binding = {"artifact_sha256": checkpoint["artifact"]["sha256"],
               "generation_id": manifest["run_id"], "producer_sha": manifest["collector_git_sha"],
               "database_target_sha256": refresh.manifest_database_target_sha256(manifest)}
    require_pipeline_owner(manifest, _OWNER_LEASE.get())
    raw = read_file(artifact)
    if (manifest.get("collector_git_dirty") is not False or not binding["database_target_sha256"]
            or refresh.git_identity() != (binding["producer_sha"], False)
            or refresh.database_target_fingerprint(env_file).get("value") != binding["database_target_sha256"]
            or type(checkpoint["artifact"].get("bytes")) is not int
            or len(raw) != checkpoint["artifact"]["bytes"]
            or refresh._relative_to_run(artifact, run_dir) != checkpoint["artifact"].get("path")
            or refresh.sha256_file(artifact) != binding["artifact_sha256"]):
        raise refresh.GlobalStageError("Essex pre-seal input identity changed")
    try:
        payload = json.loads(raw)
        if (payload.get("runMeta") or {}).get("freshness", {}).get("generationId") != binding["generation_id"]:
            raise ValueError("generation differs")
        rows = stage_essex_identity(payload)
    except (ValueError, TypeError, KeyError) as exc:
        raise refresh.GlobalStageError("Essex pre-seal staging invalid") from exc
    report = run_dir / "gates" / "essex-realty-identity-preseal.json"
    log = run_dir / "logs" / "essex-realty-identity-preseal.log"
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists() or report.is_symlink():
        raise refresh.GlobalStageError("Essex pre-seal output already exists")
    argv = [sys.executable, str(refresh.COLLECTOR_DIR / "cre_essex_identity.py"),
            "--artifact", str(artifact), "--artifact-sha256", binding["artifact_sha256"],
            "--generation", binding["generation_id"], "--producer", binding["producer_sha"],
            "--target-sha256", binding["database_target_sha256"], "--out", str(report)]
    if env_file:
        argv.extend(["--env-file", str(env_file)])
    rc = refresh.run_command(argv, log, env=refresh.safe_process_env())
    if type(rc) is not int or rc != 0:
        raise refresh.GlobalStageError("Essex pre-seal identity probe failed")
    try:
        def object_pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate report field")
                result[key] = value
            return result

        result_raw = read_file(report)
        log_raw = read_file(log)
        result = json.loads(result_raw, object_pairs_hook=object_pairs)
        expected = {"schema", "source", "artifact_sha256", "generation_id", "producer_sha",
                    "database_target_sha256", "staged_count", "staged_sha256", "conflicts", "verdict"}
        valid = (isinstance(result, dict) and set(result) == expected
                 and result["schema"] == "cre-essex-preseal-identity-v1" and result["source"] == source
                 and all(result[key] == value for key, value in binding.items())
                 and type(result["staged_count"]) is int and result["staged_count"] == len(rows)
                 and result["staged_sha256"] == projection_digest(rows)
                 and type(result["conflicts"]) is int and result["conflicts"] >= 0
                 and result["verdict"] == ("hold" if result["conflicts"] else "clear"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise refresh.GlobalStageError("Essex pre-seal identity report invalid") from exc
    require_pipeline_owner(manifest, _OWNER_LEASE.get())
    if (not valid or read_file(report) != result_raw or read_file(log) != log_raw or read_file(artifact) != raw
            or refresh.git_identity() != (binding["producer_sha"], False)
            or refresh.database_target_fingerprint(env_file).get("value") != binding["database_target_sha256"]
            or manifest.get("collector_git_sha") != binding["producer_sha"]
            or manifest.get("collector_git_dirty") is not False
            or manifest["sources"].get(source) is not checkpoint
            or checkpoint.get("artifact") != artifact_record
            or checkpoint.get("gate") != coverage_gate
            or refresh.sha256_file(coverage_path) != coverage_digest
            or manifest["run_id"] != binding["generation_id"]
            or refresh.manifest_database_target_sha256(manifest) != binding["database_target_sha256"]):
        raise refresh.GlobalStageError("Essex pre-seal identity evidence changed")
    checkpoint["identity_preseal"] = {"path": refresh._relative_to_run(report, run_dir),
                                      "sha256": hashlib.sha256(result_raw).hexdigest(), "bytes": len(result_raw),
                                      "log": refresh._relative_to_run(log, run_dir),
                                      "log_sha256": hashlib.sha256(log_raw).hexdigest(), "log_bytes": len(log_raw),
                                      "verdict": result["verdict"], "conflicts": result["conflicts"], **binding}
    require_pipeline_owner(manifest, _OWNER_LEASE.get())
    if result["conflicts"]:
        checkpoint["state"] = "gate_blocked"
        refresh.save_manifest(run_dir, manifest)
        return False
    refresh.save_manifest(run_dir, manifest)
    return True


def preseal_source_gate(run_dir: Path, manifest: dict, source: str, env_file) -> bool:
    """Retain a confirmed source hold before it can enter a database unit."""
    import cre_checkpoint_refresh as refresh

    checkpoint = manifest["sources"][source]
    artifact = run_dir / checkpoint["artifact"]["path"]
    binding = {
        "collector_git_sha": manifest["collector_git_sha"],
        "database_target_sha256": refresh.manifest_database_target_sha256(manifest),
        "generation_id": manifest["run_id"],
        "artifact_sha256": checkpoint["artifact"]["sha256"],
    }
    require_pipeline_owner(manifest, _OWNER_LEASE.get())
    if (
        manifest.get("collector_git_dirty") is not False
        or refresh.git_identity() != (binding["collector_git_sha"], False)
        or not binding["database_target_sha256"]
        or refresh.sha256_file(artifact) != binding["artifact_sha256"]
    ):
        raise refresh.GlobalStageError(
            f"pre-seal gate input identity changed: {source}"
        )
    try:
        refresh.gate_source(run_dir, manifest, source, artifact, env_file)
    except refresh.NaiPartialProbeHold:
        require_pipeline_owner(manifest, _OWNER_LEASE.get())
        if (source != "nai-global" or refresh.git_identity() != (binding["collector_git_sha"], False)
                or refresh.manifest_database_target_sha256(manifest) != binding["database_target_sha256"]
                or refresh.sha256_file(artifact) != binding["artifact_sha256"]):
            raise refresh.GlobalStageError("NAI held probe owner or target changed")
        checkpoint["state"] = "gate_blocked"
        checkpoint["partial_probe_hold"] = "source_identity_partition_invalid"
        refresh.save_manifest(run_dir, manifest)
        return False
    except refresh.DaumPartialProbeHold as exc:
        require_pipeline_owner(manifest, _OWNER_LEASE.get())
        if (source != "daum-commercial" or refresh.git_identity() != (binding["collector_git_sha"], False)
                or refresh.manifest_database_target_sha256(manifest) != binding["database_target_sha256"]
                or refresh.sha256_file(artifact) != binding["artifact_sha256"]):
            raise refresh.GlobalStageError("DAUM held probe owner or target changed")
        checkpoint["state"] = "gate_blocked"
        checkpoint["partial_probe_hold"] = ("all_emitted_target_held"
                                            if isinstance(exc, refresh.DaumAllTargetHeld)
                                            else "source_identity_partition_invalid")
        refresh.save_manifest(run_dir, manifest)
        return False
    except refresh.LyonPartialProbeHold:
        require_pipeline_owner(manifest, _OWNER_LEASE.get())
        if (source != "lyon-stahl" or refresh.git_identity() != (binding["collector_git_sha"], False)
                or refresh.manifest_database_target_sha256(manifest) != binding["database_target_sha256"]
                or refresh.sha256_file(artifact) != binding["artifact_sha256"]):
            raise refresh.GlobalStageError("Lyon held probe owner or target changed")
        checkpoint["state"] = "gate_blocked"
        checkpoint["partial_probe_hold"] = "source_identity_partition_invalid"
        refresh.save_manifest(run_dir, manifest)
        return False
    gate = checkpoint["gate"]
    path = run_dir / gate["path"]
    digest = refresh.sha256_file(path)
    try:
        report = refresh._load_json(path)
        report_manifest = manifest
        if source == "nai-global" and checkpoint["artifact"].get("nai_coverage_partial") is True:
            import copy
            from nai_partial.coverage_candidate import ORIGINAL
            if artifact == run_dir / ORIGINAL:
                report_manifest = copy.deepcopy(manifest)
                report_manifest["sources"][source]["artifact"]["path"] = ORIGINAL
        refresh.current_inventory.validate_report_scope(
            report, report_manifest, source, artifact
        )
        info = report["per_source"][source]
        summary = report["summary"]
        valid = (
            isinstance(info, dict)
            and isinstance(summary, dict)
            and set(report["per_source"]) == {source}
            and summary.get("mode") == "live"
            and type(summary.get("torow_errors")) is int
            and summary["torow_errors"] == 0
            and summary.get("baseline_updated") is False
            and summary.get("baseline_update_sources") == []
            and type(gate.get("rc")) is int
            and gate["rc"] in (0, 2)
            and info.get("verdict") == gate.get("verdict")
            and gate["rc"]
            == {"ok": 0, "first_seen": 0, "hold": 2}.get(
                info.get("verdict")
                if info.get("verdict") in {"ok", "first_seen", "hold"}
                else info.get("raw_verdict")
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise refresh.GlobalStageError(
            f"pre-seal gate report is invalid: {source}"
        ) from exc
    require_pipeline_owner(manifest, _OWNER_LEASE.get())
    if (
        not valid
        or refresh.sha256_file(path) != digest
        or refresh.sha256_file(artifact) != binding["artifact_sha256"]
        or refresh.git_identity() != (binding["collector_git_sha"], False)
        or manifest["collector_git_sha"] != binding["collector_git_sha"]
        or manifest["run_id"] != binding["generation_id"]
        or refresh.manifest_database_target_sha256(manifest)
        != binding["database_target_sha256"]
    ):
        raise refresh.GlobalStageError(
            f"pre-seal gate evidence changed or is invalid: {source}"
        )
    if refresh.gate_verdict_is_admitted(manifest, gate["verdict"], source=source):
        return (preseal_essex_identity(run_dir, manifest, artifact, env_file, path, digest, dict(gate))
                if source == "essex-realty" else True)
    if (gate["verdict"], gate["rc"]) not in {
        ("hold", 2),
        ("first_seen", 0),
    } or (
        not {
            "current_active",
            "baseline_median",
            "effective_floor",
            "floor_policy",
            "verdict",
            "reason",
            "mark_missing_safe",
        }
        <= info.keys()
        or type(info["current_active"]) is not int
        or info["current_active"] < 0
        or type(info["effective_floor"]) is not int
        or (
            info["baseline_median"] is not None
            and type(info["baseline_median"]) is not int
        )
        or not isinstance(info["floor_policy"], (str, type(None)))
        or not isinstance(info["reason"], str)
        or info["mark_missing_safe"] is not False
    ):
        raise refresh.GlobalStageError(
            f"unrecognized pre-seal source gate refusal: {source}"
        )
    gate.update(sha256=digest, preseal_binding=binding)
    checkpoint["state"] = (
        "baseline_seed_required" if gate["verdict"] == "first_seen" else "gate_blocked"
    )
    refresh.save_manifest(run_dir, manifest)
    return False


def require_pipeline_full_report(report: dict) -> None:
    """New pipeline evidence never inherits the serial legacy-report waiver."""
    import cre_checkpoint_refresh as refresh

    if (
        report.get("validation_profile") != refresh.FULL_VALIDATION_PROFILE
        or report.get("audit_scope")
        != refresh.VALIDATION_AUDIT_SCOPES[refresh.FULL_VALIDATION_PROFILE]
    ):
        raise refresh.GlobalStageError(
            "pipeline requires an explicit complete full audit"
        )
    try:
        refresh.require_full_validation_report(report)
    except (TypeError, ValueError) as exc:
        raise refresh.GlobalStageError("pipeline full audit is incomplete") from exc


@dataclass(frozen=True)
class InitialBeforeReport:
    """In-memory handoff of this owner's just-qualified startup comparison."""

    path: Path
    sha256: str
    run_id: str
    target: str
    owner_pid: int
    owner_lease: str
    commit_sequence: int = 0


def initial_preflight_binding(run_dir, manifest, *, fresh, resumed, owner_check):
    """Never manufacture a runtime handoff from persisted resume evidence."""
    import cre_checkpoint_refresh as refresh

    if (
        not fresh
        or resumed
        or (manifest.get("pipeline") or {}).get("commit_sequence", 0) != 0
    ):
        return None
    owner_check()
    preflight = manifest["preflight"]
    target = refresh.manifest_database_target_sha256(manifest)
    lock = Path(preflight["shared_lock"])
    lease = refresh._lock_lease(lock)
    if not lease or refresh._lock_owner(lock) != os.getpid() or not target:
        return None
    path = run_dir / preflight["validation_path"]
    if refresh.sha256_file(path) != preflight["validation_sha256"]:
        raise refresh.GlobalStageError("initial qualified preflight changed")
    report = refresh._load_json(path)
    refresh.require_checkpoint_preflight_report(
        report, expected_profile=refresh.CHECKPOINT_PREFLIGHT_PROFILE
    )
    refresh.require_lifecycle_schema_contract(report)
    return InitialBeforeReport(
        path,
        preflight["validation_sha256"],
        manifest["run_id"],
        target,
        os.getpid(),
        lease,
    )


def reuse_initial_before(run_dir, manifest, unit, initial_before):
    import cre_checkpoint_refresh as refresh

    if not isinstance(initial_before, InitialBeforeReport) or (
        initial_before.run_id != manifest["run_id"]
        or initial_before.target != refresh.manifest_database_target_sha256(manifest)
        or initial_before.owner_pid != os.getpid()
        or initial_before.owner_lease != _OWNER_LEASE.get()
        or initial_before.commit_sequence != 0
        or manifest["pipeline"]["commit_sequence"] != 0
    ):
        return None
    require_pipeline_owner(manifest, initial_before.owner_lease)
    if refresh.sha256_file(initial_before.path) != initial_before.sha256:
        raise refresh.GlobalStageError("initial qualified preflight changed")
    report = refresh._load_json(initial_before.path)
    refresh.require_checkpoint_preflight_report(
        report, expected_profile=refresh.CHECKPOINT_PREFLIGHT_PROFILE
    )
    refresh.require_lifecycle_schema_contract(report)
    before = run_dir / "pipeline" / unit["unit_id"] / "before.json"
    before.parent.mkdir(parents=True, exist_ok=True)
    refresh.atomic_write_text(before, initial_before.path.read_bytes().decode("utf-8"))
    refresh.make_private_artifact(before)
    if (
        refresh.sha256_file(before) != initial_before.sha256
        or refresh.sha256_file(initial_before.path) != initial_before.sha256
    ):
        raise refresh.GlobalStageError(
            "initial qualified preflight changed during handoff"
        )
    unit.update(
        before_reused=True,
        before_reuse_kind="initial_preflight",
        before_source_path=str(initial_before.path.relative_to(run_dir)),
    )
    return before


def unit_before_report(
    run_dir: Path,
    manifest: dict,
    unit: dict,
    env_file: str | None,
    previous: dict | None,
    *,
    initial_before: InitialBeforeReport | None = None,
) -> Path:
    import cre_checkpoint_refresh as refresh

    target = refresh.manifest_database_target_sha256(manifest)
    sequence = manifest["pipeline"]["commit_sequence"]
    handed_before = (
        reuse_initial_before(run_dir, manifest, unit, initial_before)
        if previous is None
        else None
    )
    if previous is not None:
        if (
            previous.get("target") != target
            or previous.get("sequence") != sequence
            or previous.get("run_id") != manifest["run_id"]
        ):
            raise refresh.GlobalStageError(
                "pipeline prior audit sequence or target changed"
            )
        before = run_dir / previous["path"]
        if refresh.sha256_file(before) != previous["sha256"]:
            raise refresh.GlobalStageError("pipeline prior qualified audit changed")
        require_pipeline_full_report(refresh._load_json(before))
        unit["before_reused"] = True
    elif handed_before is not None:
        before = handed_before
    else:
        # Fresh comparison whenever this uninterrupted owner has no preceding
        # qualified report. A resumed/stale persisted report is never a cache.
        before = run_dir / "pipeline" / unit["unit_id"] / "before.json"
        rc = refresh.run_command(
            refresh.build_validate_argv(
                before,
                env_file,
                expected_db_target_sha256=target,
                profile=refresh.CHECKPOINT_PREFLIGHT_PROFILE,
            ),
            run_dir / "logs" / f"{unit['unit_id']}-pre-validation.log",
            env=refresh.safe_process_env(),
        )
        if rc != 0:
            raise refresh.GlobalStageError("pipeline before comparison failed")
        report = refresh._load_json(before)
        refresh.require_checkpoint_preflight_report(
            report, expected_profile=refresh.CHECKPOINT_PREFLIGHT_PROFILE
        )
        refresh.require_lifecycle_schema_contract(report)
        unit["before_reused"] = False
    unit["before_path"] = str(before.relative_to(run_dir))
    unit["before_sha256"] = refresh.sha256_file(before)
    refresh.save_manifest(run_dir, manifest)
    return before


def run_database_unit(
    run_dir: Path,
    manifest: dict,
    unit: dict,
    env_file: str | None,
    previous: dict | None,
    *,
    initial_before: InitialBeforeReport | None = None,
) -> dict:
    """Retain existing gates, serial transactions, full audit and exact readback."""
    import cre_checkpoint_refresh as refresh

    sources = tuple(unit["sources"])
    evidence = run_dir / "pipeline" / unit["unit_id"]
    evidence.mkdir(parents=True, exist_ok=True)
    before = unit_before_report(
        run_dir, manifest, unit, env_file, previous, initial_before=initial_before
    )
    for source in sources:
        checkpoint = manifest["sources"][source]
        artifact = run_dir / checkpoint["artifact"]["path"]
        if not refresh.dry_run_source(run_dir, manifest, source, artifact):
            raise refresh.GlobalStageError(f"sealed source dryrun failed: {source}")
    unit["aggregate_gate"] = dict(
        refresh.run_aggregate_gate(
            run_dir, manifest, env_file, sources=sources, evidence_dir=evidence
        )
    )
    unit["state"] = "committing"
    refresh.save_manifest(run_dir, manifest)
    for source in sources:
        checkpoint = manifest["sources"][source]
        checkpoint["pipeline_stage"] = "committing"
        existing = refresh._manifest_checkpoint_artifact_valid(
            run_dir, manifest, source
        )
        if not existing:
            raise refresh.GlobalStageError(
                f"sealed artifact changed or expired: {source}"
            )
        # Repeat exact source/aggregate proof immediately before each write.
        if (refresh.current_inventory.admission_configuration(manifest["config"], source)
                or refresh.colliers_partial_candidate(manifest, source)
                or refresh.franklin_partial_candidate(manifest, source)
                or refresh.lyon_partial_candidate(manifest, source)
                or refresh.nai_post_partial_candidate(manifest, source)):
            aggregate = refresh._load_json(evidence / "aggregate-gate.json")
            refresh._admit_current_inventory(
                run_dir, manifest, aggregate, existing[0], "aggregate", source=source
            )
        require_pipeline_owner(manifest, _OWNER_LEASE.get())
        refresh.ingest_source(run_dir, manifest, source, existing[0], env_file)
        manifest["pipeline"]["source_timings"][source]["ingested_at"] = (
            refresh.utc_now()
        )
        manifest["pipeline"]["commit_sequence"] += 1
        checkpoint["pipeline_stage"] = "validating"
    unit["state"] = "validating"
    refresh.save_manifest(run_dir, manifest)
    if refresh.sha256_file(before) != unit["before_sha256"]:
        raise refresh.GlobalStageError("pipeline before comparison changed during unit")
    validation = refresh.run_final_validation(
        run_dir,
        manifest,
        env_file,
        sources=sources,
        evidence_dir=evidence,
        before_path=before,
    )
    if "colliers" in sources and refresh.colliers_partial_candidate(manifest, "colliers"):
        settle_colliers_partial_cursor(run_dir, manifest)
    unit["validation"] = dict(validation)
    unit["validation_path"] = validation["path"]
    unit["validation_sha256"] = refresh.sha256_file(run_dir / validation["path"])
    return {
        "path": unit["validation_path"],
        "sha256": unit["validation_sha256"],
        "sequence": manifest["pipeline"]["commit_sequence"],
        "target": refresh.manifest_database_target_sha256(manifest),
        "run_id": manifest["run_id"],
    }


def settle_colliers_partial_cursor(run_dir: Path, manifest: dict) -> None:
    """Publish an already verified partial position under the current owner."""
    import cre_checkpoint_refresh as refresh
    from cre_partial_cursor import advance_cursor

    checkpoint = manifest["sources"]["colliers"]
    binding = (checkpoint.get("gate") or {}).get("current_inventory_proof") or {}
    readback = checkpoint.get("readback") or {}
    proof = readback.get("observation")
    if (readback.get("ok") is not True or not isinstance(proof, dict)
            or checkpoint.get("partial_cursor_settlement") is not None):
        raise refresh.GlobalStageError("Colliers partial cursor lacks exact settlement")
    lock_path = (manifest.get("preflight") or {}).get("shared_lock")
    if not isinstance(lock_path, str):
        raise refresh.GlobalStageError("Colliers partial cursor lacks owner path")

    def check_owner():
        require_pipeline_owner(manifest, _OWNER_LEASE.get())

    check_owner()
    state = checkpoint["partial_cursor_settlement"] = {
        "state": "publishing", "attempt_id": proof.get("attempt_id"),
        "cursor": binding.get("cursor"), "next_cursor": binding.get("next_cursor"),
        "artifact_sha256": binding.get("artifact_sha256"),
    }
    refresh.save_manifest(run_dir, manifest)
    try:
        advance_cursor(Path(lock_path), "colliers", binding.get("database_target_sha256"),
            binding.get("cursor"), binding.get("next_cursor"), generation=binding.get("generation_id"),
            artifact_sha256=binding.get("artifact_sha256"), attempt_id=proof.get("attempt_id"),
            outcome="exact_commit", proof=proof, owner_check=check_owner)
        check_owner()
    except BaseException:
        state["state"] = "recovery_required"
        refresh.save_manifest(run_dir, manifest)
        raise
    state["state"] = "settled"
    refresh.save_manifest(run_dir, manifest)


def reconcile_pipeline_ingests(
    run_dir: Path, manifest: dict, env_file: str | None, *, probe_allowed: bool
) -> bool:
    """Reuse exact existing readbacks while this canonical owner is still alive.

    Never replay ingestion. A confirmed rollback deliberately still stops this
    pipeline; a new generation can begin only after ordinary owner settlement.
    """
    import cre_checkpoint_refresh as refresh

    if probe_allowed and _OWNER_CHECK.get() is not None:
        try:
            require_pipeline_owner(manifest, _OWNER_LEASE.get())
        except Exception:
            probe_allowed = False
    uncertain = [
        source
        for source, checkpoint in manifest["sources"].items()
        if checkpoint.get("state") in {"ingesting", "ingest_recovery_required"}
    ]
    unresolved = False
    manifest["pipeline"]["commit_outcome_unknown"] = bool(uncertain)
    for source in uncertain:
        checkpoint = manifest["sources"][source]
        if probe_allowed:
            try:
                refresh.recover_interrupted_ingest(run_dir, manifest, source, env_file)
            except Exception as probe_error:
                if refresh._cleanup_unknown_in_chain(probe_error):
                    raise
                recovery = checkpoint.get("ingest_recovery") or {}
                if not (
                    checkpoint.get("state") == "dry_run_passed"
                    and recovery.get("outcome") == "exact_rollback"
                    and recovery.get("replay_safe") is True
                ):
                    unresolved = True
        else:
            unresolved = True
        if checkpoint.get("state") not in {"ingested", "dry_run_passed"}:
            unresolved = True
    manifest["pipeline"]["commit_outcome_unknown"] = unresolved
    return not unresolved


def run_bounded_pipeline(
    run_dir,
    manifest,
    sources,
    *,
    page_cap,
    concurrency,
    attempts_this_run,
    env_file,
    source_workers,
    owner_check: Callable[[], None] | None = None,
    initial_before: InitialBeforeReport | None = None,
):
    """Run a finite rolling acquisition pool inside the existing flat owner."""
    import cre_checkpoint_refresh as refresh

    selected = refresh.scoped_source_keys(manifest, sources)
    source_workers = resolve_source_workers(True, source_workers)
    if manifest.get("pipeline"):
        raise refresh.GlobalStageError(
            "pipeline resume requires manual exact-generation reconciliation; no mixed-stage replay"
        )
    lock_path = (manifest.get("preflight") or {}).get("shared_lock")
    lease = refresh._lock_lease(Path(lock_path)) if isinstance(lock_path, str) else None
    if owner_check is not None:
        owner_check()
    require_pipeline_owner(manifest, lease)
    state = manifest["pipeline"] = {
        "status": "running",
        "source_workers": source_workers,
        "units": [],
        "source_results": {},
        "source_timings": {
            source: {"queued_at": refresh.utc_now()} for source in selected
        },
        "active_unit": None,
        "commit_sequence": 0,
    }
    active = []
    ready = []
    partial_ready = []
    pending = list(selected)
    terminal_source_holds: set[str] = set()
    sealed = ()
    preparing = ()
    dispatch_pending = False
    clock_deadlines = {}
    budget = PipelineBudget()
    prior_attempts = {
        source: len(manifest["sources"][source]["attempts"]) for source in selected
    }
    previous = None
    root_token = _COMPUTE_ROOT.set(run_dir)
    owner_token = _OWNER_CHECK.set(owner_check)
    lease_token = _OWNER_LEASE.set(lease)

    def counts():
        source_results = state["source_results"]
        new = {
            "fetching": len(active),
            "pending": len(ready) + len(partial_ready),
            "validating": sum(
                manifest["sources"][source].get("pipeline_stage") == "validating"
                for source in (*sealed, *preparing)
            ),
            "committing": sum(
                manifest["sources"][source].get("pipeline_stage") == "committing"
                for source in sealed
            ),
            "verified": sum(
                result.get("state") == "verified" for result in source_results.values()
            ),
            "failed": sum(
                result.get("state") == "failed" for result in source_results.values()
            ),
        }
        if state.get("counts") != new:
            state["counts"] = new
            refresh.save_manifest(run_dir, manifest)

    def retry_or_fail(source, reason="collection failed"):
        checkpoint = manifest["sources"][source]
        if (
            not refresh.has_terminal_collection_failure(source, checkpoint)
            and len(checkpoint["attempts"]) - prior_attempts[source] < attempts_this_run
            and checkpoint.get("state") not in {
                "collect_infrastructure_failed",
                "artifact_rejected",
                "gate_blocked",
                "baseline_seed_required",
            }
        ):
            pending.append(source)
        else:
            checkpoint["pipeline_stage"] = "failed"
            state["source_results"][source] = {"state": "failed", "reason": reason}

    def tick():
        nonlocal dispatch_pending
        require_pipeline_owner(manifest, lease)
        # Stable list order is configured dispatch order for simultaneous exits.
        for item in list(active):
            if item.process.poll() is None:
                continue
            clock_key = ("finish", item.source)
            try:
                admitted = refresh._finalize_cohort_collection(
                    run_dir, manifest, item, candidate_only=True,
                    clock_expired=clock_key in clock_deadlines and time.monotonic() >= clock_deadlines[clock_key],
                )
            except refresh.ClockOrderingPending:
                clock_deadlines.setdefault(clock_key, time.monotonic() + 1.0)
                continue
            clock_deadlines.pop(clock_key, None)
            active.remove(item)
            if admitted:
                ready.append(item.source)
                state["source_timings"][item.source]["collection_finished_at"] = (
                    getattr(item, "attempt", None) or {}
                ).get("finished_at") or refresh.utc_now()
            elif (item.source in {"colliers", "lyon-stahl"} and not item.attempt.get("clock_order_refused")
                  and item.attempt.get("rc") == refresh.COHORT_SETTLED_FAILURE_EXIT
                  and (item.attempt.get(f"{item.source}_partial") or {}).get("state") == "sealed_local"
                  and item.attempt[f"{item.source}_partial"].get("outcome") == "source_error"
                  and not manifest["sources"][item.source].get("terminal_collection_failure")):
                # Process only on the foreground lane. Finalization proved the
                # child group settled and sealed the original failed artifact.
                partial_ready.append(item.source)
            else:
                retry_or_fail(item.source)
        counts()
        if partial_ready:
            # Do not start an exclusive source before the foreground partial
            # lane has had a chance to claim its settled work.
            return
        oversized = any(
            (manifest["sources"][source].get("candidate") or {}).get("bytes", 0)
            >= budget.soft_bytes
            for source in (*ready, *preparing, *sealed)
        )
        if oversized:
            return
        if (
            set(ready) & refresh.COHORT_EXCLUSIVE_SOURCE_KEYS
            or set(sealed) & refresh.COHORT_EXCLUSIVE_SOURCE_KEYS
            or set(preparing) & refresh.COHORT_EXCLUSIVE_SOURCE_KEYS
        ):
            return
        excluded = {
            run_dir / checkpoint["artifact"]["path"]
            for source, checkpoint in manifest["sources"].items()
            if checkpoint.get("artifact")
            and (
                source in sealed
                or state["source_results"].get(source, {}).get("state") == "verified"
                or (
                    source in terminal_source_holds
                    and checkpoint.get("state") in {"gate_blocked", "baseline_seed_required", "pyramid_historical_guard_refused", "transwestern_image_guard_refused", "jll_image_guard_refused", "settled_ingest_failed"}
                )
            )
        }
        candidates = refresh.select_cohort_sources(
            pending, [item.source for item in active], source_workers=source_workers
        )
        for source in candidates:
            if source in refresh.COHORT_EXCLUSIVE_SOURCE_KEYS and (
                active or ready or sealed or preparing
            ):
                continue
            active_sizes = [
                item.tmp_artifact.stat().st_size if item.tmp_artifact.exists() else 0
                for item in active
            ]
            observed = pending_disk_bytes(run_dir, excluded)
            free = shutil.disk_usage(run_dir).free
            if not budget.can_dispatch(
                pending_count=len(ready) + len(active) + len(preparing),
                observed_bytes=observed,
                active_sizes=active_sizes,
                free_bytes=free,
            ):
                break
            dispatch_pending = True
            try:
                started = refresh._start_cohort_collection(
                    run_dir, manifest, source, page_cap=page_cap, concurrency=concurrency
                )
            except refresh.ClockOrderingPending:
                dispatch_pending = False
                clock_key = ("start", source)
                deadline = clock_deadlines.setdefault(clock_key, time.monotonic() + 1.0)
                if time.monotonic() >= deadline:
                    clock_deadlines.pop(clock_key, None)
                    pending.remove(source)
                    manifest["sources"][source]["state"] = "collect_infrastructure_failed"
                    manifest["sources"][source]["clock_hold"] = {"phase": "start", "generation_id": manifest["run_id"],
                        "attempt_count": len(manifest["sources"][source]["attempts"])}
                    retry_or_fail(source, "wall clock did not reach generation start")
                else:
                    pending.remove(source)
                    pending.append(source)
                continue
            clock_deadlines.pop(("start", source), None)
            pending.remove(source)
            if started is not None:
                active.append(started)
                state["source_timings"][source].setdefault(
                    "collection_started_at",
                    getattr(started, "attempt_started_at", None) or refresh.utc_now(),
                )
            else:
                retry_or_fail(source)
            dispatch_pending = False
        counts()

    cursor_recovery_checked = False

    def recover_colliers_before_window(owner):
        config = manifest["config"]
        if (not isinstance(owner, refresh.SharedLock) or config.get("additive") is not True
                or config.get("mark_missing") is not False or config.get("status_activation") is not False
                or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0):
            return True
        from recovery.colliers_cursor import SourceEvidenceRefusal, recover_prior_generation

        def publication_state(unknown):
            require_pipeline_owner(manifest, lease)
            state["cursor_recovery_outcome_unknown"] = unknown
            refresh.save_manifest(run_dir, manifest)

        try:
            recovered = recover_prior_generation(owner,
                expected_target=refresh.manifest_database_target_sha256(manifest), env_file=env_file,
                before_publish=lambda: publication_state(True), after_publish=lambda: publication_state(False))
        except SourceEvidenceRefusal as refusal:
            if state.get("cursor_recovery_outcome_unknown") is True:
                raise
            require_pipeline_owner(manifest, lease)
            if refresh.database_target_fingerprint(env_file) != manifest["preflight"]["database_target"]:
                raise refresh.GlobalStageError("Colliers recovery target changed")
            manifest["sources"]["colliers"].update(state="gate_blocked", pipeline_stage="failed")
            # Record only a small, controlled classification, never raw
            # exception text or provider data.
            refusal_code = refusal.code if refusal.code in {
                "quality_regression", "readback_mismatch", "historical_readback_refused"
            } else "source_evidence_refused"
            state["source_results"]["colliers"] = {"state": "failed", "reason": "prior cursor recovery refused",
                                                   "refusal_code": refusal_code}
            terminal_source_holds.add("colliers")
            refresh.save_manifest(run_dir, manifest)
            return False
        if recovered is not None and recovered.get("history_unavailable") is True:
            require_pipeline_owner(manifest, lease)
            if refresh.database_target_fingerprint(env_file) != manifest["preflight"]["database_target"]:
                raise refresh.GlobalStageError("Colliers recovery target changed")
            manifest["sources"]["colliers"]["cursor_recovery"] = {
                "state": "prior_cursor_history_unavailable",
                "generation": recovered["generation"], "position": recovered["position"],
                "prior_window_qualified": False, "whole_source_coverage": False,
            }
            refresh.save_manifest(run_dir, manifest)
        elif recovered is not None:
            manifest["sources"]["colliers"]["cursor_recovery"] = {
                "generation": recovered["generation"], "attempt_id": recovered["attempt_id"],
                "artifact_sha256": recovered["artifact_sha256"], "position": recovered["next_cursor"],
                "whole_source_coverage": False, "validation_path": recovered["validation_path"],
                "validation_sha256": recovered["validation_sha256"],
            }
            refresh.save_manifest(run_dir, manifest)
        return True

    try:
        with command_poll_hook(tick) as poll:
            while pending or active or ready or partial_ready:
                poll()
                # Sealing may occur inside tick's poll callback. Database writes
                # run only here, on the foreground coordinator's serial lane.
                refresh.pump_source_evidence(run_dir, manifest)
                if partial_ready and not ready:
                    source = partial_ready.pop(0)
                    preparing = (source,)
                    require_pipeline_owner(manifest, _OWNER_LEASE.get())
                    owner = getattr(_OWNER_CHECK.get(), "__self__", None)
                    if source == "colliers" and not cursor_recovery_checked:
                        cursor_recovery_checked = True
                        if not recover_colliers_before_window(owner):
                            preparing = ()
                            counts()
                            continue
                    if source == "lyon-stahl":
                        from cre_lyon_partial_checkpoint import run_replay_worker
                        candidate_path = run_replay_worker(run_dir, manifest, owner, refresh.save_manifest)
                    else:
                        from cre_colliers_partial_checkpoint import run_window_worker
                        windows = manifest["sources"][source].get("partial_windows", [])
                        candidate_path = run_window_worker(
                            run_dir, manifest, owner, refresh.run_command, refresh.save_manifest,
                            window_index=len(windows) + 1,
                            stop_before_position=windows[0]["first_position"] if windows else None)
                    require_pipeline_owner(manifest, _OWNER_LEASE.get())
                    preparing = ()
                    if candidate_path is not None:
                        ready.append(source)
                    else:
                        checkpoint = manifest["sources"][source]
                        if source == "colliers":
                            from cre_colliers_partial_checkpoint import (
                                archive_no_write_pipeline_window, prepare_next_pipeline_window,
                                restore_last_verified_pipeline_window,
                            )
                            if checkpoint.get("state") == "partial_held_no_write":
                                more = archive_no_write_pipeline_window(run_dir, manifest, owner)
                                refresh.save_manifest(run_dir, manifest)
                                if more:
                                    prepare_next_pipeline_window(checkpoint)
                                    refresh.save_manifest(run_dir, manifest)
                                    partial_ready.append(source)
                                    counts()
                                    continue
                            previous_result = restore_last_verified_pipeline_window(checkpoint)
                            if previous_result is not None:
                                state["source_results"][source] = previous_result
                                refresh.save_manifest(run_dir, manifest)
                                counts()
                                continue
                        terminal_source_holds.add(source)
                        checkpoint["pipeline_stage"] = "failed"
                        state["source_results"][source] = {"state": "failed", "partial": True,
                            "reason": "partial source window did not admit listing writes"}
                        refresh.save_manifest(run_dir, manifest)
                        counts()
                        continue

                    counts()
                if ready:
                    oversized_index = next(
                        (
                            index
                            for index, source in enumerate(ready)
                            if (manifest["sources"][source].get("candidate") or {}).get(
                                "bytes", 0
                            )
                            >= budget.soft_bytes
                        ),
                        None,
                    )
                    if oversized_index is not None and active:
                        time.sleep(0.05)
                        continue
                    take = (
                        budget.max_pending
                        if oversized_index is None
                        else (oversized_index or 1)
                    )
                    # A Pyramid write owns a singleton unit. Previously
                    # completed siblings therefore use the normal verified
                    # unit/baseline chain before a source-local rollback.
                    guarded = next((source for source in ready[:take] if source in {"pyramid-brokerage", "transwestern", "jll", "daum-commercial"}
                                    or refresh.colliers_partial_candidate(manifest, source)
                                    or (source == "colliers" and
                                        (manifest["sources"][source].get("attempts") or [{}])[-1]
                                        .get("colliers_partial", {}).get("outcome") == "source_error")), None)
                    if guarded is not None:
                        index = ready.index(guarded)
                        take = index if index else 1
                    preparing = tuple(ready[:take])
                    del ready[: len(preparing)]
                    unit_id = f"unit-{len(state['units']) + 1:04d}"
                    unit = {
                        "unit_id": unit_id,
                        "candidate_sources": list(preparing),
                        "sources": [],
                        "state": "preparing",
                        "started_at": refresh.utc_now(),
                    }
                    state["units"].append(unit)
                    state["active_unit"] = unit_id
                    for source in preparing:
                        checkpoint = manifest["sources"][source]
                        checkpoint["pipeline_stage"] = "validating"
                        state["source_timings"][source]["validation_started_at"] = (
                            refresh.utc_now()
                        )
                    counts()
                    refresh.save_manifest(run_dir, manifest)
                    prepared = []
                    for source in preparing:
                        try:
                            if not prepare_candidate(run_dir, manifest, source):
                                raise refresh.ArtifactValidationError(
                                    "candidate validation failed"
                                )
                        except refresh.ArtifactValidationError as exc:
                            checkpoint = manifest["sources"][source]
                            checkpoint["state"] = "artifact_rejected"
                            checkpoint["attempts"][-1]["error"] = str(exc)
                            archive_failed_candidate(run_dir, manifest, source)
                            partial = checkpoint["attempts"][-1].get("colliers_partial") or {}
                            if (source == "colliers" and partial.get("state") == "sealed_local"
                                    and partial.get("outcome") == "source_error"):
                                # Run partial windows on the regular foreground lane,
                                # after already-ready peers have had their turn.
                                partial_ready.append(source)
                                continue

                            retry_or_fail(source, "artifact validation failed")
                            continue
                        prepared.append(source)
                    # Check each prepared source once at the final pre-seal
                    # boundary, before any member can enter a database unit.
                    qualified = []
                    for source in prepared:
                        if preseal_source_gate(run_dir, manifest, source, env_file):
                            qualified.append(source)
                        else:
                            terminal_source_holds.add(source)
                            reason = ("all emitted observations held by target checks"
                                      if manifest["sources"][source].get("partial_probe_hold") == "all_emitted_target_held"
                                      else "pre-seal source gate refused")
                            retry_or_fail(source, reason)
                    sealed = tuple(qualified)
                    preparing = ()
                    unit.update(
                        sources=list(sealed), state="sealed" if sealed else "rejected"
                    )
                    refresh.save_manifest(run_dir, manifest)
                    if not sealed:
                        unit["finished_at"] = refresh.utc_now()
                        # No database unit was sealed; source receipts retain
                        # rejected candidates without publishing empty members.
                        state["units"].pop()
                        state["active_unit"] = None
                        counts()
                        continue
                    try:
                        if initial_before is None:
                            previous = run_database_unit(
                                run_dir, manifest, unit, env_file, previous
                            )
                        else:
                            previous = run_database_unit(
                                run_dir, manifest, unit, env_file, previous,
                                initial_before=initial_before,
                            )
                    except refresh.SourceGuardRefusal as exc:
                        from cre_pyramid_rollback_result import verified_rollback
                        require_pipeline_owner(manifest, _OWNER_LEASE.get())
                        if isinstance(exc, refresh.SettledIngestFailure):
                            from recovery.atomic_ingest_rollback import verified_rollback as verify_atomic
                            verified = len(sealed) == 1 and verify_atomic(run_dir, manifest, sealed[0])
                        else:
                            verified = (len(sealed) == 1 and sealed[0] in {"pyramid-brokerage", "transwestern", "jll"}
                                        and verified_rollback(run_dir, manifest, sealed[0]))
                        if not verified:
                            raise refresh.GlobalStageError("source-local rollback evidence changed")
                        source = sealed[0]
                        manifest["sources"][source]["pipeline_stage"] = "failed"
                        terminal_source_holds.add(source)
                        state["source_results"][source] = {
                            "state": "failed", "reason": ("settled_ingest_failure" if isinstance(exc, refresh.SettledIngestFailure) else "pyramid_historical_identity_guard" if source == "pyramid-brokerage" else "jll_image_guard_refusal" if source == "jll" else "Transwestern protected image multiplicity lost"),
                            "unit_id": unit_id,
                        }
                        unit.update(state="source_held", finished_at=refresh.utc_now())
                        sealed = ()
                        state["active_unit"] = None
                        counts()
                        refresh.save_manifest(run_dir, manifest)
                        continue
                    if any(
                        (manifest["sources"][source].get("readback") or {}).get("ok")
                        is not True
                        for source in sealed
                    ):
                        raise refresh.GlobalStageError(
                            "pipeline member lacks exact readback"
                        )
                    unit.update(state="verified", finished_at=refresh.utc_now())
                    for source in sealed:
                        checkpoint = manifest["sources"][source]
                        checkpoint["pipeline_stage"] = "verified"
                        state["source_timings"][source]["verified_at"] = (
                            refresh.utc_now()
                        )
                        state["source_results"][source] = {
                            "state": "verified",
                            "unit_id": unit_id,
                            "validation_path": unit["validation_path"],
                            "validation_sha256": unit["validation_sha256"],
                            "readback_ok": True,
                            "generation_id": manifest["run_id"],
                            "artifact_sha256": checkpoint["artifact"]["sha256"],
                        }
                        if refresh.verified_subset_candidate(manifest, source):
                            binding = checkpoint.get("gate", {}).get("current_inventory_proof", {})
                            state["source_results"][source].update(
                                qualification="qualified_partial_subset", partial=True,
                                whole_source_coverage=False,
                                emitted_count=binding.get("emitted_count"), held_count=binding.get("held_count"),
                            )
                            if binding.get("schema") == "nai-public-post-partial-v1":
                                state["source_results"][source]["visibility_limitation"] = (
                                    "new post rows with unknown status are excluded from active listing views")
                            if binding.get("schema") == "franklin-site-partial-v1":
                                state["source_results"][source].update(
                                    buildout_pending=binding.get("buildout_pending"),
                                    held_observations=binding.get("held_observations"),
                                    identity_uncertainty=binding.get("identity_uncertainty"),
                                    visibility_limitation="site-card rows without explicit ACTIVE status are excluded from active views",
                                )
                            if binding.get("schema") == "pyramid-incomplete-v2":
                                state["source_results"][source].update(
                                    incomplete_count=binding.get("incomplete_count"),
                                    transition_count=binding.get("transition_count"),
                                    missing_field_counts=binding.get("missing_field_counts"),
                                )
                    if sealed == ("colliers",) and refresh.colliers_partial_candidate(manifest, "colliers"):
                        from cre_colliers_partial_checkpoint import archive_verified_pipeline_window, prepare_next_pipeline_window
                        require_pipeline_owner(manifest, _OWNER_LEASE.get())
                        owner = getattr(_OWNER_CHECK.get(), "__self__", None)
                        more = archive_verified_pipeline_window(
                            run_dir, manifest, unit, state["source_results"]["colliers"], owner)
                        refresh.save_manifest(run_dir, manifest)
                        if more:
                            prepare_next_pipeline_window(manifest["sources"]["colliers"])
                            refresh.save_manifest(run_dir, manifest)
                            partial_ready.append("colliers")
                    sealed = ()
                    state["active_unit"] = None
                    counts()
                    refresh.save_manifest(run_dir, manifest)

                elif active:
                    time.sleep(0.05)
                elif pending:
                    if any(key[0] == "start" for key in clock_deadlines):
                        time.sleep(0.05)
                        continue
                    raise refresh.GlobalStageError(
                        "pipeline disk/output admission cannot make progress"
                    )
        failures = [
            source
            for source in selected
            if state["source_results"].get(source, {}).get("state") != "verified"
        ]
        state["status"] = "partial" if failures or any(
            item.get("partial") is True for item in state["source_results"].values()
        ) else "complete"
        refresh.save_manifest(run_dir, manifest)
        return failures
    except BaseException as exc:
        state["commit_outcome_unknown"] = any(
            checkpoint.get("state") in {"ingesting", "ingest_recovery_required"}
            for checkpoint in manifest["sources"].values()
        )
        checkpoint = manifest["sources"].get("colliers", {})
        worker_state = (checkpoint.get("colliers_partial_worker") or {}).get("state")
        if (checkpoint.get("partial_windows") and checkpoint.get("state") != "ingested"
                and not state["commit_outcome_unknown"] and not refresh._cleanup_unknown_in_chain(exc)
                and worker_state not in {"cursor_publishing", "cursor_recovery_required", "settlement_unknown"}):
            from cre_colliers_partial_checkpoint import restore_last_verified_pipeline_window
            prior_result = restore_last_verified_pipeline_window(checkpoint)
            if prior_result is not None:
                state["source_results"]["colliers"] = prior_result
        state["status"] = "reconciliation_required"

        if state["active_unit"]:
            unit = state["units"][-1]
            if unit["state"] == "preparing" and unit["sources"] == []:
                # Preparation has no database directory, members or writes.
                # Keep source evidence without publishing an empty unit.
                state["units"].pop()
            else:
                unit.update(state="failed", finished_at=refresh.utc_now())
        try:
            refresh._terminate_cohort_processes(active)
            active.clear()
        finally:
            if dispatch_pending and not refresh._cleanup_unknown_in_chain(exc):
                refresh._raise_unsettled_dispatch(exc)
        for source in selected:
            if state["source_results"].get(source, {}).get("state") != "verified":
                manifest["sources"][source]["pipeline_stage"] = "failed"
                state["source_results"][source] = {
                    "state": "failed",
                    "reason": "global pipeline stop",
                }
        ready.clear()
        preparing = sealed = ()
        state["active_unit"] = None
        counts()
        reconcile_pipeline_ingests(
            run_dir,
            manifest,
            env_file,
            probe_allowed=not isinstance(exc, KeyboardInterrupt)
            and not refresh._cleanup_unknown_in_chain(exc),
        )
        refresh.save_manifest(run_dir, manifest)
        raise
    finally:
        _COMPUTE_ROOT.reset(root_token)
        _OWNER_CHECK.reset(owner_token)
        _OWNER_LEASE.reset(lease_token)
